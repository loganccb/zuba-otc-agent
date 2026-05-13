import os
import re
from datetime import datetime, timedelta
from typing import Optional

import anthropic

from config import ANTHROPIC_API_KEY, BASE_URL
from models import Trade, Customer, TradeState, CustomerType
from rates import is_rate_expired, seconds_remaining
import lp_comms
import slack
from pricing import calculate_markup, apply_markup_to_rate, min_acceptable_rate, detect_sensitivity
from compliance import check_compliance, format_flags_for_message

# --- Client and state ---

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

sessions: dict[str, dict] = {}
_pending_lp: dict[str, str] = {}           # trade_id -> client phone, async LP flow
_pending_compliance: dict[str, str] = {}   # trade_id -> client phone, compliance hold

_COUNTER_FILE = os.path.join(os.path.dirname(__file__), "trade_counter.txt")
_COUNTER_FALLBACK = 207  # first trade will be TRD-208


def _next_trade_id() -> str:
    try:
        with open(_COUNTER_FILE, "r") as f:
            current = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        current = _COUNTER_FALLBACK
    next_id = current + 1
    with open(_COUNTER_FILE, "w") as f:
        f.write(str(next_id))
    return f"TRD-{next_id}"


# --- Known customers ---
# TODO: replace with database lookup once WhatsApp number -> business name mapping is built
# TODO: set customer_type correctly per customer once confirmed with Tolu
KNOWN_CUSTOMERS: dict[str, Customer] = {
    "+447455819005": Customer(
        phone_number="+447455819005",
        name="LCB Enterprises, Ltd",
        customer_type=CustomerType.FI,
        kyc_verified=True,
        is_new=False,
    ),
    "+19178210600": Customer(
        phone_number="+19178210600",
        name="LCB Enterprises, Ltd",
        customer_type=CustomerType.FI,
        kyc_verified=True,
        is_new=False,
    ),
}

HARD_CURRENCIES = ["USDT", "USDC", "USD", "GBP", "EUR", "CAD", "ZAR", "CNY"]

ZUBA_ACCOUNT = {
    "account_name":   "Zuba Technologies Ltd",
    "account_number": "7662758412",
    "bank_name":      "Globus Bank",
}


# --- Session management ---

def _get_or_create_session(phone_number: str) -> dict:
    if phone_number not in sessions:
        customer = KNOWN_CUSTOMERS.get(
            phone_number,
            Customer(phone_number=phone_number)
        )
        sessions[phone_number] = {
            "trade":             None,
            "customer":          customer,
            "history":           [],
            "last_message_time": None,
        }
    return sessions[phone_number]


def _get_or_create_trade(session: dict) -> Trade:
    if session["trade"] is None:
        session["trade"] = Trade(trade_id=_next_trade_id())
    return session["trade"]


# --- Pre-extraction ---

# Maps currency symbols and common words/variants to canonical codes.
# Checked in order after the HARD_CURRENCIES code scan fails.
_CURRENCY_ALIASES: list[tuple[str, str]] = [
    # Symbols (checked against original-case message)
    ("€",            "EUR"),
    ("£",            "GBP"),
    # Multi-word phrases first to avoid partial matches
    ("US DOLLARS",   "USD"),
    ("US DOLLAR",    "USD"),
    ("CANADIAN DOLLARS", "CAD"),
    ("CANADIAN DOLLAR",  "CAD"),
    # Single words (checked against uppercased message)
    ("EUROS",        "EUR"),
    ("EURO",         "EUR"),
    ("POUNDS",       "GBP"),
    ("POUND",        "GBP"),
    ("STERLING",     "GBP"),
    ("DOLLARS",      "USD"),
    ("DOLLAR",       "USD"),
    ("TETHER",       "USDT"),
    ("RANDS",        "ZAR"),
    ("RAND",         "ZAR"),
    ("YUAN",         "CNY"),
    ("RENMINBI",     "CNY"),
    ("RMB",          "CNY"),
    ("CEDIS",        "GHS"),
    ("CEDI",         "GHS"),
]


