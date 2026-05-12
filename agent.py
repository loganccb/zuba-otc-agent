import re
from datetime import datetime, timedelta

import anthropic

from config import ANTHROPIC_API_KEY
from models import Trade, Customer, TradeState, CustomerType
from rates import get_rate, is_rate_expired, seconds_remaining
from pricing import calculate_markup, apply_markup_to_rate, min_acceptable_rate
from compliance import check_compliance, format_flags_for_message

# --- Client and state ---

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

sessions: dict[str, dict] = {}
_trade_counter = 103


def _next_trade_id() -> str:
    global _trade_counter
    _trade_counter += 1
    return f"TRD-{_trade_counter}"


# --- Known customers ---
KNOWN_CUSTOMERS: dict[str, Customer] = {}

HARD_CURRENCIES = ["USDT", "USDC", "USD", "GBP", "EUR", "CAD", "ZAR"]

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
            "trade":    None,
            "customer": customer,
            "history":  [],
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
    ("CEDIS",        "GHS"),
    ("CEDI",         "GHS"),
]


def _extract_pair_and_volume(message: str) -> dict:
    result = {"pair": None, "volume": None}
    msg = message.upper()

    # 1. Explicit pair e.g. EUR/NGN
    pair_match = re.search(
        r'\b(USDT|USDC|USD|GBP|EUR|CAD|ZAR)/(NGN|GHS|ZAR)\b', msg
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

        if found:
            result["pair"] = f"{found}/NGN"

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


# --- Rate fetch ---

def _fetch_and_apply_rate(trade: Trade, customer: Customer):
    if not trade.currency_pair or not trade.volume_usd:
        return
    rate_data = get_rate(trade.currency_pair)
    if not rate_data:
        return
    markup        = calculate_markup(customer.customer_type, trade.volume_usd)
    customer_rate = apply_markup_to_rate(rate_data["lp_rate"], markup)
    trade.lp_name       = rate_data["lp_name"]
    trade.lp_rate       = rate_data["lp_rate"]
    trade.markup_bps    = markup
    trade.customer_rate = customer_rate
    trade.quote_time    = datetime.utcnow()
    trade.state         = TradeState.RATE_QUOTED
    trade.compliance_flags = check_compliance(trade, customer)


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

    return (
        f"Trade Summary\n"
        f"Trade Amount: {hard} {hard_amount}\n"
        f"Amount in local: {local} {local_amount}\n"
        f"Agreed Rate: {trade.customer_rate:,.2f}\n\n"
        f"Account Details\n"
        f"Name: {ZUBA_ACCOUNT['account_name']}\n"
        f"Account number: {ZUBA_ACCOUNT['account_number']}\n"
        f"Bank: {ZUBA_ACCOUNT['bank_name']}\n\n"
        f"Rate is locked. Do not send funds until you have received this confirmation."
    )


def _format_beneficiary_request(trade: Trade) -> str:
    return (
        f"Please provide your beneficiary details:\n\n"
        f"Beneficiary name:\n"
        f"Account number:\n"
        f"SWIFT/IBAN:\n"
        f"Address:"
    )


def _format_final_summary(trade: Trade) -> str:
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
        f"Trade ID:         {trade.trade_id}\n"
        f"Output Currency:  {hard}\n"
        f"Output Amount:    {hard_amount}\n"
        f"Input Currency:   {local}\n"
        f"Input Amount:     {local_amount}\n"
        f"Exchange Rate:    {trade.customer_rate:,.2f}\n"
        f"Name of customer: {trade.counterparty or 'unknown'}\n\n"
        f"{beneficiary_block}"
    )


# --- Claude for conversational states only ---

