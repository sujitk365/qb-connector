from fastapi import FastAPI, Request
from fastapi.responses import Response, PlainTextResponse
import re
import uuid

app = FastAPI()

# ── Config ──────────────────────────────────────────
QB_USERNAME = "qbuser"
QB_PASSWORD = "admin123"

# ── Health check endpoints ───────────────────────────
@app.get("/")
async def root():
    return PlainTextResponse("QB Connector Running")

@app.get("/qbwc")
async def qbwc_get():
    return PlainTextResponse("QB Connector Service Ready")

# ── SOAP Helper ──────────────────────────────────────
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

# ── Main QBWC SOAP Handler ───────────────────────────
@app.post("/qbwc")
async def qbwc_handler(request: Request):
    body = await request.body()
    body_str = body.decode("utf-8")
    print("📥 Received:", body_str[:300])

    # ── serverVersion ─────────────────────────────
    if "serverVersion" in body_str:
        print("📌 serverVersion request")
        xml = soap_envelope("serverVersion", "<serverVersionRet>1.0</serverVersionRet>")

    # ── clientVersion ─────────────────────────────
    elif "clientVersion" in body_str:
        print("📌 clientVersion request")
        xml = soap_envelope("clientVersion", "<clientVersionRet></clientVersionRet>")

    # ── authenticate ──────────────────────────────
    elif "authenticate" in body_str:
        username = re.search(r'<strUserName>(.*?)</strUserName>', body_str)
        password = re.search(r'<strPassword>(.*?)</strPassword>', body_str)

        u = username.group(1) if username else ""
        p = password.group(1) if password else ""
        print(f"🔐 Auth attempt — user: {u}")

        if u == QB_USERNAME and p == QB_PASSWORD:
            print("✅ Auth success")
            session_ticket = str(uuid.uuid4())
            xml = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <authenticateResponse xmlns="http://developer.intuit.com/">
      <authenticateResult>
        <string>{session_ticket}</string>
        <string></string>
      </authenticateResult>
    </authenticateResponse>
  </soap:Body>
</soap:Envelope>"""
        else:
            print("❌ Auth failed — wrong credentials")
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

    # ── sendRequestXML ────────────────────────────
    elif "sendRequestXML" in body_str:
        print("📤 Sending CompanyQuery to QB")
        qbxml = """<?xml version="1.0" ?><?qbxml version="13.0"?><QBXML><QBXMLMsgsRq onError="stopOnError"><ItemInventoryQueryRq requestID="1"><ActiveStatus>ActiveOnly</ActiveStatus></ItemInventoryQueryRq></QBXMLMsgsRq></QBXML>"""
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <sendRequestXMLResponse xmlns="http://developer.intuit.com/">
      <sendRequestXMLResult><![CDATA[{qbxml}]]></sendRequestXMLResult>
    </sendRequestXMLResponse>
  </soap:Body>
</soap:Envelope>"""

    # ── receiveResponseXML ────────────────────────
    elif "receiveResponseXML" in body_str:
        print("📩 Received inventory data from QB!")
        
        # Extract the response parameter
        response_match = re.search(
            r'<strHCPResponse>(.*?)</strHCPResponse>', 
            body_str, re.DOTALL
        )
        if not response_match:
            response_match = re.search(
                r'<response>(.*?)</response>', 
                body_str, re.DOTALL
            )
        
        if response_match:
            import html
            raw = html.unescape(response_match.group(1))
            print("📊 Decoded XML:")
            print(raw)
            
            # Parse inventory items
            items = re.findall(
                r'<ItemInventoryRet>(.*?)</ItemInventoryRet>', 
                raw, re.DOTALL
            )
            print(f"✅ Found {len(items)} inventory items")
            
            for item in items:
                list_id = re.search(r'<ListID>(.*?)</ListID>', item)
                name = re.search(r'<FullName>(.*?)</FullName>', item)
                price = re.search(r'<SalesPrice>(.*?)</SalesPrice>', item)
                cost = re.search(r'<PurchaseCost>(.*?)</PurchaseCost>', item)
                qty = re.search(r'<QuantityOnHand>(.*?)</QuantityOnHand>', item)
                
                print(f"📦 Item: {name.group(1) if name else 'N/A'} | "
                    f"Price: {price.group(1) if price else 'N/A'} | "
                    f"Cost: {cost.group(1) if cost else 'N/A'} | "
                    f"Qty: {qty.group(1) if qty else 'N/A'}")
        
        xml = """<?xml version="1.0" encoding="utf-8"?>
    <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                xmlns:xsd="http://www.w3.org/2001/XMLSchema">
    <soap:Body>
        <receiveResponseXMLResponse xmlns="http://developer.intuit.com/">
        <receiveResponseXMLResult>100</receiveResponseXMLResult>
        </receiveResponseXMLResponse>
    </soap:Body>
    </soap:Envelope>"""

    # ── getLastError ──────────────────────────────
    elif "getLastError" in body_str:
        print("⚠️ getLastError called")
        xml = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <getLastErrorResponse xmlns="http://developer.intuit.com/">
      <getLastErrorResult></getLastErrorResult>
    </getLastErrorResponse>
  </soap:Body>
</soap:Envelope>"""

    # ── closeConnection ───────────────────────────
    elif "closeConnection" in body_str:
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

    # ── unknown ───────────────────────────────────
    else:
        print("❓ Unknown request:", body_str[:300])
        xml = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body/>
</soap:Envelope>"""

    return Response(content=xml, media_type="text/xml; charset=utf-8")