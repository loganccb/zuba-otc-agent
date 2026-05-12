from models import CustomerType

# --- Constants ---

FLOOR_BPS = 30  # Minimum margin - never quote below this (Tolu's floor)

BASE_MARKUP_BPS = {
    CustomerType.FI:       50,   # Financial institutions
    CustomerType.MERCHANT: 100,  # Merchants
}

# Volume compression: each $100k of trade size reduces markup by 20%
# e.g. FI at $200k: 50 * 0.8^2 = 32bps (just above floor)
# e.g. FI at $300k: 50 * 0.8^3 = 25.6 -> floored at 30bps
COMPRESSION_RATE  = 0.80       # multiply by this per step
COMPRESSION_STEP  = 100_000    # one step = $100k

# Max concession during negotiation: reduce markup by up to 50%
MAX_NEGOTIATION_CONCESSION = 0.50


def calculate_markup(customer_type: CustomerType, volume_usd: float) -> float:
    """
    Calculate markup in basis points for a given customer type and volume.

    Starts at base rate and compresses 20% per $100k of volume.
    Hard floor at 30bps regardless of volume or customer type.
    """
    base = BASE_MARKUP_BPS[customer_type]
    steps = volume_usd / COMPRESSION_STEP
    markup = base * (COMPRESSION_RATE ** steps)
    return round(max(markup, FLOOR_BPS), 2)


def apply_markup_to_rate(lp_rate: float, markup_bps: float) -> float:
    """
    Derive the customer-facing rate from the LP rate and our markup.

    We quote the customer a rate slightly worse than the LP rate.
    The difference is our captured spread.

    Note: this assumes customer is converting FROM base currency to local
    (e.g. selling USDT to receive NGN). Direction should be confirmed with
    Tolu for trades where customer is buying the base currency.
    """
    customer_rate = lp_rate * (1 - markup_bps / 10_000)
    return round(customer_rate, 2)


def min_acceptable_rate(lp_rate: float, markup_bps: float) -> float:
    """
    The lowest rate we can accept during negotiation without going below floor.
    Used to tell Claude the negotiation boundary.
    """
    min_markup = max(markup_bps * (1 - MAX_NEGOTIATION_CONCESSION), FLOOR_BPS)
    return apply_markup_to_rate(lp_rate, min_markup)


def markup_summary(customer_type: CustomerType, volume_usd: float) -> dict:
    """Return a full pricing summary dict for use in agent context."""
    markup = calculate_markup(customer_type, volume_usd)
    return {
        "customer_type": customer_type.value,
        "volume_usd":    volume_usd,
        "markup_bps":    markup,
        "floor_bps":     FLOOR_BPS,
    }