def _extract_pair_and_volume(message: str) -> dict:
    result = {"pair": None, "volume": None}
    msg = message.upper()

    # 1. Explicit pair e.g. EUR/NGN
    pair_match = re.search(
        r'\b(USDT|USDC|USD|GBP|EUR|CAD|ZAR|CNY)/(NGN|GHS|ZAR)\b', msg
    )
    if pair_match:
        result["pair"] = pair_match.group(0)
    else:
        found = None
        # 2. Currency code as a standalone word
        for hc in HARD_CURRENCIES:
            if re.search(rf'\b{hc}\b', msg):
                found = hc
                break
        # 3. Currency symbols and natural-language words
        if not found:
            for alias, code in _CURRENCY_ALIASES:
                # Symbols checked on original message; words on uppercased
                haystack = message if alias in ("€", "£") else msg
                if alias in haystack:
                    found = code
                    break
        # 4. Bare $ with no other currency found -> USD
        if not found and "$" in message:
            found = "USD"

        if found and found != "GHS":
            ghs_in_msg = bool(re.search(r'\b(GHS|CEDIS?|CEDI)\b', message, re.IGNORECASE))
            local = "GHS" if ghs_in_msg else "NGN"
            result["pair"] = f"{found}/{local}"

    # Volume: allow €/£/$ symbol optionally attached to number
    vol_match = re.search(
        r'[€£$]?\s*([\d,]+(?:\.\d+)?)\s*([kmb]?)\b', msg, re.IGNORECASE
    )
    if vol_match:
        num_str = vol_match.group(1).replace(",", "")
        suffix  = vol_match.group(2).upper()
        try:
            num = float(num_str)
            if suffix == "K":   num *= 1_000
            elif suffix == "M": num *= 1_000_000
            elif suffix == "B": num *= 1_000_000_000
            if num >= 100:
                result["volume"] = num
        except ValueError:
            pass

    return result


def _is_confirmation(message: str) -> bool:
    msg = message.upper().strip()
    return any(w in msg for w in ["CONFIRM", "CONFIRMED", "ACCEPT", "ACCEPTED", "AGREED", "DEAL", "LOCK IT", "LOCK IN", "YES"])


def _is_acknowledgement(message: str) -> bool:
    msg = message.upper().strip()
    return any(w in msg for w in ["OK", "OKAY", "NOTED", "GOT IT", "RECEIVED", "THANKS", "THANK YOU", "UNDERSTOOD", "YES"])


def _is_new_enquiry(message: str) -> bool:
    """Detect when the client wants to start a separate/fresh trade."""
    msg = message.upper().strip()
    return any(phrase in msg for phrase in [
        "NEW ENQUIRY", "SEPARATE ENQUIRY", "NEW TRADE", "DIFFERENT TRADE",
        "START OVER", "FRESH ENQUIRY", "DIFFERENT CURRENCY", "NEW QUOTE",
        "SEPARATE QUOTE", "ANOTHER ENQUIRY",
    ])


# --- Counter-rate extraction ---

def _extract_counter_rate(message: str, current_rate: float) -> Optional[float]:
    """
    Extract a numeric counter-rate from the message, validated against the
    current quoted rate. Must be within 20% of current_rate to qualify.
    Returns None if no plausible counter found.

    Handles common formatting variants:
    - UK/US thousands separators: 1,485 -> 1485
    - European thousands separators: 1.485 -> 1485 (tried as fallback)
    - Standard decimals: 1485.50
    """
    cleaned = message.replace(",", "")
    match = re.search(r'\b(\d+(?:\.\d+)?)\b', cleaned)
    if not match:
        return None
    try:
        val = float(match.group(1))
    except ValueError:
        return None

    lo, hi = current_rate * 0.80, current_rate * 1.20

    if lo <= val <= hi:
        return val

    # Fallback: treat a decimal point as a European thousands separator
    # e.g. "1.485" -> 1485 when current_rate is ~1481
    alt = val * 1000
    if lo <= alt <= hi:
        return alt

    return None


# --- Rate fetch (split into initiate + apply for async LP flow) ---

