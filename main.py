from fastapi import FastAPI, Request
from fastapi.responses import Response, PlainTextResponse, JSONResponse
import re
import uuid
import html
import os
import logging
import requests
from datetime import datetime
from typing import Optional, Tuple
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# ── Logging ─────────────────────────────────────────────────────────────────
# Local: set LOG_LEVEL=DEBUG once to see request URL, total_count, etc.
_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(level=_level, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(_level)
LOG_PREFIX = "[OMS_SYNC]"

# ── Config ──────────────────────────────────────────────────────────────────
QB_USERNAME = os.environ.get("QB_USERNAME", "qbuser")
QB_PASSWORD = os.environ.get("QB_PASSWORD", "admin123")
OMS_BASE_URL = (os.environ.get("OMS_BASE_URL") or "https://oms.kitchen365test.com").rstrip("/")
OMS_ACCESS_TOKEN = (os.environ.get("OMS_ACCESS_TOKEN") or "").strip()
OMS_PAGE_SIZE = int(os.environ.get("OMS_PAGE_SIZE", "100"))
OMS_REQUEST_TIMEOUT = int(os.environ.get("OMS_REQUEST_TIMEOUT", "30"))

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


# ── OMS customer fetch and sync ──────────────────────────────────────────────

def _oms_headers() -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if OMS_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {OMS_ACCESS_TOKEN}"
    return headers


def magento_customer_to_payload(customer: dict) -> Optional[dict]:
    """Map Magento customer object to payload expected by build_customer_xml."""
    try:
        first = (customer.get("firstname") or "").strip()
        last = (customer.get("lastname") or "").strip()
        email = (customer.get("email") or "").strip()
        if not email:
            logger.error("%s [CUSTOMER_FETCH] Skipping customer with missing email: id=%s", LOG_PREFIX, customer.get("id"))
            return None
        addresses = customer.get("addresses") or []
        default_billing = customer.get("default_billing")
        addr = None
        if default_billing and isinstance(addresses, list):
            for a in addresses:
                if isinstance(a, dict) and str(a.get("id")) == str(default_billing):
                    addr = a
                    break
        if not addr and addresses:
            addr = addresses[0] if isinstance(addresses[0], dict) else None
        street = ""
        city = ""
        state = ""
        postal = ""
        country = ""
        company = ""
        phone = ""
        if addr:
            street_list = addr.get("street")
            if isinstance(street_list, list):
                street = (street_list[0] or "").strip() if street_list else ""
            elif isinstance(street_list, str):
                street = street_list.strip()
            city = (addr.get("city") or "").strip()
            region = addr.get("region")
            if isinstance(region, dict):
                state = (region.get("region_code") or region.get("region") or "").strip()
            elif isinstance(region, str):
                state = region.strip()
            postal = (addr.get("postcode") or "").strip()
            country_id = (addr.get("country_id") or "").strip()
            country = country_id
            company = (addr.get("company") or "").strip()
            phone = (addr.get("telephone") or "").strip()
        name = f"{first} {last}".strip() or email
        if not name:
            name = email
        name = f"Kitchen365 {name}" if name else f"Kitchen365 {email}"
        return {
            "name": name,
            "company": company,
            "first_name": first,
            "last_name": last,
            "email": email,
            "phone": phone,
            "addr1": street,
            "city": city,
            "state": state,
            "postal": postal,
            "country": country,
        }
    except Exception as e:
        logger.exception("%s [CUSTOMER_FETCH] Failed to map customer id=%s: %s", LOG_PREFIX, customer.get("id"), e)
        return None


def fetch_all_oms_customers() -> list:
    """Fetch all customers from OMS. Sort params: field + direction; paginate with pageSize/currentPage to get all (e.g. 372)."""
    url_base = f"{OMS_BASE_URL}/rest/V1/customers/search"
    all_items = []
    page = 1
    total_count = None
    if not OMS_ACCESS_TOKEN:
        logger.warning("%s [CUSTOMER_FETCH] OMS_ACCESS_TOKEN not set; request will likely get 401. Set env OMS_ACCESS_TOKEN to your Bearer token.", LOG_PREFIX)
    logger.info("%s [CUSTOMER_FETCH] Starting OMS customer fetch from %s", LOG_PREFIX, OMS_BASE_URL)
    while True:
        params = {
            "searchCriteria[sortOrders][0][field]": "created_at",
            "searchCriteria[sortOrders][0][direction]": "DESC",
            "searchCriteria[pageSize]": OMS_PAGE_SIZE,
            "searchCriteria[currentPage]": page,
        }
        try:
            resp = requests.get(
                url_base,
                params=params,
                headers=_oms_headers(),
                timeout=OMS_REQUEST_TIMEOUT,
            )
            logger.debug("%s [CUSTOMER_FETCH] Request URL: %s", LOG_PREFIX, resp.url)
        except requests.RequestException as e:
            logger.error("%s [CUSTOMER_FETCH] HTTP request failed (page=%s): %s", LOG_PREFIX, page, e)
            break
        if resp.status_code != 200:
            body_snippet = (resp.text or "")[:500]
            logger.error("%s [CUSTOMER_FETCH] HTTP %s from OMS (page=%s). Body: %s", LOG_PREFIX, resp.status_code, page, body_snippet)
            if resp.status_code == 401:
                logger.error("%s [CUSTOMER_FETCH] Set OMS_ACCESS_TOKEN to an integration token with Magento_Customer::customer access.", LOG_PREFIX)
            break
        try:
            data = resp.json()
        except ValueError as e:
            logger.error("%s [CUSTOMER_FETCH] JSON decode error (page=%s): %s. Body snippet: %s", LOG_PREFIX, page, e, (resp.text or "")[:300])
            break
        items = data.get("items")
        if not isinstance(items, list):
            logger.error("%s [CUSTOMER_FETCH] Response missing or invalid 'items' (page=%s)", LOG_PREFIX, page)
            break
        if total_count is None and "total_count" in data:
            total_count = data.get("total_count")
            logger.info("%s [CUSTOMER_FETCH] total_count from API: %s", LOG_PREFIX, total_count)
        logger.info("%s [CUSTOMER_FETCH] Page %s: got %s items (total so far: %s)", LOG_PREFIX, page, len(items), len(all_items) + len(items))
        all_items.extend(items)
        if not items:
            break
        if total_count is not None and len(all_items) >= total_count:
            break
        page += 1
    logger.info("%s [CUSTOMER_FETCH] Finished OMS customer fetch. Total fetched: %s", LOG_PREFIX, len(all_items))
    return all_items


def sync_oms_customers_to_queue(client_id: str) -> Tuple[int, int]:
    """
    Fetch all OMS customers, map to push_customer jobs, and append to job_queue for those not already
    in transaction_map or already pending in queue. Returns (added_count, skipped_count).
    """
    raw_customers = fetch_all_oms_customers()
    existing_pending_k365 = {j["k365_id"] for j in job_queue if j.get("k365_id") and j["status"] == "pending"}
    added = 0
    skipped = 0
    for customer in raw_customers:
        cid = customer.get("id")
        if cid is None:
            logger.debug("%s [CUSTOMER_FETCH] Skipping customer with no id", LOG_PREFIX)
            skipped += 1
            continue
        k365_id = str(cid)
        if transaction_map.get(f"customer:{k365_id}"):
            skipped += 1
            continue
        if k365_id in existing_pending_k365:
            skipped += 1
            continue
        payload = magento_customer_to_payload(customer)
        if not payload:
            skipped += 1
            continue
        job_id = f"job_oms_{k365_id}"
        job_queue.append({
            "id": job_id,
            "client_id": client_id,
            "operation": "push_customer",
            "priority": 3,
            "source": "customer_flow",
            "status": "pending",
            "k365_id": k365_id,
            "linked_order": None,
            "retry_count": 0,
            "qb_id": None,
            "payload": payload,
        })
        existing_pending_k365.add(k365_id)
        added += 1
    logger.info("%s [CUSTOMER_FETCH] Sync complete: jobs added=%s, skipped=%s", LOG_PREFIX, added, skipped)
    return added, skipped


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
    return f"""<?xml version="1.0" ?><?qbxml version="13.0"?><QBXML><QBXMLMsgsRq onError="stopOnError"><CustomerAddRq requestID="{request_id}"><CustomerAdd><Name>{payload['name']}</Name><CompanyName>{payload.get('company','')}</CompanyName><FirstName>{payload.get('first_name','')}</FirstName><LastName>{payload.get('last_name','')}</LastName><BillAddress><Addr1>{payload.get('addr1','')}</Addr1><City>{payload.get('city','')}</City><State>{payload.get('state','')}</State><PostalCode>{payload.get('postal','')}</PostalCode><Country>{payload.get('country','')}</Country></BillAddress><Phone>{payload.get('phone','')}</Phone><Email>{payload.get('email','')}</Email></CustomerAdd></CustomerAddRq></QBXMLMsgsRq></QBXML>"""

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


@app.post("/sync/customers")
async def sync_customers():
    """Manual trigger: fetch OMS customers and merge push_customer jobs into the queue."""
    try:
        added, skipped = sync_oms_customers_to_queue(QB_USERNAME)
        return {"ok": True, "jobs_added": added, "skipped": skipped}
    except Exception as e:
        logger.exception("%s [CUSTOMER_FETCH] Manual sync failed: %s", LOG_PREFIX, e)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


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

            # Sync OMS customers into job queue (Option A: on every authenticate)
            try:
                sync_oms_customers_to_queue(u)
            except Exception as e:
                logger.exception("%s [CUSTOMER_FETCH] Sync failed during authenticate: %s", LOG_PREFIX, e)

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
                list_id = re.search(r'<ListID>(.*?)</ListID>', raw)
                name    = re.search(r'<FullName>(.*?)</FullName>', raw)

                if list_id:
                    qb_list_id = list_id.group(1)
                    update_job(job["id"], status="completed", qb_id=qb_list_id)
                    transaction_map[f"customer:{job['k365_id']}"] = qb_list_id
                    print(f"✅ Customer created! ListID: {qb_list_id}")
                    print(f"👤 QB Name: {name.group(1) if name else 'N/A'}")
                    # Unblock any orders waiting for this customer
                    resolve_dependencies(job["id"])
                elif code == "3100":
                    # Already exists — not a real error for us
                    print(f"⚠️ Customer already exists in QB — marking completed")
                    update_job(job["id"], status="completed")
                else:
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
                    print(f"✅ Order created! TxnID: {qb_txn_id}")
                    print(f"📝 RefNumber: {ref_num.group(1) if ref_num else 'N/A'}")
                else:
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
            progress = 100
            print("⚠️ Could not parse response or no session found")

        xml = receive_response(progress)

    # ── getLastError ───────────────────────────────────────────
    elif "getLastError" in body_str:
        print("⚠️ getLastError called")
        xml = soap_envelope("getLastError", "<getLastErrorResult></getLastErrorResult>")

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