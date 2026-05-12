import re
from datetime import datetime, timezone

import anthropic

from config import ANTHROPIC_API_KEY
from models import Trade, Customer, TradeState, CustomerType
from rates import get_rate, is_rate_expired, seconds_remaining, supported_pairs
from pricing import calculate_markup, apply_markup_to_rate, min_acceptable_rate
from compliance import check_compliance, format_flags_for_message

# --- Client and state ---

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# In-memory session store: phone_number -> {trade, customer, history}
sessions: dict[str, dict] = {}

_trade_counter = 103  # Continues from TRD-103


def _next_trade_id() -> str:
    global _trade_counter
    _trade_counter += 1
    return f"TRD-{_trade_counter}"


# --- Known customers ---
# Add real numbers here once confirmed with Tolu
# Format: "+447700000000": Customer(...)

KNOWN_CUSTOMERS: dict[str, Customer] = {}

HARD_CURRENCIES = ["USDT", "USDC", "USD", "GBP", "EUR", "CAD", "ZAR"]
FUNDING_CURRENCIES = ["NGN", "GHS", "ZAR"]

# Zuba's receiving account - client sends local currency here
ZUBA_ACCOUNT = {
    "account_name":   "Zuba Technologies Ltd",
    "account_number": "7662758412",
    "bank_name":      "Globus Bank",
}


# --- Pre-extraction: pair and volume from raw message ---

def _extract_pair_and_volume(message: str) -> dict:
    """
    Extract currency pair and volume from a user message using regex.
    Called before Claude so we can pre-fetch the rate.

    Returns dict with keys 'pair' (str or None) and 'volume' (float or None).
    """
    result = {"pair": None, "volume": None}
    msg = message.upper()

    # Try explicit pair first: e.g. "USD/NGN", "USDT/GHS"
    pair_match = re.search(
        r'\b(USDT|USDC|USD|GBP|EUR|CAD|ZAR)/(NGN|GHS|ZAR)\b', msg
    )
    if pair_match:
        result["pair"] = pair_match.group(0)
    else:
        # Look for a hard currency alone - default to NGN as funding
        for hc in HARD_CURRENCIES:
            if re.search(rf'\b{hc}\b', msg):
                result["pair"] = f"{hc}/NGN"
                break

    # Extract volume - handles: $100k, 100k, $100,000, 100000, 2.5m
    vol_match = re.search(
        r'\$?\s*([\d,]+(?:\.\d+)?)\s*([kmb]?)\b', msg, re.IGNORECASE
    )
    if vol_match:
        num_str = vol_match.group(1).replace(",", "")
        suffix  = vol_match.group(2).upper()
        try:
            num = float(num_str)
            if suffix == "K":   num *= 1_000
            elif suffix == "M": num *= 1_000_000
            elif suffix == "B": num *= 1_000_000_000
            if num >= 100:  # Ignore small numbers that are likely not volumes
                result["volume"] = num
        except ValueError:
            pass

    return result


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


# --- Rate fetch and price calculation ---

def _fetch_and_apply_rate(trade: Trade, customer: Customer):
    """
    Fetch mock LP rate, calculate markup, set all rate fields on the trade object.
    Called when we have pair + volume and are ready to quote.
    """
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

    # Run compliance check now that we have pair and volume
    flags = check_compliance(trade, customer)
    trade.compliance_flags = flags


# --- System prompt ---

