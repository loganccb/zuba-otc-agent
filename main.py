from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse
from twilio.rest import Client as TwilioClient
from apscheduler.schedulers.background import BackgroundScheduler

from config import PORT, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_OTC_NUMBER, TWILIO_LP_NUMBER
from agent import handle_message, handle_lp_response, approve_trade, reject_trade, cancel_expired_trades
import lp_comms
import slack

app = FastAPI()

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
scheduler = BackgroundScheduler()


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
# Scheduler: proactive rate expiry cancellation
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def start_scheduler():
    def _check_and_cancel():
        for phone_number, msg in cancel_expired_trades():
            twilio_client.messages.create(
                from_=f"whatsapp:{TWILIO_OTC_NUMBER}",
                to=f"whatsapp:{phone_number}",
                body=msg,
            )
    scheduler.add_job(_check_and_cancel, "interval", seconds=30)
    scheduler.start()


@app.on_event("shutdown")
async def stop_scheduler():
    scheduler.shutdown()


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
# Compliance review: approve / reject endpoints
# Tolu or Ali hits these URLs from the Slack notification to release or reject a trade.
# TODO: add secret token parameter before going to production.
# TODO: switch Slack notifications from DM to #trading channel when ready.
# ---------------------------------------------------------------------------

REJECTION_MESSAGE = (
    "We're unable to proceed with this trade at the moment. "
    "A member of our team will be in touch with you directly."
)


@app.get("/trade/approve")
async def trade_approve(id: str):
    result = approve_trade(id)
    if not result:
        return PlainTextResponse("Trade not found or already processed.", status_code=404)
    client_phone, quote = result
    twilio_client.messages.create(
        from_=f"whatsapp:{TWILIO_OTC_NUMBER}",
        to=f"whatsapp:{client_phone}",
        body=quote,
    )
    slack._post(f":white_check_mark: [{id}] Approved - quote sent to client.")
    return PlainTextResponse(f"Approved. Quote sent to {client_phone}.")


@app.get("/trade/reject")
async def trade_reject(id: str):
    client_phone = reject_trade(id)
    if not client_phone:
        return PlainTextResponse("Trade not found or already processed.", status_code=404)
    twilio_client.messages.create(
        from_=f"whatsapp:{TWILIO_OTC_NUMBER}",
        to=f"whatsapp:{client_phone}",
        body=REJECTION_MESSAGE,
    )
    slack._post(f":x: [{id}] Rejected - client notified to expect follow-up.")
    return PlainTextResponse(f"Rejected. Client at {client_phone} notified.")


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
