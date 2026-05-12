import os
from dotenv import load_dotenv

load_dotenv()

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_OTC_NUMBER  = os.getenv("TWILIO_OTC_NUMBER")   # Twilio #1 - OTC desk
TWILIO_LP_NUMBER   = os.getenv("TWILIO_LP_NUMBER")    # Twilio #2 - LP simulator

# Slack
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

# Server
PORT = int(os.getenv("PORT", 8000))  # Railway sets PORT automatically
