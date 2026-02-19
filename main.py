from fastapi import FastAPI, Request
from fastapi.responses import Response, PlainTextResponse
import re
import uuid
import html

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
        print("📤 Sending SalesOrder to QB")
        qbxml = """<?xml version="1.0" ?><?qbxml version="13.0"?><QBXML><QBXMLMsgsRq onError="stopOnError"><SalesOrderAddRq requestID="1"><SalesOrderAdd><CustomerRef><FullName>Abercrombie, Kristy</FullName></CustomerRef><TxnDate>2026-02-19</TxnDate><PONumber>K365-TEST-001</PONumber><SalesOrderLineAdd><ItemRef><FullName>Wood Door:Exterior 1122</FullName></ItemRef><Quantity>2</Quantity><Rate>120.00</Rate></SalesOrderLineAdd></SalesOrderAdd></SalesOrderAddRq></QBXMLMsgsRq></QBXML>"""
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
        print("📩 Received response from QB!")

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
            raw = html.unescape(response_match.group(1))

            # Check status
            status_code = re.search(r'<statusCode>(.*?)</statusCode>', raw)
            status_msg = re.search(r'<statusMessage>(.*?)</statusMessage>', raw)
            status_sev = re.search(r'<statusSeverity>(.*?)</statusSeverity>', raw)

            if status_code:
                print(f"📋 Status: {status_code.group(1)} | "
                      f"Severity: {status_sev.group(1) if status_sev else 'N/A'} | "
                      f"Message: {status_msg.group(1) if status_msg else 'N/A'}")

            # Extract SalesOrder details
            txn_id = re.search(r'<TxnID>(.*?)</TxnID>', raw)
            ref_num = re.search(r'<RefNumber>(.*?)</RefNumber>', raw)
            customer = re.search(r'<FullName>(.*?)</FullName>', raw)

            if txn_id:
                print(f"✅ SalesOrder created in QB!")
                print(f"🎫 TxnID: {txn_id.group(1)}")
                print(f"📝 RefNumber: {ref_num.group(1) if ref_num else 'N/A'}")
                print(f"👤 Customer: {customer.group(1) if customer else 'N/A'}")
            else:
                print("⚠️ No TxnID found — possible error")
                print("📊 Raw response:", raw[:500])
        else:
            print("⚠️ Could not extract response body")
            print("📊 Full body:", body_str[300:800])

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