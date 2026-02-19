from fastapi import FastAPI, Request
from fastapi.responses import Response, PlainTextResponse
import re

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
def soap_wrap(method: str, inner: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <{method}Response xmlns="http://developer.intuit.com/qbxml/qbwebconnector">
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
    print("📥 Received:", body_str[:500])

    # ── serverVersion ─────────────────────────────
    if "serverVersion" in body_str:
        print("📌 serverVersion request")
        xml = soap_wrap("serverVersion", "1.0")

    # ── clientVersion ─────────────────────────────
    elif "clientVersion" in body_str:
        print("📌 clientVersion request")
        # Empty string = OK, proceed
        xml = soap_wrap("clientVersion", "")

    # ── authenticate ──────────────────────────────
    elif "authenticate" in body_str:
        username = re.search(r'<strUserName>(.*?)</strUserName>', body_str)
        password = re.search(r'<strPassword>(.*?)</strPassword>', body_str)

        u = username.group(1) if username else ""
        p = password.group(1) if password else ""
        print(f"🔐 Auth attempt — user: {u}")

        if u == QB_USERNAME and p == QB_PASSWORD:
            print("✅ Auth success")
            # Empty string = use currently open company file
            inner = "<string></string><string></string>"
        else:
            print("❌ Auth failed — wrong credentials")
            inner = "<string>nvu</string><string></string>"

        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <authenticateResponse xmlns="http://developer.intuit.com/qbxml/qbwebconnector">
      <authenticateResult>
        {inner}
      </authenticateResult>
    </authenticateResponse>
  </soap:Body>
</soap:Envelope>"""

    # ── sendRequestXML ────────────────────────────
    elif "sendRequestXML" in body_str:
        print("📤 Sending CompanyQuery to QB")
        qbxml = """<?xml version="1.0" ?>
<?qbxml version="13.0"?>
<QBXML>
  <QBXMLMsgsRq onError="stopOnError">
    <CompanyQueryRq requestID="1"/>
  </QBXMLMsgsRq>
</QBXML>"""
        xml = soap_wrap("sendRequestXML", qbxml)

    # ── receiveResponseXML ────────────────────────
    elif "receiveResponseXML" in body_str:
        print("📩 Received response from QB:")
        # Extract and print the response data
        response_data = re.search(r'<response>(.*?)</response>', body_str, re.DOTALL)
        if response_data:
            print(response_data.group(1))
        print("✅ Sync complete!")
        xml = soap_wrap("receiveResponseXML", "100")

    # ── getLastError ──────────────────────────────
    elif "getLastError" in body_str:
        print("⚠️ QB requested last error")
        xml = soap_wrap("getLastError", "")

    # ── closeConnection ───────────────────────────
    elif "closeConnection" in body_str:
        print("🔒 Session closed")
        xml = soap_wrap("closeConnection", "OK")

    # ── unknown ───────────────────────────────────
    else:
        print("❓ Unknown request:", body_str[:300])
        xml = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body/>
</soap:Envelope>"""

    return Response(content=xml, media_type="text/xml; charset=utf-8")