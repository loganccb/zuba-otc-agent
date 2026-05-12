from datetime import datetime, timedelta
from typing import Optional

# Mock LP rates - realistic values for May 2026
# Format: "PAIR": {"lp": "LP name", "rate": float}
# Rate = local currency units per 1 unit of base currency
# e.g. USDT/NGN = 1580 means 1 USDT buys 1580 NGN from LP

MOCK_RATES = {
    "USDT/NGN": {"lp": "Yeba",          "rate": 1580.0},
    "USD/NGN":  {"lp": "Yeba",          "rate": 1578.0},
    "GBP/NGN":  {"lp": "One Liquidity", "rate": 2005.0},
    "USDC/NGN": {"lp": "Muva",          "rate": 1579.0},
    "USDC/GHS": {"lp": "Emergent",      "rate": 15.42},
    "USDT/GHS": {"lp": "Emergent",      "rate": 15.40},
    "USD/ZAR":  {"lp": "Kora Pay",      "rate": 18.65},
    "GBP/ZAR":  {"lp": "Kora Pay",      "rate": 23.55},
}

RATE_VALIDITY_SECONDS = 600  # 10 minutes - matches real LP validity window


def get_rate(currency_pair: str) -> Optional[dict]:
    """Fetch a mock LP rate. Returns None if pair not supported."""
    pair = currency_pair.upper().replace(" ", "")
    if pair not in MOCK_RATES:
        return None

    data = MOCK_RATES[pair]
    fetched_at = datetime.utcnow()
    expires_at = fetched_at + timedelta(seconds=RATE_VALIDITY_SECONDS)

    return {
        "pair":       pair,
        "lp_name":    data["lp"],
        "lp_rate":    data["rate"],
        "fetched_at": fetched_at,
        "expires_at": expires_at,
    }


def is_rate_expired(quote_time: datetime) -> bool:
    """Check whether a quoted rate has passed its 10-minute validity window."""
    return (datetime.utcnow() - quote_time).total_seconds() > RATE_VALIDITY_SECONDS


def seconds_remaining(quote_time: datetime) -> int:
    """How many seconds until the rate expires. Returns 0 if already expired."""
    elapsed = (datetime.utcnow() - quote_time).total_seconds()
    return max(0, int(RATE_VALIDITY_SECONDS - elapsed))


def supported_pairs() -> list:
    return list(MOCK_RATES.keys())
