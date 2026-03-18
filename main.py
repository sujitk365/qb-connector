import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response, PlainTextResponse
import re
import uuid
import html
from datetime import datetime
from typing import Optional, List, Any

app = FastAPI()

# ── Config ──────────────────────────────────────────────────────────────────
QB_USERNAME = os.environ.get("QB_USERNAME", "qbuser")
QB_PASSWORD = os.environ.get("QB_PASSWORD", "admin123")

# Magento OMS (when qb-connector calls Magento REST)
OMS_BASE_URL = os.environ.get("OMS_BASE_URL", "https://oms.kitchen365test.com")
OMS_BEARER_TOKEN = os.environ.get("OMS_BEARER_TOKEN", "tqe1crbqwqdqlxocd9j07d1x2etbat08")

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


# ── Customer push REST API (separate file) ────────────────────────────────────
from customer_push import get_customer_router

app.include_router(get_customer_router(job_queue, transaction_map, OMS_BASE_URL, OMS_BEARER_TOKEN))

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