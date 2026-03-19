from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import Response, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from contextlib import asynccontextmanager
import asyncio
import re
import uuid
import html
import os
import xml.sax.saxutils as saxutils
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
QB_USERNAME = os.getenv("QB_USERNAME", "qbuser")
QB_PASSWORD = os.getenv("QB_PASSWORD", "admin123")
OMS_BASE_URL = (os.getenv("OMS_BASE_URL") or "").rstrip("/")
OMS_ACCESS_TOKEN = os.getenv("OMS_ACCESS_TOKEN") or ""
OMS_PAGE_SIZE = int(os.getenv("OMS_PAGE_SIZE", "100"))
OMS_REQUEST_TIMEOUT = int(os.getenv("OMS_REQUEST_TIMEOUT", "30"))
OMS_SYNC_ON_STARTUP = os.getenv("OMS_SYNC_ON_STARTUP", "false").lower() in ("1", "true", "yes")

QB_CUSTOMER_NAME_MAX_LEN = 41
QBWC_MAX_JOBS_PER_SESSION = int(os.getenv("QBWC_MAX_JOBS_PER_SESSION", "10"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if OMS_SYNC_ON_STARTUP and OMS_BASE_URL and OMS_ACCESS_TOKEN:
        print("OMS_SYNC_ON_STARTUP enabled; loading customers from OMS into queue")
        try:
            await asyncio.to_thread(sync_customers_from_oms, QB_USERNAME)
        except Exception:
            print("Startup OMS customer sync failed")
    elif OMS_SYNC_ON_STARTUP:
        print("OMS_SYNC_ON_STARTUP set but OMS_BASE_URL/OMS_ACCESS_TOKEN missing; skipping")
    yield


app = FastAPI(lifespan=lifespan)
http_basic = HTTPBasic(auto_error=False)

# ── In-Memory Store (replace with MySQL in production) ───────────────────────
#
# sessions       : active QBWC sessions keyed by ticket
# job_queue      : pending jobs to process (simulates qb_sync_queue table)
# transaction_map: completed jobs (simulates qb_transaction_map table)
# last_inventory_pull: timestamp of last inventory sync

sessions = {}          # { ticket: { client_id, jobs, index, total } }
transaction_map = {}   # { "customer:email" : listID, "order:k365_id" : txnID }
last_inventory_pull: Optional[datetime] = None
# QBWC calls getLastError after failures; empty getLastErrorResult causes "GetLastError failed" in the UI.
qbwc_last_error: str = ""

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


# ── OMS / Magento customer API → QB job queue ───────────────────────────────


def escape_qbxml_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    return saxutils.escape(str(value), entities={'"': "&quot;", "'": "&apos;"})


def verify_sync_credentials(credentials: Optional[HTTPBasicCredentials] = Depends(http_basic)):
    if credentials is None or credentials.username != QB_USERNAME or credentials.password != QB_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid or missing Basic auth")
    return credentials.username


def fetch_all_oms_customers() -> List[Dict[str, Any]]:
    """
    GET /rest/V1/customers/search with pagination.
    Magento defaults pageSize to ~20 if omitted — without paging, only the first page was synced.
    """
    if not OMS_BASE_URL or not OMS_ACCESS_TOKEN:
        print("OMS_BASE_URL or OMS_ACCESS_TOKEN is empty; cannot call customers/search")
        raise RuntimeError("OMS customer API is not configured (check .env)")

    headers = {
        "Authorization": f"Bearer {OMS_ACCESS_TOKEN}",
        "Accept": "application/json",
    }
    url = f"{OMS_BASE_URL}/rest/V1/customers/search"
    all_items: List[Dict[str, Any]] = []
    page = 1
    total_count: Optional[int] = None

    while True:
        params = {
            "searchCriteria[pageSize]": OMS_PAGE_SIZE,
            "searchCriteria[currentPage]": page,
            "searchCriteria[sortOrders][0][field]": "created_at",
            "searchCriteria[sortOrders][0][direction]": "DESC",
        }
        print(
            f"OMS customers/search page={page} pageSize={OMS_PAGE_SIZE} "
            f"(accumulated={len(all_items)})"
        )
        try:
            resp = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=OMS_REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"OMS customers/search network error: {e}")
            raise

        if resp.status_code != 200:
            print(
                f"OMS customers/search HTTP {resp.status_code} "
                f"body_snippet={repr((resp.text or '')[:500])}"
            )
            raise RuntimeError(f"OMS customers/search failed: HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError:
            print(
                f"OMS customers/search invalid JSON body_snippet="
                f"{repr((resp.text or '')[:300])}"
            )
            raise

        batch = data.get("items") or []
        if total_count is None:
            total_count = data.get("total_count")
        all_items.extend(batch)
        print(
            f"OMS customers/search page={page} batch={len(batch)} "
            f"total_count={total_count} accumulated={len(all_items)}"
        )

        if not batch:
            break
        if len(batch) < OMS_PAGE_SIZE:
            break
        if total_count is not None and len(all_items) >= total_count:
            break
        page += 1

    return all_items


def _pick_billing_address(customer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    addresses = customer.get("addresses") or []
    if not addresses:
        return None
    default_billing_id = customer.get("default_billing")
    if default_billing_id is not None:
        for a in addresses:
            if a.get("id") == default_billing_id:
                return a
    for a in addresses:
        if a.get("default_billing"):
            return a
    return addresses[0]


def magento_customer_to_job_payload(customer: Dict[str, Any]) -> Tuple[dict, str]:
    """Map Magento REST customer → same payload keys as static push_customer jobs."""
    cid = customer.get("id")
    if cid is None:
        raise ValueError("customer missing id")
    k365_id = str(cid)

    email = (customer.get("email") or "").strip()
    first = (customer.get("firstname") or "").strip()
    last = (customer.get("lastname") or "").strip()

    company = ""
    addr1 = ""
    city = ""
    state = ""
    postal = ""
    country = ""
    phone = ""

    addr = _pick_billing_address(customer)
    if addr:
        company = (addr.get("company") or "").strip()
        street = addr.get("street")
        if isinstance(street, list):
            parts = [s for s in street if s]
            addr1 = ", ".join(parts) if parts else ""
        else:
            addr1 = str(street or "").strip()
        city = (addr.get("city") or "").strip()
        region = addr.get("region") or {}
        if isinstance(region, dict):
            state = (region.get("region_code") or region.get("region") or "").strip()
        else:
            state = str(region or "").strip()
        if not state:
            state = (addr.get("region_code") or "").strip()
        postal = (addr.get("postcode") or "").strip()
        country = (addr.get("country_id") or "").strip()
        phone = (addr.get("telephone") or "").strip()

    display_name = f"{first} {last}".strip() or (email.split("@")[0] if email else "") or f"Customer-{k365_id}"
    if company and company.lower() not in display_name.lower():
        display_name = f"{company} — {display_name}"
    display_name = display_name[:QB_CUSTOMER_NAME_MAX_LEN]

    payload = {
        "name": display_name,
        "company": company[:100] if company else "",
        "first_name": first[:50],
        "last_name": last[:50],
        "email": email[:100],
        "phone": phone[:30],
        "addr1": addr1[:200],
        "city": city[:50],
        "state": state[:50],
        "postal": postal[:20],
        "country": (country[:50] if country else "US"),
    }
    return payload, k365_id


def _can_enqueue_customer_job(k365_id: str) -> bool:
    """False if already in QB or an active/completed queue job exists; drop dead/failed to allow retry."""
    if transaction_map.get(f"customer:{k365_id}"):
        return False

    to_drop: List[int] = []
    for idx, j in enumerate(job_queue):
        if j.get("operation") != "push_customer" or str(j.get("k365_id")) != str(k365_id):
            continue
        st = j.get("status")
        if st in ("pending", "processing", "hold", "completed"):
            return False
        if st in ("failed", "dead"):
            to_drop.append(idx)

    for idx in reversed(to_drop):
        dropped = job_queue.pop(idx)
        print(f"Removed prior {dropped.get('status')} customer job id={dropped.get('id')} k365_id={k365_id} for re-queue from OMS")
    return True


def sync_customers_from_oms(client_id: str) -> Dict[str, Any]:
    """
    Fetch all customers from OMS and append push_customer jobs (priority 3, source oms_api).
    Matches existing job payload fields for QB CustomerAdd.
    """
    stats: Dict[str, Any] = {
        "fetched": 0,
        "queued": 0,
        "skipped": 0,
        "mapping_errors": 0,
        "ok": True,
        "error": None,
    }
    try:
        customers = fetch_all_oms_customers()
    except Exception as e:
        stats["ok"] = False
        stats["error"] = str(e)
        print(f"OMS customer fetch failed: {e}")
        return stats

    stats["fetched"] = len(customers)
    for c in customers:
        try:
            payload, k365_id = magento_customer_to_job_payload(c)
        except Exception as e:
            stats["mapping_errors"] += 1
            print(f"Skip customer id={c.get('id')}: mapping error: {e}")
            continue

        if not _can_enqueue_customer_job(k365_id):
            stats["skipped"] += 1
            continue

        job_id = f"oms_customer_{k365_id}"
        job_queue.append(
            {
                "id": job_id,
                "client_id": client_id,
                "operation": "push_customer",
                "priority": 3,
                "source": "oms_api",
                "status": "pending",
                "k365_id": k365_id,
                "linked_order": None,
                "retry_count": 0,
                "qb_id": None,
                "payload": payload,
            }
        )
        stats["queued"] += 1
        print(f"Queued push_customer from OMS job_id={job_id} k365_id={k365_id} email={payload.get('email')} qb_name={payload.get('name')!r}")

    print(f"OMS→QB queue sync done: fetched={stats['fetched']} queued={stats['queued']} skipped={stats['skipped']} mapping_errors={stats['mapping_errors']}")
    return stats


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
                job["status"] = "pending"

def build_customer_xml(payload: dict, request_id: str = "1") -> str:
    rid = escape_qbxml_text(request_id)
    return f"""<?xml version="1.0" ?><?qbxml version="13.0"?><QBXML><QBXMLMsgsRq onError="stopOnError"><CustomerAddRq requestID="{rid}"><CustomerAdd><Name>{escape_qbxml_text(payload.get('name'))}</Name><CompanyName>{escape_qbxml_text(payload.get('company',''))}</CompanyName><FirstName>{escape_qbxml_text(payload.get('first_name',''))}</FirstName><LastName>{escape_qbxml_text(payload.get('last_name',''))}</LastName><BillAddress><Addr1>{escape_qbxml_text(payload.get('addr1',''))}</Addr1><City>{escape_qbxml_text(payload.get('city',''))}</City><State>{escape_qbxml_text(payload.get('state',''))}</State><PostalCode>{escape_qbxml_text(payload.get('postal',''))}</PostalCode><Country>{escape_qbxml_text(payload.get('country',''))}</Country></BillAddress><Phone>{escape_qbxml_text(payload.get('phone',''))}</Phone><Email>{escape_qbxml_text(payload.get('email',''))}</Email></CustomerAdd></CustomerAddRq></QBXMLMsgsRq></QBXML>"""

def build_order_xml(payload: dict, request_id: str = "1") -> str:
    rid = escape_qbxml_text(request_id)
    lines_xml = ""
    for line in payload.get("lines", []):
        item_name = escape_qbxml_text(line.get("item", ""))
        qty = line.get("qty", 0)
        rate = line.get("rate", 0)
        lines_xml += (
            f"<SalesOrderLineAdd><ItemRef><FullName>{item_name}</FullName></ItemRef>"
            f"<Quantity>{qty}</Quantity><Rate>{rate}</Rate></SalesOrderLineAdd>"
        )
    cust = escape_qbxml_text(payload.get("customer_name", ""))
    txn = escape_qbxml_text(payload.get("txn_date", ""))
    po = escape_qbxml_text(payload.get("po_number", ""))
    return f"""<?xml version="1.0" ?><?qbxml version="13.0"?><QBXML><QBXMLMsgsRq onError="stopOnError"><SalesOrderAddRq requestID="{rid}"><SalesOrderAdd><CustomerRef><FullName>{cust}</FullName></CustomerRef><TxnDate>{txn}</TxnDate><PONumber>{po}</PONumber>{lines_xml}</SalesOrderAdd></SalesOrderAddRq></QBXMLMsgsRq></QBXML>"""

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
@app.post("/sync/customers-from-oms")
async def sync_customers_from_oms_endpoint(_user: str = Depends(verify_sync_credentials)):
    """
    Pull all customers from OMS (Magento customers/search, created_at DESC) and queue push_customer jobs.
    Use HTTP Basic auth: same QB_USERNAME / QB_PASSWORD as QuickBooks Web Connector.
    """
    stats = await asyncio.to_thread(sync_customers_from_oms, QB_USERNAME)
    if not stats.get("ok"):
        raise HTTPException(status_code=502, detail=stats)
    return stats


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


# ── Main QBWC SOAP Handler ───────────────────────────────────────────────────
@app.post("/qbwc")
async def qbwc_handler(request: Request):
    body = await request.body()
    body_str = body.decode("utf-8")
    print("\n" + "="*60)
    print("📥 Received:", body_str[:200])

    # ── serverVersion ──────────────────────────────────────────
    if "serverVersion" in body_str:
        print("📌 serverVersion")
        xml = soap_envelope("serverVersion", "<serverVersionRet>1.0</serverVersionRet>")

    # ── clientVersion ──────────────────────────────────────────
    elif "clientVersion" in body_str:
        print("📌 clientVersion")
        xml = soap_envelope("clientVersion", "<clientVersionRet></clientVersionRet>")

    # ── authenticate ───────────────────────────────────────────
    elif "authenticate" in body_str:
        u_match = re.search(r'<strUserName>(.*?)</strUserName>', body_str)
        p_match = re.search(r'<strPassword>(.*?)</strPassword>', body_str)
        u = u_match.group(1) if u_match else ""
        p = p_match.group(1) if p_match else ""
        print(f"🔐 Auth attempt — user: {u}")

        if u == QB_USERNAME and p == QB_PASSWORD:
            ticket = str(uuid.uuid4())

            # Load next batch of jobs for this client
            jobs = get_next_jobs_for_client(u, max_jobs=QBWC_MAX_JOBS_PER_SESSION)

            if jobs:
                sessions[ticket] = {
                    "client_id": u,
                    "jobs": jobs,
                    "index": 0,
                    "total": len(jobs)
                }
                print(f"✅ Auth success — ticket: {ticket[:8]}... — {len(jobs)} jobs queued")
                for j in jobs:
                    print(f"   📋 {j['id']} | {j['operation']} | priority {j['priority']}")
            else:
                sessions[ticket] = {
                    "client_id": u,
                    "jobs": [],
                    "index": 0,
                    "total": 0
                }
                print(f"✅ Auth success — no pending jobs")

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

            if job["operation"] == "push_customer":
                qbxml = build_customer_xml(job["payload"], request_id=job["id"])
                print(f"👤 Pushing customer: {job['payload']['name']}")

            elif job["operation"] == "push_order":
                qbxml = build_order_xml(job["payload"], request_id=job["id"])
                print(f"🛒 Pushing order: {job['payload']['po_number']}")

            elif job["operation"] == "pull_inventory":
                qbxml = build_inventory_xml(request_id=job["id"])
                print(f"📦 Pulling inventory")

            else:
                qbxml = ""
                print(f"❓ Unknown operation: {job['operation']}")

            xml = send_request_response(qbxml)

    # ── receiveResponseXML ─────────────────────────────────────
    elif "receiveResponseXML" in body_str:
        global qbwc_last_error
        progress = 100
        ticket_match = re.search(r'<ticket>(.*?)</ticket>', body_str)
        ticket = ticket_match.group(1) if ticket_match else ""
        session = sessions.get(ticket)

        print("📩 Received response from QB!")

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

            # ── Handle by operation ───────────────────────────
            if job["operation"] == "push_customer":
                # ListID appears in CustomerRet (success) or nested in response
                list_id = re.search(r'<ListID>([^<]+)</ListID>', raw)
                name    = re.search(r'<FullName>([^<]+)</FullName>', raw)

                if list_id:
                    qb_list_id = list_id.group(1).strip()
                    update_job(job["id"], status="completed", qb_id=qb_list_id)
                    transaction_map[f"customer:{job['k365_id']}"] = qb_list_id
                    qbwc_last_error = ""
                    print(f"✅ Customer created! ListID: {qb_list_id}")
                    print(f"👤 QB Name: {name.group(1) if name else 'N/A'}")
                    # Unblock any orders waiting for this customer
                    resolve_dependencies(job["id"])
                elif code == "3100" or (msg and "already exists" in msg.lower()):
                    # Duplicate name / already exists — do not retry forever
                    print(f"⚠️ Customer already exists in QB — marking completed")
                    update_job(job["id"], status="completed")
                    qbwc_last_error = ""
                else:
                    err_detail = msg or raw[:500] or "Unknown QB customer error"
                    qbwc_last_error = f"CustomerAdd failed (code={code}): {err_detail}"
                    print(f"❌ Customer push failed: {msg}")
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
                    qbwc_last_error = ""
                    print(f"✅ Order created! TxnID: {qb_txn_id}")
                    print(f"📝 RefNumber: {ref_num.group(1) if ref_num else 'N/A'}")
                else:
                    qbwc_last_error = f"SalesOrderAdd failed (code={code}): {msg or raw[:500]}"
                    print(f"❌ Order push failed: {msg}")
                    retry = job["retry_count"] + 1
                    new_status = "dead" if retry >= 3 else "failed"
                    update_job(job["id"], status=new_status, retry_count=retry)

            elif job["operation"] == "pull_inventory":
                global last_inventory_pull
                items = re.findall(
                    r'<ItemInventoryRet>(.*?)</ItemInventoryRet>', raw, re.DOTALL
                )
                print(f"✅ Found {len(items)} inventory items")
                for item in items:
                    name  = re.search(r'<FullName>(.*?)</FullName>', item)
                    price = re.search(r'<SalesPrice>(.*?)</SalesPrice>', item)
                    cost  = re.search(r'<PurchaseCost>(.*?)</PurchaseCost>', item)
                    qty   = re.search(r'<QuantityOnHand>(.*?)</QuantityOnHand>', item)
                    print(f"   📦 {name.group(1) if name else 'N/A'} | "
                          f"Price: {price.group(1) if price else 'N/A'} | "
                          f"Cost: {cost.group(1) if cost else 'N/A'} | "
                          f"Qty: {qty.group(1) if qty else 'N/A'}")
                last_inventory_pull = datetime.now()
                update_job(job["id"], status="completed")
                qbwc_last_error = ""

            # ── Advance session index ─────────────────────────
            session["index"] += 1
            remaining = session["total"] - session["index"]

            if remaining > 0:
                progress = int((session["index"] / session["total"]) * 100)
                print(f"⏳ {remaining} jobs remaining — progress: {progress}%")
            else:
                progress = 100
                print(f"🏁 All jobs complete — closing session")
                sessions.pop(ticket, None)

        else:
            # Avoid stuck session: QBWC still expects progress; empty getLastError breaks UI
            if session and session["index"] < session["total"]:
                job = session["jobs"][session["index"]]
                qbwc_last_error = (
                    "Could not parse QB XML response (strHCPResponse/response missing or empty). "
                    "Check QBWC log and server logs."
                )
                print(f"⚠️ {qbwc_last_error}")
                retry = job["retry_count"] + 1
                new_status = "dead" if retry >= 3 else "failed"
                update_job(job["id"], status=new_status, retry_count=retry)
                session["index"] += 1
                remaining = session["total"] - session["index"]
                progress = (
                    int((session["index"] / session["total"]) * 100)
                    if session["total"]
                    else 100
                )
                if remaining <= 0:
                    progress = 100
                    sessions.pop(ticket, None)
            else:
                progress = 100
                if not response_match:
                    qbwc_last_error = "receiveResponseXML: no response payload matched"
                elif not session:
                    qbwc_last_error = "receiveResponseXML: unknown or expired ticket"
                print("⚠️ Could not parse response or no session found")

        xml = receive_response(progress)

    # ── getLastError ───────────────────────────────────────────
    elif "getLastError" in body_str:
        # QBWC requires a non-empty human-readable string; empty element causes "GetLastError failed"
        err = (qbwc_last_error or "No error").strip()
        if len(err) > 2000:
            err = err[:1997] + "..."
        safe_err = escape_qbxml_text(err)
        print(f"⚠️ getLastError called → {err[:200]!r}")
        xml = soap_envelope("getLastError", f"<getLastErrorResult>{safe_err}</getLastErrorResult>")
        qbwc_last_error = ""

    # ── closeConnection ────────────────────────────────────────
    elif "closeConnection" in body_str:
        ticket_match = re.search(r'<ticket>(.*?)</ticket>', body_str)
        ticket = ticket_match.group(1) if ticket_match else ""
        sessions.pop(ticket, None)
        print("🔒 Session closed")
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
        xml = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body/>
</soap:Envelope>"""

    return Response(content=xml, media_type="text/xml; charset=utf-8")