def _initiate_lp_request(trade: Trade, phone_number: str) -> None:
    """
    Fetch LP rate (mock) and apply it immediately.
    If compliance flags are raised, holds trade in COMPLIANCE_REVIEW and notifies via Slack.
    In live phase: replace get_mock_rate with async LP send and restore AWAITING_LP_RATE path.
    """
    lp_name = lp_comms.MOCK_RATES.get(trade.currency_pair, {}).get("lp", "LP")
    rate_data = lp_comms.get_mock_rate(trade.currency_pair, trade.trade_id)
    slack.post_lp_request(trade.trade_id, trade.currency_pair, trade.volume_usd, lp_name)
    if rate_data:
        _apply_lp_rate(trade, sessions[phone_number]["customer"], rate_data)
        slack.post_lp_response(trade.trade_id, trade.currency_pair, trade.lp_name, trade.lp_rate, trade.customer_rate)
        print(f"[COMPLIANCE CHECK] id={trade.trade_id} volume={trade.volume_usd} flags={trade.compliance_flags} state={trade.state.value}")
        if trade.compliance_flags:
            trade.state = TradeState.COMPLIANCE_REVIEW
            _pending_compliance[trade.trade_id] = phone_number
            slack.post_compliance_review(trade, phone_number, BASE_URL)
            print(f"[COMPLIANCE] Trade {trade.trade_id} held for review - flags: {trade.compliance_flags}")
        # else: state remains RATE_QUOTED from _apply_lp_rate
    else:
        _pending_lp[trade.trade_id] = phone_number
        trade.state = TradeState.AWAITING_LP_RATE


def _apply_lp_rate(trade: Trade, customer: Customer, rate_data: dict) -> None:
    """Apply received LP rate data to the trade. Called when LP response arrives."""
    markup        = calculate_markup(customer.customer_type, trade.volume_usd, trade.sensitivity_score)
    customer_rate = apply_markup_to_rate(rate_data["lp_rate"], markup)
    trade.lp_name       = rate_data["lp_name"]
    trade.lp_rate       = rate_data["lp_rate"]
    trade.markup_bps    = markup
    trade.customer_rate = customer_rate
    trade.quote_time    = datetime.utcnow()
    trade.state         = TradeState.RATE_QUOTED
    trade.quote_shown   = True  # Quote sent directly via outbound, not via quote_shown flow
    trade.compliance_flags = check_compliance(trade, customer)


def handle_lp_response(body: str) -> Optional[tuple[str, str]]:
    """
    Called from /webhook when Twilio #1 receives a rate reply from Twilio #2 (LP).
    Applies the rate, formats the quote, returns (client_phone, quote_message).
    Returns None if the response can't be parsed or no matching trade is found.
    """
    rate_data = lp_comms.parse_lp_response(body)
    if not rate_data:
        return None

    trade_id     = rate_data["trade_id"]
    client_phone = _pending_lp.get(trade_id)
    if not client_phone:
        return None

    session = sessions.get(client_phone)
    if not session or not session["trade"] or session["trade"].trade_id != trade_id:
        return None

    trade    = session["trade"]
    customer = session["customer"]

    # Fill in lp_name from trade's pair (not always present in manual LP replies)
    if "lp_name" not in rate_data:
        rate_data["lp_name"] = lp_comms.MOCK_RATES.get(trade.currency_pair, {}).get("lp", "LP")

    _apply_lp_rate(trade, customer, rate_data)
    _pending_lp.pop(trade_id, None)

    quote = _format_quote_message(trade)
    session["history"].append({"role": "assistant", "content": quote})
    return (client_phone, quote)


# --- Python-formatted messages (no Claude involved) ---

def _format_quote_message(trade: Trade) -> str:
    pair       = trade.currency_pair          # e.g. "EUR/NGN"
    hard, local = pair.split("/")
    expiry_str = (trade.quote_time + timedelta(seconds=600)).strftime("%H:%M UTC")
    rate_str   = f"{trade.customer_rate:,.2f}"

    return (
        f"Rate: {rate_str} {local} per {hard}\n"
        f"Expires at: {expiry_str}\n\n"
        f"INDICATIVE - not committed until locked. Reply CONFIRM to lock this in, "
        f"or reply with a counter-rate if you'd like to negotiate."
    )


def _format_trade_summary(trade: Trade) -> str:
    pair        = trade.currency_pair
    hard, local = pair.split("/")
    hard_amount  = f"{trade.volume_usd:,.2f}"
    local_amount = f"{trade.customer_rate * trade.volume_usd:,.2f}" if trade.customer_rate and trade.volume_usd else "-"
    lock_time    = trade.locked_at or datetime.utcnow()
    expiry_str   = (lock_time + timedelta(seconds=600)).strftime("%H:%M UTC")

    return (
        f"Trade Summary\n"
        f"Trade Amount: {hard} {hard_amount}\n"
        f"Amount in local: {local} {local_amount}\n"
        f"Agreed Rate: {trade.customer_rate:,.2f}\n\n"
        f"Account Details\n"
        f"Name: {ZUBA_ACCOUNT['account_name']}\n"
        f"Account number: {ZUBA_ACCOUNT['account_number']}\n"
        f"Bank: {ZUBA_ACCOUNT['bank_name']}\n\n"
        f"Rate locked for 10 minutes. Please send funds before {expiry_str}."
    )


