"""
lp_comms.py - LP communication layer

Mock phase: logs outbound messages and returns instant mock responses.
Live phase: replace _send_to_lp() with Twilio outbound API calls,
            and wire inbound LP replies through a separate webhook route.

Nothing in this module touches agent state - it just sends/receives
and returns structured dicts. All routing logic stays in agent.py.
"""

from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

from rates import MOCK_RATES
from pricing import FLOOR_BPS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LP registry
# whatsapp: LP's number for outbound messages (placeholder until live)
# pairs:    currency pairs this LP covers
# negotiation_buffer_bps: how many bps the LP can improve their rate by
# ---------------------------------------------------------------------------

LP_REGISTRY: dict[str, dict] = {
    "Yeba": {
        "whatsapp":                "+2348000000001",
        "pairs":                   ["USD/NGN", "USDT/NGN", "CAD/NGN"],
        "negotiation_buffer_bps":  20,
    },
    "Muva": {
        "whatsapp":                "+2348000000002",
        "pairs":                   ["USDC/NGN"],
        "negotiation_buffer_bps":  15,
    },
    "One Liquidity": {
        "whatsapp":                "+2348000000003",
        "pairs":                   ["GBP/NGN", "EUR/NGN"],
        "negotiation_buffer_bps":  15,
    },
    "Emergent": {
        "whatsapp":                "+2348000000004",
        "pairs":                   ["USD/GHS", "USDC/GHS", "USDT/GHS", "GBP/GHS", "EUR/GHS"],
        "negotiation_buffer_bps":  20,
    },
    "Kora Pay": {
        "whatsapp":                "+2348000000005",
        "pairs":                   ["USD/ZAR", "GBP/ZAR", "EUR/ZAR"],
        "negotiation_buffer_bps":  10,
    },
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _send_to_lp(lp_name: str, message: str) -> None:
    """
    Send a message to an LP.
    Mock: logs to stdout and logger.
    Live: replace with Twilio outbound WhatsApp API call.
    """
    phone = LP_REGISTRY.get(lp_name, {}).get("whatsapp", "unknown")
    log_line = f"[LP OUT -> {lp_name} ({phone})]: {message}"
    print(log_line)
    logger.info(log_line)


def _receive_from_lp(lp_name: str, message: str) -> None:
    """Log a (mock) inbound LP response."""
    log_line = f"[LP IN  <- {lp_name}]: {message}"
    print(log_line)
    logger.info(log_line)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def request_rate(pair: str, volume: float) -> Optional[dict]:
    """
    Request a rate from the appropriate LP for a given pair and volume.

    Returns a dict compatible with the old rates.get_rate() structure,
    or None if the pair is unsupported.

    Mock: looks up MOCK_RATES and simulates the message exchange.
    Live: send outbound WhatsApp and await async inbound reply.
    """
    pair = pair.upper()
    if pair not in MOCK_RATES:
        return None

    lp_name  = MOCK_RATES[pair]["lp"]
    lp_rate  = MOCK_RATES[pair]["rate"]
    hard, _  = pair.split("/")

    _send_to_lp(lp_name, f"Rate request: {pair}, volume {hard} {volume:,.0f}. Please quote.")

    mock_response = f"{pair}: {lp_rate:,.4f} valid 10 mins"
    _receive_from_lp(lp_name, mock_response)

    return {
        "pair":       pair,
        "lp_name":    lp_name,
        "lp_rate":    lp_rate,
        "fetched_at": datetime.utcnow(),
    }


def request_negotiation(
    lp_name: str,
    pair: str,
    volume: float,
    client_counter: float,
    original_lp_rate: float,
) -> dict:
    """
    Ask the LP if they can improve their rate to accommodate a client counter.

    Returns:
        accepted          bool   - True if LP can support the client's counter
        lp_best_rate      float  - LP's best achievable rate
        min_customer_rate float  - lowest rate we can offer client on LP's best rate

    Mock: applies negotiation_buffer_bps to compute LP's best rate.
    Live: send outbound WhatsApp and await async inbound reply.
    """
    cfg        = LP_REGISTRY.get(lp_name, {})
    buffer_bps = cfg.get("negotiation_buffer_bps", 0)
    hard, _    = pair.split("/")

    _send_to_lp(
        lp_name,
        f"Client countering at {client_counter:,.2f} on {pair} {hard} {volume:,.0f}. "
        f"Can you improve on {original_lp_rate:,.4f}?"
    )

    # LP lowers their rate by up to buffer_bps
    lp_best_rate      = round(original_lp_rate * (1 - buffer_bps / 10_000), 4)
    # Minimum we can pass to client = LP's best + our hard floor
    min_customer_rate = round(lp_best_rate * (1 + FLOOR_BPS / 10_000), 2)
    accepted          = client_counter >= min_customer_rate

    mock_response = (
        f"Can do {lp_best_rate:,.4f}."
        if accepted
        else f"Floor is {lp_best_rate:,.4f}, can't go lower."
    )
    _receive_from_lp(lp_name, mock_response)

    return {
        "accepted":          accepted,
        "lp_best_rate":      lp_best_rate,
        "min_customer_rate": min_customer_rate,
    }
