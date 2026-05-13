from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse
from twilio.rest import Client as TwilioClient

from config import PORT, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_OTC_NUMBER, TWILIO_LP_NUMBER
from agent import handle_message, handle_lp_response
import lp_comms

app = FastAPI()

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _twiml(message: str) -> PlainTextResponse:
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{message}</Message>
</Response>"""
    return PlainTextResponse(content=content, media_type="application/xml")


def _empty_twiml() -> PlainTextResponse:
    return PlainTextResponse(
        content='<?xml version="1.0" encoding="UTF-8"?><Response/>',
        media_type="application/xml",
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Twilio #1 webhook - receives client messages AND LP rate responses
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def webhook(
    From: str = Form(...),
    Body: str = Form(...),
):
    from_number = From.replace("whatsapp:", "")

    # LP rate response arriving from Twilio #2
    if from_number == TWILIO_LP_NUMBER:
        result = handle_lp_response(Body)
        if result:
            client_phone, quote = result
            twilio_client.messages.create(
                from_=f"whatsapp:{TWILIO_OTC_NUMBER}",
                to=f"whatsapp:{client_phone}",
                body=quote,
            )
        return _empty_twiml()

    # Client message
    reply = handle_message(phone_number=from_number, message=Body)
    return _twiml(reply) if reply else _empty_twiml()


# ---------------------------------------------------------------------------
# Twilio #2 webhook - LP simulator, auto-replies with mock rates
# ---------------------------------------------------------------------------

@app.post("/lp-webhook")
async def lp_webhook(
    From: str = Form(...),
    Body: str = Form(...),
):
    lp_reply = lp_comms.generate_lp_reply(Body)
    return _twiml(lp_reply)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