def _format_negotiation_message(trade: Trade) -> str:
    pair = trade.currency_pair
    hard, local = pair.split("/")
    expiry_str = (trade.quote_time + timedelta(seconds=600)).strftime("%H:%M UTC")

    if trade.lp_negotiation_accepted and trade.lp_client_counter is not None:
        rate = trade.lp_client_counter
        note = f"I can do {rate:,.2f} {local} per {hard}."
    else:
        rate = trade.lp_min_customer_rate
        note = f"Best I can do is {rate:,.2f} {local} per {hard}."

    return (
        f"{note}\n"
        f"Expires at: {expiry_str}\n\n"
        f"Reply CONFIRM to lock this in."
    )


def _format_beneficiary_request(trade: Trade) -> str:
    return (
        f"Please provide your beneficiary details:\n\n"
        f"Beneficiary name:\n"
        f"Account number:\n"
        f"SWIFT/IBAN:\n"
        f"Address:"
    )


def _format_final_summary(trade: Trade, customer: Customer) -> str:
    pair        = trade.currency_pair
    hard, local = pair.split("/")
    hard_amount  = f"{trade.volume_usd:,.2f}"
    local_amount = f"{trade.customer_rate * trade.volume_usd:,.2f}" if trade.customer_rate and trade.volume_usd else "-"

    # Strip any Amount line from beneficiary details - it's already shown above
    beneficiary_block = ""
    if trade.beneficiary_details:
        lines = [
            l for l in trade.beneficiary_details.splitlines()
            if not l.strip().upper().startswith("AMOUNT")
        ]
        beneficiary_block = "\n".join(lines).strip()

    return (
        f"TRADE SUMMARY\n\n"
        f"Trade ID: {trade.trade_id}\n"
        f"Output Currency: {hard}\n"
        f"Output Amount: {hard_amount}\n"
        f"Input Currency: {local}\n"
        f"Input Amount: {local_amount}\n"
        f"Exchange Rate: {trade.customer_rate:,.2f}\n"
        f"Name of customer: {customer.name}\n\n"
        f"{beneficiary_block}"
    )


# --- Claude for conversational states only ---

def _claude_reply(session: dict, customer: Customer, trade: Trade) -> str:
    # Use LP's improved rate as the floor if negotiation has happened
    if trade.lp_counter_rate and trade.markup_bps:
        min_rate = trade.lp_min_customer_rate
    elif trade.lp_rate and trade.markup_bps:
        min_rate = min_acceptable_rate(trade.lp_rate, trade.markup_bps)
    else:
        min_rate = None

    pair = trade.currency_pair or "-"

    # LP negotiation context: only present when a counter has been processed
    if trade.lp_counter_rate is not None:
        lp_context = (
            f"\nLP negotiation result: {'ACCEPTED' if trade.lp_negotiation_accepted else 'DECLINED'}"
            f"\nMin rate you can offer: {trade.lp_min_customer_rate}"
        )
    else:
        lp_context = ""

    system = f"""You are the Zuba OTC trade desk agent handling WhatsApp enquiries.

## Context
Customer type: {customer.customer_type.value}
Trade ID: {trade.trade_id}
State: {trade.state.value}
Pair: {pair}
Volume: {'${:,.0f}'.format(trade.volume_usd) if trade.volume_usd else 'not yet known'}
Customer rate: {trade.customer_rate or 'not yet quoted'}
Min acceptable rate (floor): {min_rate or 'n/a'}{lp_context}

## Instructions
You handle two situations only:

1. ENQUIRY: Customer hasn't given you the hard currency or volume yet.
   - Ask only for what is missing.
   - Do not ask about funding currency - assume NGN.
   - Do not ask about currency variants - use what they state.
   - Once you have both, ask only for anything still missing. The quote will be sent automatically.

2. NEGOTIATING: Customer has pushed back on the quoted rate.
   - We have already checked with our LP. Use the LP negotiation result above.
   - If ACCEPTED: say "I can do [client's counter]. Reply CONFIRM to lock this in."
   - If DECLINED: say "Best I can do is [min rate]. Would you like to proceed at that rate?"
   - Never reveal the LP rate or our markup.

Keep all replies short. No bullet points. No bold text."""

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=system,
        messages=session["history"],
    )
    return response.content[0].text.strip()


