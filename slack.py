"""
slack.py - Slack notifications for LP interactions and trade completions.

Posts to SLACK_WEBHOOK_URL (a DM webhook - no channel or permissions needed).

Three events posted:
  1. LP rate request sent
  2. LP rate received (with customer rate after markup)
  3. Trade complete (full summary with beneficiary)
"""

import logging

import requests

from config import SLACK_WEBHOOK_URL

logger = logging.getLogger(__name__)


def _post(text: str) -> None:
    if not SLACK_WEBHOOK_URL:
        logger.warning("SLACK_WEBHOOK_URL not set - skipping Slack post")
        return
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=5)
        if resp.status_code != 200:
            logger.error(f"Slack post failed: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Slack post error: {e}")


def post_lp_request(trade_id: str, pair: str, volume: float, lp_name: str) -> None:
    hard, _ = pair.split("/")
    _post(
        f":outbox_tray: *LP Rate Request* [{trade_id}]\n"
        f"LP: {lp_name} | Pair: {pair} | Volume: {hard} {volume:,.0f}"
    )


def post_lp_response(trade_id: str, pair: str, lp_name: str, lp_rate: float, customer_rate: float) -> None:
    _post(
        f":inbox_tray: *LP Response* [{trade_id}]\n"
        f"LP: {lp_name} | Pair: {pair} | LP rate: {lp_rate:,.4f} | Customer rate: {customer_rate:,.2f}"
    )


def post_compliance_review(trade, client_phone: str, base_url: str) -> None:
    """
    Notify that a trade is held for compliance review, with approve/reject URLs.
    Currently posts to the DM webhook (SLACK_WEBHOOK_URL).
    TODO: switch to #trading channel once demo is complete.
    TODO: add secret token to approve/reject URLs before going to production.
    """
    pair = trade.currency_pair
    hard, _ = pair.split("/")
    flags_text = "\n".join(f"• {f}" for f in trade.compliance_flags)
    approve_url = f"{base_url}/trade/approve?id={trade.trade_id}"
    reject_url  = f"{base_url}/trade/reject?id={trade.trade_id}"
    _post(
        f":warning: *Compliance Review Required* [{trade.trade_id}]\n"
        f"Client: {client_phone} | Pair: {pair} | Volume: {hard} {trade.volume_usd:,.0f}\n"
        f"Rate: {trade.customer_rate:,.2f} | LP: {trade.lp_name}\n"
        f"Flags:\n{flags_text}\n\n"
        f"*Approve:* {approve_url}\n"
        f"*Reject:* {reject_url}"
    )


def post_trade_summary(trade) -> None:
    pair        = trade.currency_pair
    hard, local = pair.split("/")
    local_amount = (trade.customer_rate * trade.volume_usd) if trade.customer_rate and trade.volume_usd else 0

    flags_line = f"\n:warning: Compliance flags: {', '.join(trade.compliance_flags)}" if trade.compliance_flags else ""
    _post(
        f":white_check_mark: *Trade Complete* [{trade.trade_id}]\n"
        f"Pair: {pair} | {hard} {trade.volume_usd:,.2f} -> {local} {local_amount:,.2f}\n"
        f"Rate: {trade.customer_rate:,.2f} | LP: {trade.lp_name} | LP rate: {trade.lp_rate:,.4f} | Markup: {trade.markup_bps:.0f}bps\n"
        f"Beneficiary: {trade.beneficiary_details or 'not provided'}"
        f"{flags_line}"
    )
