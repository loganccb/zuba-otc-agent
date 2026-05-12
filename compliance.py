from datetime import datetime
from models import Trade, Customer
from rates import is_rate_expired

# --- Thresholds ---

LARGE_TRADE_THRESHOLD_USD = 100_000  # Flag and pause for manual sign-off above this


def check_compliance(trade: Trade, customer: Customer) -> list[str]:
    """
    Run compliance checks and return a list of flag strings.
    Empty list = no flags. Any flags = trade must pause for human review.
    """
    flags = []

    # 1. Large trade - requires Tolu/Ali sign-off
    if trade.volume_usd and trade.volume_usd >= LARGE_TRADE_THRESHOLD_USD:
        flags.append(
            f"LARGE_TRADE: Volume ${trade.volume_usd:,.0f} exceeds "
            f"${LARGE_TRADE_THRESHOLD_USD:,.0f} threshold - Tolu/Ali sign-off required"
        )

    # 2. New counterparty or KYC not verified
    if customer.is_new or not customer.kyc_verified:
        flags.append(
            "NEW_COUNTERPARTY: KYC verification required before execution"
        )

    # 3. Weekend execution risk
    weekday = datetime.utcnow().weekday()  # Monday=0, Sunday=6
    if weekday >= 5:
        flags.append(
            "WEEKEND_EXECUTION: Settlement risk - confirm LP cutoff times before proceeding"
        )

    # 4. Rate expired before lock-in
    if trade.quote_time and is_rate_expired(trade.quote_time):
        flags.append(
            "RATE_EXPIRED: Quote has exceeded 10-minute validity window - must re-fetch rate"
        )

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