# --- Main entry point ---

def handle_message(phone_number: str, message: str) -> str:
    session  = _get_or_create_session(phone_number)
    customer = session["customer"]
    session["last_message_time"] = datetime.utcnow()

    # If the previous trade is complete, start a fresh one for the new enquiry
    if session["trade"] and session["trade"].state == TradeState.SUMMARY_POSTED:
        session["trade"] = None

    trade = _get_or_create_trade(session)

    # Add user message to history
    session["history"].append({"role": "user", "content": message})

    # Step 1: ENQUIRY - extract pair/volume, ping LP when both known
    if trade.state == TradeState.ENQUIRY:
        extracted = _extract_pair_and_volume(message)
        if extracted["pair"] and not trade.currency_pair:
            trade.currency_pair = extracted["pair"]
        if extracted["volume"] and not trade.volume_usd:
            trade.volume_usd = extracted["volume"]
        if trade.currency_pair and trade.volume_usd:
            trade.sensitivity_score = detect_sensitivity(message, customer.customer_type)
            _initiate_lp_request(trade, phone_number)
            if trade.state == TradeState.RATE_QUOTED:
                reply = _format_quote_message(trade)
                session["history"].append({"role": "assistant", "content": reply})
                return reply
            if trade.state == TradeState.COMPLIANCE_REVIEW:
                reply = "We're reviewing your enquiry and will be in touch shortly."
                session["history"].append({"role": "assistant", "content": reply})
                return reply
            return ""  # Async LP path only
        # else: fall through to Claude to ask for missing info

    # Step 1b: COMPLIANCE_REVIEW - trade held, ignore all messages until resolved
    if trade.state == TradeState.COMPLIANCE_REVIEW:
        return ""

    # Step 1c: AWAITING_LP_RATE - LP pinged, waiting for response
    if trade.state == TradeState.AWAITING_LP_RATE:
        extracted = _extract_pair_and_volume(message)
        if extracted["volume"] or extracted["pair"]:
            # Client clarified - update and re-ping LP
            if extracted["pair"]:
                trade.currency_pair = extracted["pair"]
            if extracted["volume"]:
                trade.volume_usd = extracted["volume"]
            trade.sensitivity_score = detect_sensitivity(message, customer.customer_type)
            _initiate_lp_request(trade, phone_number)
            return ""  # No reply - client waits for LP response
        # No useful info: ignore, send no reply
        return ""

    # Step 2: Check rate expiry - full trade reset so client provides fresh details
    if trade.quote_time and is_rate_expired(trade.quote_time):
        if trade.state in (TradeState.RATE_QUOTED, TradeState.NEGOTIATING):
            session["trade"] = None  # Full reset - fresh Trade created on next message
            reply = "Your rate has expired. Please send a fresh enquiry with the pair and amount."
            session["history"].append({"role": "assistant", "content": reply})
            return reply

    # Step 2b: Detect new enquiry intent in RATE_QUOTED/NEGOTIATING
    if trade.state in (TradeState.RATE_QUOTED, TradeState.NEGOTIATING) and _is_new_enquiry(message):
        session["trade"] = None
        reply = "Got it - starting a fresh enquiry. Please send the pair and amount."
        session["history"].append({"role": "assistant", "content": reply})
        return reply

    # Step 3: Route to Python-formatted messages or Claude

    # CONFIRM received - fix negotiated rate, send pre-funding trade summary to client
    if _is_confirmation(message) and trade.state in (TradeState.RATE_QUOTED, TradeState.NEGOTIATING):
        # Fix negotiated rate: update customer_rate to the agreed negotiated rate
        if trade.state == TradeState.NEGOTIATING:
            if trade.lp_negotiation_accepted and trade.lp_client_counter is not None:
                trade.customer_rate = trade.lp_client_counter
            elif trade.lp_min_customer_rate is not None:
                trade.customer_rate = trade.lp_min_customer_rate
        trade.state             = TradeState.LOCKED_IN
        trade.locked_at         = datetime.utcnow()
        trade.trade_summary_sent = True
        reply = _format_trade_summary(trade)
        session["history"].append({"role": "assistant", "content": reply})
        return reply

    # Any message after trade summary - send beneficiary request and await details
    if trade.state == TradeState.LOCKED_IN:
        trade.state = TradeState.AWAITING_BENEFICIARY
        reply = _format_beneficiary_request(trade)
        session["history"].append({"role": "assistant", "content": reply})
        return reply

    # Beneficiary details received - post final summary to Slack only, simple reply to client
    if trade.state == TradeState.AWAITING_BENEFICIARY:
        trade.beneficiary_details = message
        trade.state = TradeState.SUMMARY_POSTED
        slack.post_trade_summary(trade, customer)
        reply = "Trade booked. We'll process your payment shortly."
        session["history"].append({"role": "assistant", "content": reply})
        return reply

    # Counter-rate in RATE_QUOTED/NEGOTIATING: ping LP, return formatted reply with expiry
    if trade.state in (TradeState.RATE_QUOTED, TradeState.NEGOTIATING):
        if not _is_confirmation(message) and not _is_acknowledgement(message):
            counter = _extract_counter_rate(message, trade.customer_rate)
            if counter is not None and trade.lp_name and trade.lp_rate:
                neg = lp_comms.request_negotiation(
                    lp_name=trade.lp_name,
                    pair=trade.currency_pair,
                    volume=trade.volume_usd,
                    client_counter=counter,
                    original_lp_rate=trade.lp_rate,
                )
                trade.lp_client_counter      = counter
                trade.lp_counter_rate        = neg["lp_best_rate"]
                trade.lp_min_customer_rate   = neg["min_customer_rate"]
                trade.lp_negotiation_accepted = neg["accepted"]
                trade.quote_time             = datetime.utcnow()
                trade.state = TradeState.NEGOTIATING
                reply = _format_negotiation_message(trade)
                session["history"].append({"role": "assistant", "content": reply})
                return reply

    # All other states: Claude handles conversationally
    reply = _claude_reply(session, customer, trade)
    session["history"].append({"role": "assistant", "content": reply})
    return reply


