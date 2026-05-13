"""
lp_comms.py - LP communication layer

Mock phase (current):
  - request_rate() sends a WhatsApp message from Twilio #1 to LP's WhatsApp number
  - Twilio #2's /lp-webhook auto-replies; generate_lp_reply() powers that response
  - parse_lp_response() processes LP replies arriving back at /webhook on Twilio #1
  - request_negotiation() remains synchronous mock (no LP WhatsApp thread needed yet)

Live phase (future):
  - Replace Twilio #2 entries in LP_REGISTRY with real LP numbers
  - Adapt parse_lp_response() to match LP's actual message format
  - Make request_negotiation() async like request_rate()
"""

from __future__ import annotations

import re
import logging
from typing import Optional

from twilio.rest import Client as TwilioClient

from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_OTC_NUMBER, TWILIO_LP_NUMBER
from rates import MOCK_RATES
from pricing import FLOOR_BPS

logger = logging.getLogger(__name__)

# Lazy Twilio client - only initialised if credentials are present
_twilio: Optional[TwilioClient] = None


def _get_twilio() -> Optional[TwilioClient]:
    global _twilio
    if _twilio is None and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        _twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio


# ---------------------------------------------------------------------------
# LP registry
# In mock phase, all LPs route to Twilio #2. In live phase, replace
# TWILIO_LP_NUMBER with each LP's real WhatsApp number.
# ---------------------------------------------------------------------------

