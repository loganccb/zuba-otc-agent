from datetime import datetime, timedelta
from typing import Optional

# Mock LP rates - realistic values for May 2026
# Format: "PAIR": {"lp": "LP name", "rate": float}
# Rate = local currency units per 1 unit of base currency
# e.g. USDT/NGN = 1580 means 1 USDT buys 1580 NGN from LP

MOCK_RATES = {
    # Rate = local currency units per 1 unit of hard/stable currency
    # Customer sends local (NGN/GHS/ZAR), receives hard currency
    # Calibrated against TRD data: USD/NGN ~1384, May 2026

    # NGN pairs
    "USD/NGN":  {"lp": "Yeba",          "rate": 1384.0},
    "USDT/NGN": {"lp": "Yeba",          "rate": 1383.5},
    "USDC/NGN": {"lp": "Muva",          "rate": 1383.5},
    "GBP/NGN":  {"lp": "One Liquidity", "rate": 1748.0},  # GBP/USD ~1.263 * 1384
    "EUR/NGN":  {"lp": "One Liquidity", "rate": 1481.0},  # EUR/USD ~1.070 * 1384
    "CAD/NGN":  {"lp": "Yeba",          "rate": 1010.0},  # CAD/USD ~0.730 * 1384

    # GHS pairs
    "USD/GHS":  {"lp": "Emergent",      "rate": 15.42},
    "USDC/GHS": {"lp": "Emergent",      "rate": 15.42},
    "USDT/GHS": {"lp": "Emergent",      "rate": 15.40},
    "GBP/GHS":  {"lp": "Emergent",      "rate": 19.48},
    "EUR/GHS":  {"lp": "Emergent",      "rate": 16.50},

    # ZAR pairs
    "USD/ZAR":  {"lp": "Kora Pay",      "rate": 18.65},
    "GBP/ZAR":  {"lp": "Kora Pay",      "rate": 23.55},
    "EUR/ZAR":  {"lp": "Kora Pay",      "rate": 19.96},
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