# --- Compliance review: approve / reject ---

def approve_trade(trade_id: str) -> Optional[tuple[str, str]]:
    """
    Release a compliance-held trade. Called from /trade/approve endpoint.
    Returns (client_phone, quote_message) to send to client, or None if not found.
    """
    client_phone = _pending_compliance.pop(trade_id, None)
    if not client_phone:
        return None
    session = sessions.get(client_phone)
    if not session or not session["trade"] or session["trade"].trade_id != trade_id:
        return None
    trade = session["trade"]
    trade.state = TradeState.RATE_QUOTED
    quote = _format_quote_message(trade)
    session["history"].append({"role": "assistant", "content": quote})
    return (client_phone, quote)


def reject_trade(trade_id: str) -> Optional[str]:
    """
    Reject a compliance-held trade. Called from /trade/reject endpoint.
    Returns client_phone to send rejection message to, or None if not found.
    """
    client_phone = _pending_compliance.pop(trade_id, None)
    if not client_phone:
        return None
    session = sessions.get(client_phone)
    if session and session["trade"] and session["trade"].trade_id == trade_id:
        session["trade"].state = TradeState.SUMMARY_POSTED  # close session
    return client_phone


# --- Proactive rate expiry cancellation ---

def cancel_expired_trades() -> list[tuple[str, str]]:
    """
    Check all sessions for trades where the quote has expired and the client
    has not messaged since the quote was sent. Called by the scheduler every 30s.
    Returns list of (phone_number, message) pairs to send via Twilio.
    """
    to_cancel = []
    for phone_number, session in list(sessions.items()):
        trade = session.get("trade")
        if not trade:
            continue
        if trade.state not in (TradeState.RATE_QUOTED, TradeState.NEGOTIATING):
            continue
        if not trade.quote_time or not is_rate_expired(trade.quote_time):
            continue
        last_msg = session.get("last_message_time")
        if last_msg and last_msg > trade.quote_time:
            # Client messaged after the quote - reactive expiry check handles this
            continue
        # Proactive cancellation
        session["trade"] = None
        msg = (
            "Your rate has expired and the trade has been cancelled. "
            "Please do not send funds. Message us when you are ready to proceed and we will reconfirm rates."
        )
        session["history"].append({"role": "assistant", "content": msg})
        to_cancel.append((phone_number, msg))
    return to_cancel