def _claude_reply(session: dict, customer: Customer, trade: Trade) -> str:
    min_rate = None
    if trade.lp_rate and trade.markup_bps:
        min_rate = min_acceptable_rate(trade.lp_rate, trade.markup_bps)

    pair = trade.currency_pair or "-"

    system = f"""You are the Zuba OTC trade desk agent handling WhatsApp enquiries.

## Context
Customer type: {customer.customer_type.value}
Trade ID: {trade.trade_id}
State: {trade.state.value}
Pair: {pair}
Volume: {'${:,.0f}'.format(trade.volume_usd) if trade.volume_usd else 'not yet known'}
Customer rate: {trade.customer_rate or 'not yet quoted'}
Min acceptable rate (floor): {min_rate or 'n/a'}

## Instructions
You handle two situations only:

1. ENQUIRY: Customer hasn't given you the hard currency or volume yet.
   - Ask only for what is missing.
   - Do not ask about funding currency - assume NGN.
   - Do not ask about currency variants - use what they state.
   - Once you have both, ask only for anything still missing. The quote will be sent automatically.

2. NEGOTIATING: Customer has pushed back on the quoted rate.
   - If their counter is at or above {min_rate or 'the floor'}: accept and say "Confirmed at [rate]. Reply CONFIRM to lock this in."
   - If below the floor: decline firmly. "Best I can do is {min_rate}. Would you like to proceed at that rate?"
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
    trade    = _get_or_create_trade(session)
    customer = session["customer"]

    # Add user message to history
    session["history"].append({"role": "user", "content": message})

    # Step 1: Pre-extract pair/volume and fetch rate if in ENQUIRY
    if trade.state == TradeState.ENQUIRY:
        extracted = _extract_pair_and_volume(message)
        if extracted["pair"] and not trade.currency_pair:
            trade.currency_pair = extracted["pair"]
        if extracted["volume"] and not trade.volume_usd:
            trade.volume_usd = extracted["volume"]
        if trade.currency_pair and trade.volume_usd and not trade.lp_rate:
            _fetch_and_apply_rate(trade, customer)

    # Step 2: Check rate expiry
    if trade.quote_time and is_rate_expired(trade.quote_time):
        if trade.state in (TradeState.RATE_QUOTED, TradeState.NEGOTIATING):
            trade.state      = TradeState.ENQUIRY
            trade.lp_rate    = None
            trade.customer_rate = None
            trade.quote_time = None
            reply = "Your rate has expired. Please send your enquiry again and I'll fetch a fresh quote."
            session["history"].append({"role": "assistant", "content": reply})
            return reply

    # Step 3: Route to Python-formatted messages or Claude

    # Quote message - return as soon as rate is available and not yet shown
    if trade.state == TradeState.RATE_QUOTED and not trade.quote_shown:
        trade.quote_shown = True
        reply = _format_quote_message(trade)
        session["history"].append({"role": "assistant", "content": reply})
        return reply

    # CONFIRM received - send trade summary
    if _is_confirmation(message) and trade.state in (TradeState.RATE_QUOTED, TradeState.NEGOTIATING):
        trade.state             = TradeState.LOCKED_IN
        trade.locked_at         = datetime.utcnow()
        trade.trade_summary_sent = True
        reply = _format_trade_summary(trade)
        session["history"].append({"role": "assistant", "content": reply})
        return reply

    # Acknowledgement of trade summary - send beneficiary request
    if trade.state == TradeState.LOCKED_IN and trade.trade_summary_sent and _is_acknowledgement(message):
        reply = _format_beneficiary_request(trade)
        session["history"].append({"role": "assistant", "content": reply})
        return reply

    # Beneficiary details received - send final summary
    if trade.state == TradeState.LOCKED_IN and trade.trade_summary_sent and not _is_acknowledgement(message):
        # Assume any non-acknowledgement message in this state is beneficiary details
        trade.beneficiary_details = message
        trade.state = TradeState.SUMMARY_POSTED
        reply = _format_final_summary(trade)
        session["history"].append({"role": "assistant", "content": reply})
        return reply

    # All other states: Claude handles conversationally
    reply = _claude_reply(session, customer, trade)
    session["history"].append({"role": "assistant", "content": reply})
    return reply
