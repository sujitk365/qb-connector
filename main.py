from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, PlainTextResponse
import html
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, List, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Logging (console + rotating file) ────────────────────────────────────────
LOG = logging.getLogger("qb_connector")


def _setup_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = os.getenv("LOG_FILE", "logs/qb-connector.log").strip() or "logs/qb-connector.log"
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        return
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, mode=0o755, exist_ok=True)
    try:
        fh = RotatingFileHandler(
            log_file, maxBytes=int(os.getenv("LOG_MAX_BYTES", "5242880")), backupCount=int(os.getenv("LOG_BACKUP_COUNT", "3"))
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
        LOG.info("File logging enabled: %s", os.path.abspath(log_file))
    except OSError as e:
        LOG.warning("Could not open log file %s: %s", log_file, e)
    logging.getLogger("httpx").setLevel(logging.WARNING)


_setup_logging()

# ── Config ──────────────────────────────────────────────────────────────────
QB_USERNAME = os.getenv("QB_USERNAME", "qbuser")
QB_PASSWORD = os.getenv("QB_PASSWORD", "admin123")
OMS_BASE_URL = (os.getenv("OMS_BASE_URL") or "").strip().rstrip("/")
OMS_ACCESS_TOKEN = (os.getenv("OMS_ACCESS_TOKEN") or "").strip()
OMS_PAGE_SIZE = max(1, min(500, int(os.getenv("OMS_PAGE_SIZE", "100"))))
OMS_REQUEST_TIMEOUT = float(os.getenv("OMS_REQUEST_TIMEOUT", "30"))
OMS_SYNC_ON_AUTH = os.getenv("OMS_SYNC_ON_AUTH", "1").strip().lower() in ("1", "true", "yes", "on")
OMS_SYNC_API_KEY = (os.getenv("OMS_SYNC_API_KEY") or "").strip()
OMS_MAX_PAGES = max(1, int(os.getenv("OMS_MAX_PAGES", "500")))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    print("🚀 QB Connector starting…")
    LOG.info(
        "QB Connector starting | OMS configured=%s | sync_on_auth=%s",
        bool(OMS_BASE_URL and OMS_ACCESS_TOKEN),
        OMS_SYNC_ON_AUTH,
    )
    yield
    print("🛑 QB Connector shutting down")
    LOG.info("QB Connector shutting down")


app = FastAPI(lifespan=_lifespan)

# ── In-Memory Store (replace with MySQL in production) ───────────────────────
#
# sessions       : active QBWC sessions keyed by ticket
# job_queue      : pending jobs to process (simulates qb_sync_queue table)
# transaction_map: completed jobs (simulates qb_transaction_map table)
# last_inventory_pull: timestamp of last inventory sync

sessions = {}          # { ticket: { client_id, jobs, index, total } }
transaction_map = {}   # { "customer:email" : listID, "order:k365_id" : txnID }
last_inventory_pull: Optional[datetime] = None

# ── POC Job Queue (simulates MySQL qb_sync_queue) ────────────────────────────
# In production these would come from MySQL.
# For POC we preload test jobs here.
#
# Job structure:
# {
#   "id"           : unique job id
#   "client_id"    : which client this belongs to
#   "operation"    : push_customer | push_order | pull_inventory
#   "priority"     : 1=customer(order_flow) 2=order 3=customer(standalone) 4=inventory
#   "source"       : order_flow | customer_flow | scheduled
#   "status"       : pending | processing | completed | failed | dead
#   "k365_id"      : Kitchen365 record ID
#   "linked_order" : for order_flow customers, the order job id waiting
#   "payload"      : dict with data to push
#   "retry_count"  : number of retries attempted
#   "qb_id"        : QB TxnID or ListID after success
# }

job_queue = [
    # ── Standalone customer (no order) ────────────────────────────────────
    {
        "id": "job_001",
        "client_id": "qbuser",
        "operation": "push_customer",
        "priority": 3,
        "source": "customer_flow",
        "status": "pending",
        "k365_id": "cust_standalone_001",
        "linked_order": None,
        "retry_count": 0,
        "qb_id": None,
        "payload": {
            "name": "Kitchen365 Standalone Customer",
            "company": "Standalone Co",
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane.doe@standalone.com",
            "phone": "9876543211",
            "addr1": "456 Standalone Street",
            "city": "Ahmedabad",
            "state": "GJ",
            "postal": "380002",
            "country": "India"
        }
    },

    # ── Customer needed for order below (order_flow) ───────────────────────
    {
        "id": "job_002",
        "client_id": "qbuser",
        "operation": "push_customer",
        "priority": 1,
        "source": "order_flow",
        "status": "pending",
        "k365_id": "cust_order_flow_001",
        "linked_order": "job_003",
        "retry_count": 0,
        "qb_id": None,
        "payload": {
            "name": "Kitchen365 Order Customer",
            "company": "Order Co",
            "first_name": "Bob",
            "last_name": "Builder",
            "email": "bob.builder@orderco.com",
            "phone": "9876543212",
            "addr1": "789 Order Street",
            "city": "Ahmedabad",
            "state": "GJ",
            "postal": "380003",
            "country": "India"
        }
    },

    # ── Order waiting for customer above ──────────────────────────────────
    {
        "id": "job_003",
        "client_id": "qbuser",
        "operation": "push_order",
        "priority": 2,
        "source": "order_flow",
        "status": "hold",           # ON HOLD until job_002 customer completes
        "k365_id": "order_K365_1001",
        "linked_order": None,
        "retry_count": 0,
        "qb_id": None,
        "payload": {
            "customer_name": "Kitchen365 Order Customer",
            "txn_date": "2026-02-19",
            "po_number": "K365-1001",
            "lines": [
                {
                    "item": "Wood Door:Exterior 1122",
                    "qty": 2,
                    "rate": 120.00
                }
            ]
        }
    },

    # ── Inventory pull (scheduled) ─────────────────────────────────────────
    {
        "id": "job_004",
        "client_id": "qbuser",
        "operation": "pull_inventory",
        "priority": 4,
        "source": "scheduled",
        "status": "pending",
        "k365_id": None,
        "linked_order": None,
        "retry_count": 0,
        "qb_id": None,
        "payload": {}
    }
]


# ── OMS / Magento customer sync ─────────────────────────────────────────────

def _qb_text_escape(value: Any) -> str:
    """Escape text for QBXML character data."""
    return xml_escape(str(value if value is not None else ""), entities={'"': "&quot;", "'": "&apos;"})


def magento_customer_to_payload(customer: dict) -> dict:
    """
    Map Magento REST customer entity to QB CustomerAdd payload fields.
    """
    cid = customer.get("id")
    first = (customer.get("firstname") or "").strip()
    last = (customer.get("lastname") or "").strip()
    email = (customer.get("email") or "").strip()
    company = (customer.get("company") or "").strip()

    addr: dict = {}
    addresses = customer.get("addresses") or []
    for a in addresses:
        if a.get("default_billing"):
            addr = a
            break
    if not addr and addresses:
        addr = addresses[0]

    street = addr.get("street")
    if isinstance(street, list):
        addr1 = (street[0] if street else "") or ""
    else:
        addr1 = str(street or "")
    city = (addr.get("city") or "").strip()
    region = addr.get("region") or {}
    state = (addr.get("region_code") or region.get("region_code") or region.get("code") or "").strip()
    postal = (addr.get("postcode") or "").strip()
    country = (addr.get("country_id") or "").strip()
    phone = (addr.get("telephone") or "").strip()

    display = company or f"{first} {last}".strip() or email or f"Customer-{cid}"
    # QuickBooks display name practical limit
    if len(display) > 41:
        display = display[:38] + "..."

    return {
        "name": display,
        "company": company,
        "first_name": first,
        "last_name": last,
        "email": email,
        "phone": phone,
        "addr1": addr1[:255] if addr1 else "",
        "city": city,
        "state": state,
        "postal": postal,
        "country": country,
    }


def _oms_customer_job_pending_for(k365_id: str) -> bool:
    kid = str(k365_id)
    for j in job_queue:
        if j.get("operation") != "push_customer":
            continue
        if str(j.get("k365_id")) != kid:
            continue
        if j["status"] in ("pending", "processing", "hold", "failed"):
            return True
    return False


def _oms_customer_job_completed_for(k365_id: str) -> bool:
    kid = str(k365_id)
    for j in job_queue:
        if j.get("operation") != "push_customer":
            continue
        if str(j.get("k365_id")) != kid:
            continue
        if j["status"] == "completed":
            return True
    return False


async def fetch_oms_customers_page(
    client: httpx.AsyncClient, page: int
) -> Tuple[List, int]:
    if not OMS_BASE_URL or not OMS_ACCESS_TOKEN:
        raise RuntimeError("OMS_BASE_URL and OMS_ACCESS_TOKEN must be set in environment")
    url = (
        f"{OMS_BASE_URL}/rest/V1/customers/search"
        f"?searchCriteria[sortOrders][0][field]=created_at"
        f"&searchCriteria[sortOrders][0][direction]=DESC"
        f"&searchCriteria[pageSize]={OMS_PAGE_SIZE}"
        f"&searchCriteria[currentPage]={page}"
    )
    headers = {
        "Authorization": f"Bearer {OMS_ACCESS_TOKEN}",
        "Accept": "application/json",
    }
    LOG.debug("OMS GET customers page=%s pageSize=%s", page, OMS_PAGE_SIZE)
    resp = await client.get(url, headers=headers, timeout=OMS_REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    items = list(data.get("items") or [])
    total = int(data.get("total_count") or 0)
    return items, total


async def fetch_all_oms_customers() -> List:
    if not OMS_BASE_URL or not OMS_ACCESS_TOKEN:
        print("⚠️ OMS fetch skipped: OMS_BASE_URL or OMS_ACCESS_TOKEN not set")
        LOG.warning("Skipping OMS fetch: OMS_BASE_URL or OMS_ACCESS_TOKEN not configured")
        return []
    out: List = []
    page = 1
    async with httpx.AsyncClient() as http_client:
        while page <= OMS_MAX_PAGES:
            try:
                items, total = await fetch_oms_customers_page(http_client, page)
            except httpx.HTTPStatusError as e:
                print(
                    f"❌ OMS HTTP error page={page} status={e.response.status_code} body={(e.response.text or '')[:200]}…"
                )
                LOG.error(
                    "OMS customers HTTP error page=%s status=%s body=%s",
                    page,
                    e.response.status_code,
                    (e.response.text or "")[:500],
                )
                raise
            except Exception as e:
                LOG.exception("OMS customers request failed page=%s: %s", page, e)
                raise
            out.extend(items)
            print(f"📥 OMS customers page={page} batch={len(items)} total_so_far={len(out)} total_count={total}")
            LOG.info("OMS customers page=%s fetched=%s total_so_far=%s total_count=%s", page, len(items), len(out), total)
            if not items or len(out) >= total:
                break
            page += 1
        else:
            print(f"❌ OMS fetch stopped: exceeded OMS_MAX_PAGES={OMS_MAX_PAGES}")
            LOG.error("OMS customer fetch stopped: exceeded OMS_MAX_PAGES=%s", OMS_MAX_PAGES)
    return out


async def sync_customers_from_oms(client_id: str) -> dict:
    """
    Pull all customers from OMS search API and enqueue push_customer jobs (deduped).
    """
    summary: dict = {"fetched": 0, "enqueued": 0, "skipped": 0, "errors": []}
    try:
        customers = await fetch_all_oms_customers()
    except Exception as e:
        summary["errors"].append(str(e))
        print(f"❌ sync_customers_from_oms aborted: {e}")
        LOG.error("sync_customers_from_oms aborted: %s", e)
        return summary

    summary["fetched"] = len(customers)
    for c in customers:
        cid = c.get("id")
        if cid is None:
            summary["skipped"] += 1
            LOG.warning("OMS customer missing id, skipping raw keys=%s", list(c.keys())[:10])
            continue
        kid = str(cid)

        if transaction_map.get(f"customer:{kid}"):
            summary["skipped"] += 1
            LOG.debug("Skip enqueue: customer %s already in transaction_map", kid)
            continue
        if _oms_customer_job_pending_for(kid) or _oms_customer_job_completed_for(kid):
            summary["skipped"] += 1
            LOG.debug("Skip enqueue: customer %s already in job_queue", kid)
            continue

        payload = magento_customer_to_payload(c)
        job = {
            "id": f"oms_cust_{kid}",
            "client_id": client_id,
            "operation": "push_customer",
            "priority": 3,
            "source": "oms_api",
            "status": "pending",
            "k365_id": kid,
            "linked_order": None,
            "retry_count": 0,
            "qb_id": None,
            "payload": payload,
        }
        job_queue.append(job)
        summary["enqueued"] += 1
        LOG.info(
            "Enqueued push_customer from OMS k365_id=%s email=%s name=%s",
            kid,
            payload.get("email") or "(no email)",
            payload.get("name") or "",
        )
    print(
        f"✅ OMS customer sync done | fetched={summary['fetched']} enqueued={summary['enqueued']} skipped={summary['skipped']}"
    )
    LOG.info(
        "OMS customer sync done | fetched=%s enqueued=%s skipped=%s",
        summary["fetched"],
        summary["enqueued"],
        summary["skipped"],
    )
    return summary


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_job(job_id: str):
    return next((j for j in job_queue if j["id"] == job_id), None)

def update_job(job_id: str, **kwargs):
    job = get_job(job_id)
    if job:
        job.update(kwargs)

def get_next_jobs_for_client(client_id: str, max_jobs: int = 5):
    """
    Pick next batch of jobs for this client in priority order.
    Respects:
    - HOLD status (order waiting for customer)
    - Priority order (1=customer_order_flow first)
    - Only pending status
    """
    pending = [
        j for j in job_queue
        if j["client_id"] == client_id
        and j["status"] == "pending"
    ]
    # Sort by priority ascending (1 = highest priority)
    pending.sort(key=lambda x: x["priority"])
    return pending[:max_jobs]

def resolve_dependencies(completed_job_id: str):
    """
    When a customer job completes, unblock any orders waiting for it.
    """
    for job in job_queue:
        if job.get("linked_order") == completed_job_id:
            # This is not right — linked_order points FROM customer TO order
            pass
    # Find orders that were waiting for this customer
    for job in job_queue:
        if job["status"] == "hold":
            # Find the customer job this order depends on
            customer_job = next(
                (j for j in job_queue
                 if j.get("linked_order") == job["id"]
                 and j["operation"] == "push_customer"),
                None
            )
            if customer_job and customer_job["status"] == "completed":
                print(f"🔓 Unblocking order job {job['id']} — customer is ready")
                LOG.info("Unblocking order job %s — customer job is completed", job["id"])
                job["status"] = "pending"

def build_customer_xml(payload: dict, request_id: str = "1") -> str:
    rid = _qb_text_escape(request_id)
    n = _qb_text_escape(payload.get("name", ""))
    co = _qb_text_escape(payload.get("company", ""))
    fn = _qb_text_escape(payload.get("first_name", ""))
    ln = _qb_text_escape(payload.get("last_name", ""))
    a1 = _qb_text_escape(payload.get("addr1", ""))
    city = _qb_text_escape(payload.get("city", ""))
    st = _qb_text_escape(payload.get("state", ""))
    zipc = _qb_text_escape(payload.get("postal", ""))
    ctry = _qb_text_escape(payload.get("country", ""))
    ph = _qb_text_escape(payload.get("phone", ""))
    em = _qb_text_escape(payload.get("email", ""))
    return f"""<?xml version="1.0" ?><?qbxml version="13.0"?><QBXML><QBXMLMsgsRq onError="stopOnError"><CustomerAddRq requestID="{rid}"><CustomerAdd><Name>{n}</Name><CompanyName>{co}</CompanyName><FirstName>{fn}</FirstName><LastName>{ln}</LastName><BillAddress><Addr1>{a1}</Addr1><City>{city}</City><State>{st}</State><PostalCode>{zipc}</PostalCode><Country>{ctry}</Country></BillAddress><Phone>{ph}</Phone><Email>{em}</Email></CustomerAdd></CustomerAddRq></QBXMLMsgsRq></QBXML>"""

def build_order_xml(payload: dict, request_id: str = "1") -> str:
    lines_xml = ""
    for line in payload.get("lines", []):
        lines_xml += f"<SalesOrderLineAdd><ItemRef><FullName>{line['item']}</FullName></ItemRef><Quantity>{line['qty']}</Quantity><Rate>{line['rate']}</Rate></SalesOrderLineAdd>"
    return f"""<?xml version="1.0" ?><?qbxml version="13.0"?><QBXML><QBXMLMsgsRq onError="stopOnError"><SalesOrderAddRq requestID="{request_id}"><SalesOrderAdd><CustomerRef><FullName>{payload['customer_name']}</FullName></CustomerRef><TxnDate>{payload['txn_date']}</TxnDate><PONumber>{payload['po_number']}</PONumber>{lines_xml}</SalesOrderAdd></SalesOrderAddRq></QBXMLMsgsRq></QBXML>"""

def build_inventory_xml(request_id: str = "1") -> str:
    return f"""<?xml version="1.0" ?><?qbxml version="13.0"?><QBXML><QBXMLMsgsRq onError="stopOnError"><ItemInventoryQueryRq requestID="{request_id}"><ActiveStatus>ActiveOnly</ActiveStatus></ItemInventoryQueryRq></QBXMLMsgsRq></QBXML>"""

def soap_envelope(method: str, inner: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <{method}Response xmlns="http://developer.intuit.com/">
      <{method}Result>
        {inner}
      </{method}Result>
    </{method}Response>
  </soap:Body>
</soap:Envelope>"""

def send_request_response(qbxml: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <sendRequestXMLResponse xmlns="http://developer.intuit.com/">
      <sendRequestXMLResult><![CDATA[{qbxml}]]></sendRequestXMLResult>
    </sendRequestXMLResponse>
  </soap:Body>
</soap:Envelope>"""

def receive_response(progress: int) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <receiveResponseXMLResponse xmlns="http://developer.intuit.com/">
      <receiveResponseXMLResult>{progress}</receiveResponseXMLResult>
    </receiveResponseXMLResponse>
  </soap:Body>
</soap:Envelope>"""


# ── Status endpoint (simple dashboard) ──────────────────────────────────────
@app.get("/status")
async def status():
    return {
        "queue": [
            {
                "id": j["id"],
                "operation": j["operation"],
                "status": j["status"],
                "priority": j["priority"],
                "source": j["source"],
                "k365_id": j["k365_id"],
                "qb_id": j["qb_id"],
                "retry_count": j["retry_count"]
            }
            for j in job_queue
        ],
        "transaction_map": transaction_map,
        "active_sessions": list(sessions.keys()),
        "last_inventory_pull": str(last_inventory_pull) if last_inventory_pull else "never"
    }

@app.get("/")
async def root():
    return PlainTextResponse("QB Connector Running")

@app.get("/qbwc")
async def qbwc_get():
    return PlainTextResponse("QB Connector Service Ready")


@app.post("/api/sync/customers")
async def api_sync_customers(request: Request):
    """
    Pull customers from OMS into the local job queue (same mapping as QBWC auth sync).
    Set OMS_SYNC_API_KEY in env and send header X-Sync-Key on public hosts.
    """
    if OMS_SYNC_API_KEY:
        provided = request.headers.get("X-Sync-Key") or request.headers.get("x-sync-key")
        if provided != OMS_SYNC_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid or missing X-Sync-Key")
    summary = await sync_customers_from_oms(QB_USERNAME)
    print(
        f"🔄 Manual /api/sync/customers | fetched={summary.get('fetched')} enqueued={summary.get('enqueued')} skipped={summary.get('skipped')}"
    )
    LOG.info("Manual /api/sync/customers | fetched=%s enqueued=%s skipped=%s", summary.get("fetched"), summary.get("enqueued"), summary.get("skipped"))
    return summary


# ── Main QBWC SOAP Handler ───────────────────────────────────────────────────
@app.post("/qbwc")
async def qbwc_handler(request: Request):
    body = await request.body()
    body_str = body.decode("utf-8")
    print("\n" + "=" * 60)
    print("📥 Received:", body_str[:200])
    LOG.debug("QBWC request (first 500 chars): %s", body_str[:500])

    # ── serverVersion ──────────────────────────────────────────
    if "serverVersion" in body_str:
        print("📌 serverVersion")
        LOG.debug("SOAP serverVersion")
        xml = soap_envelope("serverVersion", "<serverVersionRet>1.0</serverVersionRet>")

    # ── clientVersion ──────────────────────────────────────────
    elif "clientVersion" in body_str:
        print("📌 clientVersion")
        LOG.debug("SOAP clientVersion")
        xml = soap_envelope("clientVersion", "<clientVersionRet></clientVersionRet>")

    # ── authenticate ───────────────────────────────────────────
    elif "authenticate" in body_str:
        u_match = re.search(r'<strUserName>(.*?)</strUserName>', body_str)
        p_match = re.search(r'<strPassword>(.*?)</strPassword>', body_str)
        u = u_match.group(1) if u_match else ""
        p = p_match.group(1) if p_match else ""
        print(f"🔐 Auth attempt — user: {u}")
        LOG.info("QBWC authenticate attempt user=%s", u)

        if u == QB_USERNAME and p == QB_PASSWORD:
            ticket = str(uuid.uuid4())

            if OMS_SYNC_ON_AUTH:
                try:
                    oms_summary = await sync_customers_from_oms(u)
                    print(
                        f"🔄 OMS sync on auth | fetched={oms_summary.get('fetched')} "
                        f"enqueued={oms_summary.get('enqueued')} skipped={oms_summary.get('skipped')} "
                        f"errors={oms_summary.get('errors')}"
                    )
                    LOG.info(
                        "OMS customer sync on auth | fetched=%s enqueued=%s skipped=%s errors=%s",
                        oms_summary.get("fetched"),
                        oms_summary.get("enqueued"),
                        oms_summary.get("skipped"),
                        oms_summary.get("errors"),
                    )
                except Exception as e:
                    print(f"❌ OMS customer sync on auth failed (continuing): {e}")
                    LOG.exception("OMS customer sync on auth failed (continuing with existing queue): %s", e)

            # Load next batch of jobs for this client
            jobs = get_next_jobs_for_client(u, max_jobs=5)

            if jobs:
                sessions[ticket] = {
                    "client_id": u,
                    "jobs": jobs,
                    "index": 0,
                    "total": len(jobs)
                }
                print(f"✅ Auth success — ticket: {ticket[:8]}... — {len(jobs)} jobs queued")
                LOG.info("Auth success ticket=%s... jobs_queued=%s", ticket[:8], len(jobs))
                for j in jobs:
                    print(f"   📋 {j['id']} | {j['operation']} | priority {j['priority']}")
                    LOG.info("  job %s | %s | priority %s", j["id"], j["operation"], j["priority"])
            else:
                sessions[ticket] = {
                    "client_id": u,
                    "jobs": [],
                    "index": 0,
                    "total": 0
                }
                print(f"✅ Auth success — no pending jobs (ticket {ticket[:8]}…)")
                LOG.info("Auth success ticket=%s... no pending jobs", ticket[:8])

            xml = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <authenticateResponse xmlns="http://developer.intuit.com/">
      <authenticateResult>
        <string>{ticket}</string>
        <string></string>
      </authenticateResult>
    </authenticateResponse>
  </soap:Body>
</soap:Envelope>"""
        else:
            print("❌ Auth failed")
            LOG.warning("Auth failed for user=%s", u)
            xml = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <authenticateResponse xmlns="http://developer.intuit.com/">
      <authenticateResult>
        <string>nvu</string>
        <string>nvu</string>
      </authenticateResult>
    </authenticateResponse>
  </soap:Body>
</soap:Envelope>"""

    # ── sendRequestXML ─────────────────────────────────────────
    elif "sendRequestXML" in body_str:
        ticket_match = re.search(r'<ticket>(.*?)</ticket>', body_str)
        ticket = ticket_match.group(1) if ticket_match else ""
        session = sessions.get(ticket)

        if not session or session["index"] >= session["total"]:
            # No jobs — tell QBWC nothing to do
            print("📭 No jobs — returning empty")
            LOG.info("sendRequestXML: no jobs for ticket, returning empty")
            xml = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <sendRequestXMLResponse xmlns="http://developer.intuit.com/">
      <sendRequestXMLResult></sendRequestXMLResult>
    </sendRequestXMLResponse>
  </soap:Body>
</soap:Envelope>"""
        else:
            job = session["jobs"][session["index"]]
            update_job(job["id"], status="processing")
            print(f"📤 Processing job {job['id']} | {job['operation']} | priority {job['priority']}")
            LOG.info("Processing job %s | %s | priority %s", job["id"], job["operation"], job["priority"])

            if job["operation"] == "push_customer":
                qbxml = build_customer_xml(job["payload"], request_id=job["id"])
                print(f"👤 Pushing customer: {job['payload']['name']}")
                LOG.info("Pushing customer name=%s k365_id=%s", job["payload"].get("name"), job.get("k365_id"))

            elif job["operation"] == "push_order":
                qbxml = build_order_xml(job["payload"], request_id=job["id"])
                print(f"🛒 Pushing order: {job['payload']['po_number']}")
                LOG.info("Pushing order po=%s", job["payload"].get("po_number"))

            elif job["operation"] == "pull_inventory":
                qbxml = build_inventory_xml(request_id=job["id"])
                print("📦 Pulling inventory")
                LOG.info("Pulling inventory")

            else:
                qbxml = ""
                print(f"❓ Unknown operation: {job['operation']}")
                LOG.warning("Unknown operation: %s", job["operation"])

            xml = send_request_response(qbxml)

    # ── receiveResponseXML ─────────────────────────────────────
    elif "receiveResponseXML" in body_str:
        ticket_match = re.search(r'<ticket>(.*?)</ticket>', body_str)
        ticket = ticket_match.group(1) if ticket_match else ""
        session = sessions.get(ticket)

        print("📩 Received response from QB!")
        LOG.info("receiveResponseXML from QB")

        # Parse response
        response_match = re.search(
            r'<strHCPResponse>(.*?)</strHCPResponse>', body_str, re.DOTALL
        )
        if not response_match:
            response_match = re.search(
                r'<response>(.*?)</response>', body_str, re.DOTALL
            )

        if response_match and session:
            raw = html.unescape(response_match.group(1))
            job = session["jobs"][session["index"]]

            # ── Check status ──────────────────────────────────
            status_code = re.search(r'<statusCode>(.*?)</statusCode>', raw)
            status_msg  = re.search(r'<statusMessage>(.*?)</statusMessage>', raw)
            status_sev  = re.search(r'<statusSeverity>(.*?)</statusSeverity>', raw)

            code = status_code.group(1) if status_code else "0"
            sev  = status_sev.group(1) if status_sev else "Info"
            msg  = status_msg.group(1) if status_msg else ""
            print(f"📋 QB Status: {code} | {sev} | {msg}")
            LOG.info("QB response statusCode=%s severity=%s message=%s", code, sev, msg)

            # ── Handle by operation ───────────────────────────
            if job["operation"] == "push_customer":
                list_id = re.search(r'<ListID>(.*?)</ListID>', raw)
                name    = re.search(r'<FullName>(.*?)</FullName>', raw)

                if list_id:
                    qb_list_id = list_id.group(1)
                    update_job(job["id"], status="completed", qb_id=qb_list_id)
                    transaction_map[f"customer:{job['k365_id']}"] = qb_list_id
                    print(f"✅ Customer created! ListID: {qb_list_id}")
                    print(f"👤 QB Name: {name.group(1) if name else 'N/A'}")
                    LOG.info(
                        "Customer created ListID=%s QB_Name=%s",
                        qb_list_id,
                        name.group(1) if name else "N/A",
                    )
                    # Unblock any orders waiting for this customer
                    resolve_dependencies(job["id"])
                elif code == "3100":
                    # Already exists — not a real error for us
                    print("⚠️ Customer already exists in QB — marking completed")
                    LOG.warning("Customer already exists in QB (code 3100) — marking completed job=%s", job["id"])
                    update_job(job["id"], status="completed")
                else:
                    print(f"❌ Customer push failed: {msg}")
                    LOG.error("Customer push failed job=%s message=%s", job["id"], msg)
                    retry = job["retry_count"] + 1
                    new_status = "dead" if retry >= 3 else "failed"
                    update_job(job["id"], status=new_status, retry_count=retry)

            elif job["operation"] == "push_order":
                txn_id  = re.search(r'<TxnID>(.*?)</TxnID>', raw)
                ref_num = re.search(r'<RefNumber>(.*?)</RefNumber>', raw)

                if txn_id:
                    qb_txn_id = txn_id.group(1)
                    update_job(job["id"], status="completed", qb_id=qb_txn_id)
                    transaction_map[f"order:{job['k365_id']}"] = qb_txn_id
                    print(f"✅ Order created! TxnID: {qb_txn_id}")
                    print(f"📝 RefNumber: {ref_num.group(1) if ref_num else 'N/A'}")
                    LOG.info("Order created TxnID=%s RefNumber=%s", qb_txn_id, ref_num.group(1) if ref_num else "N/A")
                else:
                    print(f"❌ Order push failed: {msg}")
                    LOG.error("Order push failed job=%s message=%s", job["id"], msg)
                    retry = job["retry_count"] + 1
                    new_status = "dead" if retry >= 3 else "failed"
                    update_job(job["id"], status=new_status, retry_count=retry)

            elif job["operation"] == "pull_inventory":
                global last_inventory_pull
                items = re.findall(
                    r'<ItemInventoryRet>(.*?)</ItemInventoryRet>', raw, re.DOTALL
                )
                print(f"✅ Found {len(items)} inventory items")
                LOG.info("Inventory query returned %s items", len(items))
                for item in items:
                    name  = re.search(r'<FullName>(.*?)</FullName>', item)
                    price = re.search(r'<SalesPrice>(.*?)</SalesPrice>', item)
                    cost  = re.search(r'<PurchaseCost>(.*?)</PurchaseCost>', item)
                    qty   = re.search(r'<QuantityOnHand>(.*?)</QuantityOnHand>', item)
                    print(
                        f"   📦 {name.group(1) if name else 'N/A'} | "
                        f"Price: {price.group(1) if price else 'N/A'} | "
                        f"Cost: {cost.group(1) if cost else 'N/A'} | "
                        f"Qty: {qty.group(1) if qty else 'N/A'}"
                    )
                    LOG.debug(
                        "  item=%s price=%s cost=%s qty=%s",
                        name.group(1) if name else "N/A",
                        price.group(1) if price else "N/A",
                        cost.group(1) if cost else "N/A",
                        qty.group(1) if qty else "N/A",
                    )
                last_inventory_pull = datetime.now()
                update_job(job["id"], status="completed")

            # ── Advance session index ─────────────────────────
            session["index"] += 1
            remaining = session["total"] - session["index"]

            if remaining > 0:
                progress = int((session["index"] / session["total"]) * 100)
                print(f"⏳ {remaining} jobs remaining — progress: {progress}%")
                LOG.info("%s jobs remaining progress=%s%%", remaining, progress)
            else:
                progress = 100
                print("🏁 All jobs complete — closing session")
                LOG.info("All jobs complete — closing session")
                sessions.pop(ticket, None)

        else:
            progress = 100
            print("⚠️ Could not parse response or no session found")
            LOG.warning("Could not parse QB response or no session (ticket=%s)", ticket[:8] if ticket else "")

        xml = receive_response(progress)

    # ── getLastError ───────────────────────────────────────────
    elif "getLastError" in body_str:
        print("⚠️ getLastError called")
        LOG.warning("getLastError called")
        xml = soap_envelope("getLastError", "<getLastErrorResult></getLastErrorResult>")

    # ── closeConnection ────────────────────────────────────────
    elif "closeConnection" in body_str:
        ticket_match = re.search(r'<ticket>(.*?)</ticket>', body_str)
        ticket = ticket_match.group(1) if ticket_match else ""
        sessions.pop(ticket, None)
        print("🔒 Session closed")
        LOG.info("Session closed")
        xml = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <closeConnectionResponse xmlns="http://developer.intuit.com/">
      <closeConnectionResult>OK</closeConnectionResult>
    </closeConnectionResponse>
  </soap:Body>
</soap:Envelope>"""

    # ── unknown ────────────────────────────────────────────────
    else:
        print("❓ Unknown:", body_str[:200])
        LOG.warning("Unknown SOAP action (truncated): %s", body_str[:200])
        xml = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body/>
</soap:Envelope>"""

    return Response(content=xml, media_type="text/xml; charset=utf-8")