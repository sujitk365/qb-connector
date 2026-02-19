from fastapi import FastAPI, Request
from fastapi.responses import Response, PlainTextResponse

app = FastAPI()

@app.get("/")
async def root():
    return PlainTextResponse("QB Connector Running")

@app.get("/qbwc")
async def qbwc_get():
    return PlainTextResponse("QB Connector Service Ready")

@app.post("/qbwc")
async def qbwc_handler(request: Request):
    body = await request.body()
    body_str = body.decode("utf-8")
    print("📥 Received:", body_str[:500])

    if "authenticate" in body_str:
        xml = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <authenticateResponse xmlns="http://developer.intuit.com/qbxml/qbwebconnector">
      <authenticateResult>
        <string></string>
        <string></string>
      </authenticateResult>
    </authenticateResponse>
  </soap:Body>
</soap:Envelope>"""

    elif "sendRequestXML" in body_str:
        qbxml = """<?xml version="1.0" ?>
<?qbxml version="13.0"?>
<QBXML>
  <QBXMLMsgsRq onError="stopOnError">
    <CompanyQueryRq requestID="1"/>
  </QBXMLMsgsRq>
</QBXML>"""
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <sendRequestXMLResponse xmlns="http://developer.intuit.com/qbxml/qbwebconnector">
      <sendRequestXMLResult>{qbxml}</sendRequestXMLResult>
    </sendRequestXMLResponse>
  </soap:Body>
</soap:Envelope>"""

    elif "receiveResponseXML" in body_str:
        xml = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <receiveResponseXMLResponse xmlns="http://developer.intuit.com/qbxml/qbwebconnector">
      <receiveResponseXMLResult>100</receiveResponseXMLResult>
    </receiveResponseXMLResponse>
  </soap:Body>
</soap:Envelope>"""
        print("✅ QB responded with data!")

    elif "closeConnection" in body_str:
        xml = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <closeConnectionResponse xmlns="http://developer.intuit.com/qbxml/qbwebconnector">
      <closeConnectionResult>OK</closeConnectionResult>
    </closeConnectionResponse>
  </soap:Body>
</soap:Envelope>"""

    elif "getLastError" in body_str:
        xml = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <soap:Body>
    <getLastErrorResponse xmlns="http://developer.intuit.com/qbxml/qbwebconnector">
      <getLastErrorResult></getLastErrorResult>
    </getLastErrorResponse>
  </soap:Body>
</soap:Envelope>"""

    else:
        print("⚠️ Unknown request:", body_str[:200])
        xml = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body/>
</soap:Envelope>"""

    return Response(content=xml, media_type="text/xml; charset=utf-8")