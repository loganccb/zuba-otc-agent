import json
import anthropic

from models import CustomerType
from config import ANTHROPIC_API_KEY

# --- Constants ---

FLOOR_BPS = 20  # Minimum margin - never quote below this

BASE_MARKUP_BPS = {
    CustomerType.FI:       50,   # Financial institutions
    CustomerType.MERCHANT: 70,   # Merchants
}

# Volume compression: each $100k of trade size reduces markup by 28%
# e.g. FI at $200k: 50 * 0.72^2 = 25.9bps
# e.g. FI at $300k: 50 * 0.72^3 = 18.7bps -> floored at 20bps
COMPRESSION_RATE  = 0.72       # multiply by this per step
COMPRESSION_STEP  = 100_000    # one step = $100k

# Max concession during negotiation: reduce markup by up to 50%
MAX_NEGOTIATION_CONCESSION = 0.50

# Sensitivity-adjusted pricing: max reduction applied when sensitivity = 1.0
MAX_SENSITIVITY_ADJ = 0.20


_anthropic_client = None

def _get_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


def detect_sensitivity(message: str, customer_type: CustomerType) -> float:
    """
    Analyse an opening client message for price sensitivity signals.

    Returns a score from 0.0 (no signals) to 1.0 (strong signals).
    Excluded: bare enquiries with only pair/volume and no commentary.

    Signals counted:
    - Explicit target rate ("I need at least 1385")
    - Competitor or market rate reference ("my other desk quoted me X")
    - Volume-conditional language ("if the rate is good I can do more")
    - Request for improvement on a previous rate

    FI clients pass costs to their own end-clients so their signals carry
    more weight - but client type alone is not scored as a signal.
    """
    prompt = f"""You are reviewing a client's WhatsApp message to an OTC FX trade desk.
Determine how price-sensitive this client appears based only on explicit signals in their message.

Client type: {customer_type.value} ({"financial institution or payment operator - passes FX costs to end-clients, so rate signals carry more weight" if customer_type.value == "FI" else "merchant - buys FX for business purposes"})

Score 0.0 to 1.0:
- 0.0: no signals - message is a bare enquiry (pair, volume, pleasantries only)
- 0.3: mild signal (e.g. "can you do better?", "what's your best rate?")
- 0.6: clear signal (explicit target rate, or volume-conditional language)
- 1.0: strong signals (explicit target rate AND competitor reference, or aggressive framing)

DO NOT score as a signal:
- Providing only pair and volume with no commentary
- Standard OTC greetings or pleasantries
- Urgency about timing (not about price)

COUNT as signals:
- Explicit numeric rate target ("I need at least 1385", "looking for 1390")
- Reference to a competitor or alternative provider's rate
- Volume-conditional language ("if the rate is right, I can do more", "might increase volume")
- Request to beat or improve on a prior rate

Respond with ONLY valid JSON, no other text: {{"score": float, "reason": str}}

Client message:
{message}"""

    try:
        response = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(response.content[0].text.strip())
        return float(max(0.0, min(1.0, result.get("score", 0.0))))
    except Exception:
        return 0.0  # Fail safe - no adjustment if detection fails


def calculate_markup(customer_type: CustomerType, volume_usd: float, sensitivity: float = 0.0) -> float:
    """
    Calculate markup in basis points for a given customer type and volume.

    Starts at base rate and compresses 20% per $100k of volume.
    Hard floor at 20bps regardless of volume or customer type.
    Optional sensitivity score (0.0-1.0) applies up to 20% reduction.
    """
    base = BASE_MARKUP_BPS[customer_type]
    steps = volume_usd / COMPRESSION_STEP
    markup = base * (COMPRESSION_RATE ** steps)
    markup = max(markup, FLOOR_BPS)
    if sensitivity > 0:
        reduction = 1 - (MAX_SENSITIVITY_ADJ * sensitivity)
        markup = max(markup * reduction, FLOOR_BPS)
    return round(markup, 2)


def apply_markup_to_rate(lp_rate: float, markup_bps: float) -> float:
    """
    Derive the customer-facing rate from the LP rate and our markup.

    We quote the customer a rate slightly worse than the LP rate.
    The difference is our captured spread.

    Direction confirmed: customer sends local currency (NGN/GHS) and receives
    hard/stable currency (USD/GBP/USDT). They are buying hard currency with local.
    We charge MORE local currency per unit of hard currency than the LP charges us.
    e.g. LP rate 1380 NGN/USD + 50bps -> customer pays 1380.69 NGN/USD.
    We collect 1380.69, pay LP 1380, keep 0.69 NGN per USD as spread.
    """
    customer_rate = lp_rate * (1 + markup_bps / 10_000)
    return round(customer_rate, 2)


def min_acceptable_rate(lp_rate: float, markup_bps: float) -> float:
    """
    The lowest rate we can accept during negotiation without going below floor.
    Used to tell Claude the negotiation boundary.
    """
    # Minimum markup = lower of 20bps floor or 50% of original markup
    min_markup = min(markup_bps * (1 - MAX_NEGOTIATION_CONCESSION), FLOOR_BPS)
    return apply_markup_to_rate(lp_rate, min_markup)  # still higher than LP rate


def markup_summary(customer_type: CustomerType, volume_usd: float, sensitivity: float = 0.0) -> dict:
    """Return a full pricing summary dict for use in agent context."""
    markup = calculate_markup(customer_type, volume_usd, sensitivity)
    return {
        "customer_type": customer_type.value,
        "volume_usd":    volume_usd,
        "markup_bps":    markup,
        "floor_bps":     FLOOR_BPS,
    }