def _build_system_prompt(session: dict) -> str:
    trade:    Trade    = session["trade"]
    customer: Customer = session["customer"]

    # Build trade context block
    if trade is None or trade.state == TradeState.ENQUIRY:
        trade_context = "No rate fetched yet. Waiting for pair and volume."
    else:
        expiry_info = ""
        if trade.quote_time:
            if is_rate_expired(trade.quote_time):
                expiry_info = "⚠️ EXPIRED"
            else:
                secs = seconds_remaining(trade.quote_time)
                expiry_info = f"{secs // 60}m {secs % 60}s remaining"

        min_rate = None
        if trade.lp_rate and trade.markup_bps:
            min_rate = min_acceptable_rate(trade.lp_rate, trade.markup_bps)

        pair = trade.currency_pair or "-"
        local_currency = pair.split("/")[1] if pair and "/" in pair else "local"

        # Pre-calculate local amount for the trade summary
        local_amount = ""
        if trade.customer_rate and trade.volume_usd:
            local_amount = f"{local_currency} {trade.customer_rate * trade.volume_usd:,.2f}"

        trade_context = f"""
Trade ID:             {trade.trade_id}
State:                {trade.state.value}
Pair:                 {pair}
Volume:               {'${:,.0f}'.format(trade.volume_usd) if trade.volume_usd else '-'}
Local amount:         {local_amount or '-'}
Counterparty:         {trade.counterparty or 'not yet captured - ask at lock-in'}
LP:                   {trade.lp_name or '-'}
LP rate:              {trade.lp_rate} {local_currency} per {pair.split('/')[0] if pair and '/' in pair else '-'}
Markup:               {f'{trade.markup_bps:.1f}bps' if trade.markup_bps else '-'}
Customer rate:        {trade.customer_rate} {local_currency} per {pair.split('/')[0] if pair and '/' in pair else '-'}
Min acceptable rate:  {min_rate}
Quote expiry:         {expiry_info or '-'}
trade_summary_sent:   {trade.trade_summary_sent}
Compliance flags:     {', '.join(trade.compliance_flags) if trade.compliance_flags else 'none'}
""".strip()

    return f"""You are the Zuba OTC trade desk agent. Zuba is a stablecoin-native cross-border payments platform.

You handle inbound OTC trade enquiries over WhatsApp on behalf of Tolu, Zuba's head of OTC.

## What Zuba does
Customers (Nigerian businesses, financial institutions, payment companies) send local currency (NGN, GHS) to Zuba. Zuba converts and pays out hard/stable currency (USD, USDT, USDC, GBP, EUR, ZAR, CAD) to international beneficiaries.

## Customer
Type: {customer.customer_type.value} ({'Financial institution - 50bps base markup' if customer.customer_type == CustomerType.FI else 'Merchant - 100bps base markup'})
KYC verified: {customer.kyc_verified}
New counterparty: {customer.is_new}

## Current trade context
{trade_context}

## Your job
Guide the customer from enquiry to locked quote to trade summary. Follow these states in order.

### ENQUIRY
You need two things: what hard currency they want, and the volume.
- If only hard currency is stated, assume NGN as funding currency.
- You do NOT need counterparty name yet - ask for it at lock-in only.
- Once you have both, present the rate from the trade context. Do NOT say you are fetching it - the rate is already provided above. Present it immediately.

### RATE_QUOTED
The rate is already in your context above - do not say you are fetching it.
Present it in exactly this format:

Rate: [CUSTOMER RATE] [LOCAL CURRENCY] per [HARD CURRENCY]
Expires at: [HH:MM UTC]

Indicative - not committed until locked. Reply CONFIRM to lock this in, or reply with a counter-rate if you'd like to negotiate.

Use the expiry time from context. Format as HH:MM UTC only - no additional text.

### NEGOTIATING
Customer has pushed back on rate.
- The minimum acceptable rate is shown in context (min_acceptable).
- If customer's target is at or above min_acceptable, you may offer it.
- If below, hold firm: explain you cannot go lower without losing money on the trade.
- Once customer accepts, move to LOCKED_IN and send the trade summary.

### LOCKED_IN - step 1: trade summary
Customer has just confirmed (said CONFIRM or equivalent) AND trade_summary_sent is False.
Signal [STATE:LOCKED_IN] and send exactly this:

Trade Amount:        [HARD CURRENCY] [AMOUNT]
Amount in local:     [LOCAL CURRENCY] [AMOUNT]
Exchange Rate:       [AGREED RATE]

Account to pay:
Account name:        {ZUBA_ACCOUNT["account_name"]}
Account number:      {ZUBA_ACCOUNT["account_number"]}
Bank:                {ZUBA_ACCOUNT["bank_name"]}

Note: rate is locked. Do not send funds until you have received this confirmation.

### LOCKED_IN - step 2: beneficiary request
Client has acknowledged the trade summary (said ok, noted, received, or similar) AND trade_summary_sent is True.
Send exactly this template - leave the fields blank for the client to fill in:

Please provide your beneficiary details:

Beneficiary name:
Account number:
SWIFT/IBAN:
Address:
Amount:     [HARD CURRENCY] [AMOUNT] (pre-fill this from the trade)

### SUMMARY_POSTED
Once the client has sent back their beneficiary details, format and output the internal trade summary, then signal [STATE:SUMMARY_POSTED]:

[TRADE_ID]
Trade Amount:       [HARD CURRENCY] [AMOUNT]
Funding currency:   [LOCAL CURRENCY] [AMOUNT]
Exchange Rate:      [CUSTOMER RATE]
Counterparty:       [NAME if given, else 'unknown']
LP:                 [LP NAME] (rate locked [HH:MM] UTC)
Markup:             [BPS]bps
Quote status:       LOCKED ✅
Beneficiary name:   [NAME]
Beneficiary bank:   [BANK]
SWIFT/IBAN:         [DETAILS]
Account number:     [NUMBER]
Purpose:            [IF STATED, else 'not stated']
Compliance:         [FLAGS or ✅ Clear]

## Compliance
If any compliance flag is shown in the trade context, include this at the end of your reply:
⚠️ COMPLIANCE PAUSE: [flag]. Trade is paused - Tolu and Ali must clear this before funds move.

## Tone
Direct, professional, concise. This is a financial services context.
Do not say you are fetching or checking rates - the data is already in your context.
Do not apologise unnecessarily.
Do not repeat information already stated.
"""


