import re
from datetime import datetime, timezone

import anthropic

from config import ANTHROPIC_API_KEY
from models import Trade, Customer, TradeState, CustomerType
from rates import get_rate, is_rate_expired, seconds_remaining, supported_pairs
from pricing import calculate_markup, apply_markup_to_rate, min_acceptable_rate
from compliance import check_compliance, format_flags_for_message

# --- Clients and state ---

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# In-memory session store: phone_number -> session dict
# Each session holds the active trade, customer record, and conversation history
sessions: dict[str, dict] = {}

# Trade ID counter - continuing from TRD-103 (last known trade)
_trade_counter = 103


def _next_trade_id() -> str:
    global _trade_counter
    _trade_counter += 1
    return f"TRD-{_trade_counter}"


# --- Known customers ---
# In Phase 1, customer records are hardcoded here.
# Phase 2: move to a database.
# phone number -> Customer object

KNOWN_CUSTOMERS: dict[str, Customer] = {
    # Example entries - replace with real numbers when known
    # "+447700000001": Customer(phone_number="+447700000001", name="IFXBridge", customer_type=CustomerType.FI, kyc_verified=True, is_new=False),
}


def _get_or_create_session(phone_number: str) -> dict:
    if phone_number not in sessions:
        customer = KNOWN_CUSTOMERS.get(
            phone_number,
            Customer(phone_number=phone_number)  # Unknown = new FI by default
        )
        sessions[phone_number] = {
            "trade":    None,
            "customer": customer,
            "history":  [],  # List of {"role": "user"|"assistant", "content": "..."}
        }
    return sessions[phone_number]


# --- System prompt ---

def _build_system_prompt(session: dict) -> str:
    trade: Trade | None = session["trade"]
    customer: Customer  = session["customer"]

    # Build dynamic context block
    if trade is None:
        trade_context = "No active trade. Waiting for enquiry."
    else:
        expiry_info = ""
        if trade.quote_time:
            if is_rate_expired(trade.quote_time):
                expiry_info = "RATE EXPIRED"
            else:
                secs = seconds_remaining(trade.quote_time)
                expiry_info = f"{secs // 60}m {secs % 60}s remaining"

        trade_context = f"""
Active trade: {trade.trade_id}
State: {trade.state.value}
Pair: {trade.currency_pair or 'not yet captured'}
Volume: {'${:,.0f}'.format(trade.volume_usd) if trade.volume_usd else 'not yet captured'}
Counterparty: {trade.counterparty or 'not yet captured'}
LP: {trade.lp_name or '-'}
LP rate: {trade.lp_rate or '-'}
Customer rate: {trade.customer_rate or '-'}
Markup: {f'{trade.markup_bps:.1f}bps' if trade.markup_bps else '-'}
Quote expiry: {expiry_info or '-'}
Compliance flags: {trade.compliance_flags if trade.compliance_flags else 'none'}
""".strip()

    return f"""You are the Zuba OTC trade desk agent. Zuba is a stablecoin-native cross-border payments platform moving money between Africa, Europe, the US and Asia.

You handle inbound OTC trade enquiries over WhatsApp. Your job: take an enquiry, quote a rate, negotiate if needed, lock in the rate, then produce a structured trade summary.

## Supported hard/stable currencies for payout
USD, USDT, USDC, GBP, EUR, ZAR, CAD

## Supported currency pairs (hard currency / funding currency)
{', '.join(supported_pairs())}

## Funding currency rules
- The funding currency is the local currency the customer sends to Zuba.
- If the customer states their funding currency explicitly, use it.
- If they only state the hard currency they want (e.g. "best rate on USD"), assume NGN as the funding currency unless context suggests otherwise.
- We will add country-based detection in a future phase.

## Customer
Name: {customer.name}
Type: {customer.customer_type.value} ({'Financial institution' if customer.customer_type == CustomerType.FI else 'Merchant'})
KYC verified: {customer.kyc_verified}
New counterparty: {customer.is_new}

## Current trade context
{trade_context}

## Conversation states
You must move through these states in order. Signal each state change by starting your reply with [STATE:X] on its own line.
Valid states: ENQUIRY, RATE_QUOTED, NEGOTIATING, LOCKED_IN, SUMMARY_POSTED

**ENQUIRY**
Gather two things: hard currency wanted and volume (in USD equivalent).
- If the customer states a full pair (e.g. USD/NGN), use it. If they only state hard currency (e.g. "best rate on USD"), assume NGN as funding currency.
- You do NOT need the counterparty name to quote. You will ask for it at lock-in.
- Once you have hard currency and volume, output these tags on separate lines before your reply:
  [PAIR:USD/NGN]
  [VOLUME:100000]
  Then signal [STATE:RATE_QUOTED].
- The rate and markup will be injected into your context after you output these tags - use the values provided, do not invent them.

**RATE_QUOTED**
Present the rate clearly. Always label it INDICATIVE at this stage.
Show: rate, LP source, markup in bps, expiry time (10 minutes from now).
Invite the customer to CONFIRM or negotiate.
If the rate expires before the customer responds, tell them and signal [STATE:ENQUIRY] to re-fetch.

**NEGOTIATING**
Customer has pushed back. You may offer a tighter rate if their target is above the minimum acceptable rate provided in context.
Never quote below the LP rate. Maximum concession: reduce markup by up to 50%.
If customer's target is below what we can offer, explain clearly and hold firm.
Once customer accepts, signal [STATE:LOCKED_IN].

**LOCKED_IN**
Customer has confirmed. State clearly that the rate is now COMMITTED - not indicative.
Instruct the customer to send funds now. Emphasise: do not send funds without this confirmation.
If you do not yet have the counterparty name, ask for it now.
Ask for beneficiary details if not yet provided (name + account details).
Signal [STATE:LOCKED_IN].

**SUMMARY_POSTED**
Once you have beneficiary details, generate the full TRD-XXX summary in the exact format below.
Signal [STATE:SUMMARY_POSTED].

## TRD-XXX format (use exactly)
```
[TRADE_ID]
Trade Amount:       [BASE CURRENCY] [AMOUNT]
Amount in local:    [LOCAL CURRENCY] [LOCAL AMOUNT]
Exchange Rate:      [CUSTOMER RATE]
Counterparty:       [NAME]
LP:                 [LP NAME] (rate locked [HH:MM] UTC)
Markup:             [BPS]bps
Quote status:       LOCKED ✅
Beneficiary name:   [NAME]
Beneficiary details:[ACCOUNT DETAILS]
Purpose:            [IF PROVIDED, else 'not stated']
Compliance:         [FLAGS or ✅ Clear]
```

## Compliance
If any compliance flag is active (shown in trade context), include this block in your reply:
⚠️ COMPLIANCE PAUSE: [flag description]. Trade paused - Tolu and Ali must clear this before funds move.

## Tone
Professional, clear, concise. Financial services context. Be direct. No waffle.
Do not repeat information the customer already knows.
Do not apologise unnecessarily.
"""


