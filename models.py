from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List
from enum import Enum


class TradeState(Enum):
    ENQUIRY            = "ENQUIRY"
    AWAITING_LP_RATE   = "AWAITING_LP_RATE"
    COMPLIANCE_REVIEW  = "COMPLIANCE_REVIEW"   # Blocked pending human approval
    RATE_QUOTED        = "RATE_QUOTED"
    NEGOTIATING        = "NEGOTIATING"
    LOCKED_IN          = "LOCKED_IN"
    AWAITING_BENEFICIARY = "AWAITING_BENEFICIARY"
    SUMMARY_POSTED     = "SUMMARY_POSTED"


class CustomerType(Enum):
    FI       = "FI"        # Financial institution - lower markup
    MERCHANT = "MERCHANT"  # Merchant - higher markup


@dataclass
class Customer:
    phone_number: str
    name: str = "Unknown"
    customer_type: CustomerType = CustomerType.FI
    kyc_verified: bool = False
    is_new: bool = True  # True until we've seen them before


@dataclass
class Trade:
    trade_id: str
    state: TradeState = TradeState.ENQUIRY
    currency_pair: Optional[str] = None        # e.g. "USDT/NGN"
    volume_usd: Optional[float] = None         # trade size in USD
    counterparty: Optional[str] = None         # customer/company name
    lp_name: Optional[str] = None              # which LP quoted the rate
    lp_rate: Optional[float] = None            # LP's wholesale rate
    markup_bps: Optional[float] = None         # our markup in basis points
    customer_rate: Optional[float] = None      # rate shown to customer
    quote_time: Optional[datetime] = None      # when rate was fetched
    locked_at: Optional[datetime] = None       # when customer confirmed
    compliance_flags: List[str] = field(default_factory=list)
    beneficiary_name: Optional[str] = None
    beneficiary_details: Optional[str] = None
    quote_shown:        bool = False   # True after initial quote message sent to client
    trade_summary_sent: bool = False   # True after trade summary message sent to client
    # Price sensitivity signal detected from opening message (0.0 = none, 1.0 = strong)
    sensitivity_score:     float = 0.0
    # LP negotiation state (populated when client counters)
    lp_counter_rate:       Optional[float] = None  # LP's best achievable rate after negotiation
    lp_min_customer_rate:  Optional[float] = None  # Lowest rate we can offer on LP's best rate
    lp_negotiation_accepted: Optional[bool] = None # Whether LP can support client's counter
    created_at: datetime = field(default_factory=datetime.utcnow)
