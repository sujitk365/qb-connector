from fastapi import FastAPI, Request
from fastapi.responses import Response, PlainTextResponse

app = FastAPI()

# THIS IS THE KEY FIX — QBWC does a GET first to verify the endpoint
@app.get("/qbwc")
async def qbwc_get():
    return PlainTextResponse("QuickBooks Web Connector Service")

@app.get("/")
async def root():
    return PlainTextResponse("QB Connector Running")

SOAP_RESPONSE = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <{method}Response xmlns="http://developer.intuit.com/qbxml/qbwebconnector">
      <{method}Result>{result}</{method}Result>
    </{method}Response>
  </soap:Body>
</soap:Envelope>"""

@app.post("/qbwc")
async def qbwc_handler(request: Request):
    body = await request.body()
    body_str = body.decode("utf-8")
    print("📥 Received:", body_str[:200])

    if "authenticate" in body_str:
        xml = SOAP_RESPONSE.format(method="authenticate", result="<string></string><string></string>")
    elif "sendRequestXML" in body_str:
        qbxml = """<?xml version="1.0" ?>
<?qbxml version="13.0"?>
<QBXML>
  <QBXMLMsgsRq onError="stopOnError">
    <CompanyQueryRq requestID="1"/>
  </QBXMLMsgsRq>
</QBXML>"""
        xml = SOAP_RESPONSE.format(method="sendRequestXML", result=qbxml)
    elif "receiveResponseXML" in body_str:
        xml = SOAP_RESPONSE.format(method="receiveResponseXML", result="100")
        print("✅ Got response from QB!")
    elif "closeConnection" in body_str:
        xml = SOAP_RESPONSE.format(method="closeConnection", result="OK")
    elif "getLastError" in body_str:
        xml = SOAP_RESPONSE.format(method="getLastError", result="")
    else:
        xml = "<soap:Envelope/>"

    return Response(content=xml, media_type="text/xml")