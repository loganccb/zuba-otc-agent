from datetime import datetime
from models import Trade, Customer
from rates import is_rate_expired

# --- Thresholds ---
# TODO: confirm LARGE_TRADE_THRESHOLD_USD with Ali/Raza
# TODO: confirm LP coverage limits per LP with Tolu - needed for SIGNED_LP_AT_VOLUME check
LARGE_TRADE_THRESHOLD_USD = 100_000


def check_compliance(trade: Trade, customer: Customer) -> list[str]:
    """
    Run compliance checks and return a list of flag strings.
    Empty list = no flags. Any flags = trade pauses for human review.
    """
    flags = []

    # 1. Large trade
    if trade.volume_usd and trade.volume_usd >= LARGE_TRADE_THRESHOLD_USD:
        flags.append(
            f"LARGE_TRADE: Volume ${trade.volume_usd:,.0f} exceeds "
            f"${LARGE_TRADE_THRESHOLD_USD:,.0f} threshold"
        )

    # 2. Weekend execution risk
    if datetime.utcnow().weekday() >= 5:  # Saturday=5, Sunday=6
        flags.append("WEEKEND_EXECUTION: Confirm LP cutoff times before proceeding")

    # 3. New counterparty - not in known customer directory
    if customer.is_new:
        flags.append(
            f"NEW_COUNTERPARTY: {customer.phone_number} not in verified customer directory"
        )

    # 4. Rate expired before lock-in
    if trade.quote_time and is_rate_expired(trade.quote_time):
        flags.append("RATE_EXPIRED: Quote exceeded 10-minute validity window")

    return flags


def format_flags_for_message(flags: list[str]) -> str:
    """Format compliance flags as a readable block for WhatsApp messages."""
    if not flags:
        return "✅ No compliance flags"
    lines = ["⚠️ COMPLIANCE FLAGS:"]
    for f in flags:
        lines.append(f"  - {f}")
    lines.append("")
    lines.append("Trade paused pending human review. Tolu and Ali have been notified.")
    return "\n".join(lines)