# --- State transition logic ---

def _handle_state_transition(session: dict, new_state_str: str, message: str):
    """Update trade state and trigger any side effects (rate fetch, compliance check)."""
    trade: Trade    = session["trade"]
    customer: Customer = session["customer"]

    try:
        new_state = TradeState(new_state_str)
    except ValueError:
        return  # Unknown state tag - ignore

    if trade is None:
        return

    trade.state = new_state

    # On entering RATE_QUOTED: fetch rate and calculate markup
    if new_state == TradeState.RATE_QUOTED and trade.currency_pair and trade.volume_usd:
        rate_data = get_rate(trade.currency_pair)
        if rate_data:
            markup = calculate_markup(customer.customer_type, trade.volume_usd)
            customer_rate = apply_markup_to_rate(rate_data["lp_rate"], markup)
            trade.lp_name       = rate_data["lp_name"]
            trade.lp_rate       = rate_data["lp_rate"]
            trade.markup_bps    = markup
            trade.customer_rate = customer_rate
            trade.quote_time    = datetime.now(timezone.utc).replace(tzinfo=None)

        # Run compliance check
        flags = check_compliance(trade, customer)
        trade.compliance_flags = flags

    # On LOCKED_IN: record lock timestamp
    if new_state == TradeState.LOCKED_IN:
        trade.locked_at = datetime.utcnow()


def _extract_trade_details_from_history(session: dict, latest_message: str):
    """
    Best-effort extraction of trade details from conversation so far.
    Claude handles the NLP - this just ensures the trade object is created
    before Claude tries to quote a rate.
    """
    trade: Trade | None = session["trade"]
    if trade is None:
        session["trade"] = Trade(trade_id=_next_trade_id())
        trade = session["trade"]

    # We rely on Claude to extract pair/volume/counterparty and signal state changes.
    # This function just ensures the trade object exists.
    return trade


# --- Main entry point ---

def handle_message(phone_number: str, message: str) -> str:
    """
    Receive a WhatsApp message, process it through the agent, return a reply.
    Called by main.py on every incoming webhook.
    """
    session = _get_or_create_session(phone_number)
    trade   = _extract_trade_details_from_history(session, message)

    # Build conversation history for Claude
    session["history"].append({"role": "user", "content": message})

    # Inject current rate/pricing data into system prompt context
    system_prompt = _build_system_prompt(session)

    # Call Claude
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system_prompt,
        messages=session["history"],
    )

    raw_reply = response.content[0].text

    # Parse PAIR and VOLUME tags Claude emits before signalling RATE_QUOTED
    pair_match   = re.search(r"\[PAIR:([^\]]+)\]",   raw_reply)
    volume_match = re.search(r"\[VOLUME:([^\]]+)\]", raw_reply)
    if pair_match:
        trade.currency_pair = pair_match.group(1).upper().strip()
    if volume_match:
        try:
            trade.volume_usd = float(volume_match.group(1).replace(",", ""))
        except ValueError:
            pass

    # Parse state tag - Claude signals transitions with [STATE:X]
    state_match = re.search(r"\[STATE:(\w+)\]", raw_reply)
    if state_match:
        new_state_str = state_match.group(1)
        _handle_state_transition(session, new_state_str, message)

        # Re-call Claude with fresh rate data injected into context
        if new_state_str == "RATE_QUOTED" and trade.currency_pair and trade.volume_usd:
            system_prompt = _build_system_prompt(session)
            response = claude.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_prompt,
                messages=session["history"],
            )
            raw_reply = response.content[0].text

    # Store assistant reply in history
    session["history"].append({"role": "assistant", "content": raw_reply})

    # Strip all control tags from the customer-facing message
    clean_reply = re.sub(r"\[STATE:\w+\]\n?", "", raw_reply)
    clean_reply = re.sub(r"\[PAIR:[^\]]+\]\n?", "", clean_reply)
    clean_reply = re.sub(r"\[VOLUME:[^\]]+\]\n?", "", clean_reply)
    clean_reply = clean_reply.strip()

    # Append compliance flags if present and not already in reply
    if trade and trade.compliance_flags:
        flag_block = format_flags_for_message(trade.compliance_flags)
        if "COMPLIANCE" not in clean_reply:
            clean_reply += f"\n\n{flag_block}"

    return clean_reply