LP_REGISTRY: dict[str, dict] = {
    "Yeba": {
        "whatsapp":               TWILIO_LP_NUMBER,
        "pairs":                  ["USD/NGN", "USDT/NGN", "CAD/NGN"],
        "negotiation_buffer_bps": 20,
    },
    "Muva": {
        "whatsapp":               TWILIO_LP_NUMBER,
        "pairs":                  ["USDC/NGN"],
        "negotiation_buffer_bps": 15,
    },
    "One Liquidity": {
        "whatsapp":               TWILIO_LP_NUMBER,
        "pairs":                  ["GBP/NGN", "EUR/NGN"],
        "negotiation_buffer_bps": 15,
    },
    "Emergent": {
        "whatsapp":               TWILIO_LP_NUMBER,
        "pairs":                  ["USD/GHS", "USDC/GHS", "USDT/GHS", "GBP/GHS", "EUR/GHS"],
        "negotiation_buffer_bps": 20,
    },
    "Kora Pay": {
        "whatsapp":               TWILIO_LP_NUMBER,
        "pairs":                  ["USD/ZAR", "GBP/ZAR", "EUR/ZAR"],
        "negotiation_buffer_bps": 10,
    },
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _send_to_lp(lp_name: str, lp_phone: str, message: str) -> None:
    """Send an SMS to the LP. Logs in all cases; sends via Twilio if configured."""
    log_line = f"[LP OUT -> {lp_name} ({lp_phone})]: {message}"
    print(log_line)
    logger.info(log_line)

    client = _get_twilio()
    if client and TWILIO_OTC_NUMBER and lp_phone:
        try:
            client.messages.create(
                from_=f"whatsapp:{TWILIO_OTC_NUMBER}",
                to=f"whatsapp:{lp_phone}",
                body=message,
            )
        except Exception as e:
            logger.error(f"Twilio outbound to LP failed: {e}")


def _log_lp_inbound(lp_name: str, message: str) -> None:
    log_line = f"[LP IN  <- {lp_name}]: {message}"
    print(log_line)
    logger.info(log_line)


# ---------------------------------------------------------------------------
# Outbound: rate request to LP (async)
# LP replies separately via /lp-webhook -> /webhook
# ---------------------------------------------------------------------------

def request_rate(pair: str, volume: float, trade_id: str) -> None:
    """
    Send a rate request to the LP. Returns immediately - LP reply arrives async.
    trade_id is embedded in the message so the reply can be matched back to the trade.
    """
    pair = pair.upper()
    if pair not in MOCK_RATES:
        logger.warning(f"request_rate: unsupported pair {pair}")
        return

    lp_name  = MOCK_RATES[pair]["lp"]
    lp_phone = LP_REGISTRY.get(lp_name, {}).get("whatsapp") or TWILIO_LP_NUMBER
    hard, _  = pair.split("/")

    _send_to_lp(
        lp_name, lp_phone,
        f"Rate request [{trade_id}]: {pair}, volume {hard} {volume:,.0f}. "
        f"Reply: [{trade_id}] <rate>"
    )


# ---------------------------------------------------------------------------
# Mock rate fetch (in-process, no Twilio needed)
# Used in mock phase instead of the async SMS round-trip.
# In live phase: remove this and use request_rate() + async LP reply.
# ---------------------------------------------------------------------------

def get_mock_rate(pair: str, trade_id: str) -> Optional[dict]:
    """Return a mock LP rate directly, bypassing Twilio SMS entirely."""
    pair = pair.upper()
    if pair not in MOCK_RATES:
        logger.warning(f"get_mock_rate: unsupported pair {pair}")
        return None
    data = MOCK_RATES[pair]
    rate = data["rate"]
    lp_name = data["lp"]
    log_line = f"[LP MOCK <- {lp_name}]: [{trade_id}] {pair}: {rate:.4f}"
    print(log_line)
    logger.info(log_line)
    return {"trade_id": trade_id, "lp_rate": rate, "lp_name": lp_name}


# ---------------------------------------------------------------------------
# /lp-webhook: Twilio #2 receives request, generates mock rate, replies
# ---------------------------------------------------------------------------

def generate_lp_reply(body: str) -> str:
    """
    Parse an inbound rate request on Twilio #2 and return a mock rate response.
    Called from /lp-webhook in main.py.
    Response format: "[TRD-104] EUR/NGN: 1481.0000"
    """
    trade_id_match = re.search(r'\[([A-Z0-9-]+)\]', body)
    pair_match     = re.search(r'\]\s*:\s*([A-Z]+/[A-Z]+)', body)

    if not trade_id_match or not pair_match:
        return "Unable to parse rate request."

    trade_id = trade_id_match.group(1)
    pair     = pair_match.group(1).upper()

    if pair not in MOCK_RATES:
        return f"[{trade_id}] Pair {pair} not supported."

    rate    = MOCK_RATES[pair]["rate"]
    lp_name = MOCK_RATES[pair]["lp"]
    _log_lp_inbound(lp_name, f"[{trade_id}] {pair}: {rate:.4f}")
    return f"[{trade_id}] {pair}: {rate:.4f}"


# ---------------------------------------------------------------------------
# /webhook inbound: parse LP rate response arriving at Twilio #1
# ---------------------------------------------------------------------------

def parse_lp_response(body: str) -> Optional[dict]:
    """
    Parse LP's rate response. Accepts two formats:
    - Full (auto-webhook): "[TRD-104] EUR/NGN: 1481.0000"
    - Short (manual reply): "[TRD-104] 1481" or "[TRD-104] 1481.50"
    Returns dict with trade_id and lp_rate. lp_name is filled in by handle_lp_response.
    """
    match = re.search(r'\[([A-Z0-9-]+)\]\s*(?:[A-Z/]+\s*:\s*)?([\d.]+)', body)
    if not match:
        return None

    return {
        "trade_id": match.group(1),
        "lp_rate":  float(match.group(2)),
    }


# ---------------------------------------------------------------------------
# Negotiation (synchronous mock - no LP WhatsApp thread needed yet)
# ---------------------------------------------------------------------------

def request_negotiation(
    lp_name: str,
    pair: str,
    volume: float,
    client_counter: float,
    original_lp_rate: float,
) -> dict:
    """
    Ask the LP if they can improve their rate to accommodate a client counter.
    Synchronous mock: LP responds instantly based on negotiation_buffer_bps.
    """
    cfg        = LP_REGISTRY.get(lp_name, {})
    buffer_bps = cfg.get("negotiation_buffer_bps", 0)
    lp_phone   = cfg.get("whatsapp") or TWILIO_LP_NUMBER
    hard, _    = pair.split("/")

    _send_to_lp(
        lp_name, lp_phone,
        f"Client countering at {client_counter:,.2f} on {pair} {hard} {volume:,.0f}. "
        f"Can you improve on {original_lp_rate:,.4f}?"
    )

    lp_best_rate      = round(original_lp_rate * (1 - buffer_bps / 10_000), 4)
    min_customer_rate = round(lp_best_rate * (1 + FLOOR_BPS / 10_000), 2)
    accepted          = client_counter >= min_customer_rate

    mock_response = (
        f"Can do {lp_best_rate:,.4f}."
        if accepted
        else f"Floor is {lp_best_rate:,.4f}, can't go lower."
    )
    _log_lp_inbound(lp_name, mock_response)

    return {
        "accepted":          accepted,
        "lp_best_rate":      lp_best_rate,
        "min_customer_rate": min_customer_rate,
    }