# --- State update from Claude's reply ---

def _update_state_from_reply(session: dict, reply: str):
    """Parse [STATE:X] tag from Claude's reply and update trade state."""
    trade = session["trade"]
    if not trade:
        return
    match = re.search(r"\[STATE:(\w+)\]", reply)
    if match:
        try:
            new_state = TradeState(match.group(1))
            # On first transition to LOCKED_IN, record lock time and mark summary sent
            if new_state == TradeState.LOCKED_IN and trade.state != TradeState.LOCKED_IN:
                trade.locked_at = datetime.utcnow()
                trade.trade_summary_sent = True
            trade.state = new_state
        except ValueError:
            pass


# --- Main entry point ---

def handle_message(phone_number: str, message: str) -> str:
    """
    Receive a WhatsApp message and return a reply.
    Called by main.py on every incoming webhook.
    """
    session  = _get_or_create_session(phone_number)
    trade    = _get_or_create_trade(session)
    customer = session["customer"]

    # Step 1: Pre-extract pair and volume from message before calling Claude.
    # If we find both and haven't quoted yet, fetch the rate immediately so
    # Claude can quote in a single response without any "standby" message.
    if trade.state == TradeState.ENQUIRY:
        extracted = _extract_pair_and_volume(message)
        if extracted["pair"] and not trade.currency_pair:
            trade.currency_pair = extracted["pair"]
        if extracted["volume"] and not trade.volume_usd:
            trade.volume_usd = extracted["volume"]

        if trade.currency_pair and trade.volume_usd and trade.lp_rate is None:
            _fetch_and_apply_rate(trade, customer)

    # Step 2: Check if rate has expired mid-conversation
    if trade.quote_time and is_rate_expired(trade.quote_time):
        if trade.state in (TradeState.RATE_QUOTED, TradeState.NEGOTIATING):
            trade.state = TradeState.ENQUIRY
            trade.lp_rate = None
            trade.customer_rate = None
            trade.quote_time = None

    # Step 3: Add user message to conversation history
    session["history"].append({"role": "user", "content": message})

    # Step 4: Build system prompt with current trade context and call Claude
    system_prompt = _build_system_prompt(session)
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system_prompt,
        messages=session["history"],
    )
    raw_reply = response.content[0].text

    # Step 5: Parse any state updates Claude signals
    _update_state_from_reply(session, raw_reply)

    # Step 6: Store reply in history
    session["history"].append({"role": "assistant", "content": raw_reply})

    # Step 7: Strip control tags and append compliance flags if needed
    clean_reply = re.sub(r"\[STATE:\w+\]\n?", "", raw_reply).strip()

    if trade.compliance_flags and "COMPLIANCE" not in clean_reply:
        clean_reply += f"\n\n{format_flags_for_message(trade.compliance_flags)}"

    return clean_reply
