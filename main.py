from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse
import os

from config import PORT
from agent import handle_message

app = FastAPI()


# Health check - visit your Railway URL to confirm the server is running
@app.get("/health")
def health():
    return {"status": "ok"}


# Twilio sends every incoming WhatsApp message to this endpoint
@app.post("/webhook")
async def webhook(
    From: str = Form(...),   # sender's WhatsApp number, e.g. whatsapp:+447...
    Body: str = Form(...),   # the message text
):
    # Strip the "whatsapp:" prefix Twilio adds to phone numbers
    phone_number = From.replace("whatsapp:", "")

    # Pass to the agent and get a reply
    reply = handle_message(phone_number=phone_number, message=Body)

    # Wrap reply in TwiML format - this is what Twilio expects back
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{reply}</Message>
</Response>"""

    return PlainTextResponse(content=twiml, media_type="application/xml")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
