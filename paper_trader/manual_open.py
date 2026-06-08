"""
Manually open paper positions for named tickers at the current market price.

Usage:
    python -m paper_trader.manual_open RYAN XMTR FONR
    python -m paper_trader.manual_open ERIE:350      # custom size in AUD

For genuine opportunities the automated entry filters skipped — e.g. a high-priced
name $200 can't buy a share of, or a pick you simply want in now rather than on the
next scheduled run. Each position is linked to the most recent scored opportunity
for that ticker so its thesis and feedback stay consistent with automated entries.

Sizing: with no amount, uses the conviction-scaled size (entry.py) capped by
remaining pool budget, and skips if the pool is full. A TICKER:AMOUNT argument is
a deliberate override and MAY exceed the $2,000 pool — use it for a high-conviction
manual add when the pool is full (e.g. NVRI:300). Going over just means automated
entries pause until positions close and deployed drops back under the pool. Won't
open a ticker that's already open.
"""
import sys
from datetime import date, timedelta

from db.client import (
    get_open_paper_positions,
    get_latest_opportunity_by_ticker,
    paper_position_exists_for_opportunity,
    insert_paper_position,
    auto_fill_feedback_entry,
)
from paper_trader.entry import (
    _fetch_fx_rate,
    _fetch_price_and_earnings,
    _target_position_size,
    SLIPPAGE_PCT,
    US_BROKERAGE_AUD,
    ASX_BROKERAGE_AUD,
    TOTAL_POOL_AUD,
    MIN_TRADE_AUD,
)
from paper_trader.notify import notify_opened


def _parse_arg(arg: str) -> tuple[str, float | None]:
    """'RYAN' -> ('RYAN', None); 'ERIE:350' -> ('ERIE', 350.0)."""
    if ":" in arg:
        t, amt = arg.split(":", 1)
        try:
            return t.upper(), float(amt)
        except ValueError:
            return t.upper(), None
    return arg.upper(), None


def open_positions(args: list[str]) -> None:
    requests_ = [_parse_arg(a) for a in args]

    open_pos = get_open_paper_positions()
    open_tickers = {p["ticker"].upper() for p in open_pos}
    deployed = sum(
        float(p["entry_price_aud"]) * p["quantity"] + float(p.get("brokerage_aud", 0))
        for p in open_pos
    )
    remaining_budget = TOTAL_POOL_AUD - deployed

    today = date.today()
    week_of = (today - timedelta(days=today.weekday())).isoformat()
    fx_rate = _fetch_fx_rate()

    for ticker, override in requests_:
        if ticker in open_tickers:
            print(f"[manual_open] {ticker} already open — skipping")
            continue

        opp = get_latest_opportunity_by_ticker(ticker)
        if not opp:
            print(f"[manual_open] {ticker} has no scored opportunity — skipping (open only scored picks)")
            continue
        opp_id = opp["id"]
        if paper_position_exists_for_opportunity(opp_id):
            print(f"[manual_open] {ticker} opportunity already has a position — skipping")
            continue
        score = opp.get("total_score", 0)

        current_price, _ = _fetch_price_and_earnings(ticker)
        if current_price is None:
            print(f"[manual_open] {ticker} price fetch failed — skipping")
            continue

        is_asx = ticker.endswith(".AX")
        if is_asx:
            entry_price_aud = round(current_price * (1 + SLIPPAGE_PCT / 100), 4)
            entry_price_usd = round(entry_price_aud * fx_rate, 4)
            brokerage, market = ASX_BROKERAGE_AUD, "ASX"
        else:
            entry_price_usd = round(current_price * (1 + SLIPPAGE_PCT / 100), 4)
            entry_price_aud = round(entry_price_usd / fx_rate, 4)
            brokerage, market = US_BROKERAGE_AUD, "US"

        # Sizing. An explicit :AMOUNT is a deliberate override and MAY exceed the
        # pool budget (a manual conviction add) — honour it. Without an amount, use
        # the conviction size capped by remaining budget, and skip if the pool is
        # effectively full (tell the user how to add over budget).
        if override is not None:
            target_aud = override
            over_note = " (over pool budget)" if target_aud > remaining_budget else ""
        else:
            if remaining_budget < MIN_TRADE_AUD:
                print(f"[manual_open] {ticker} — only ${remaining_budget:.0f} pool budget left; "
                      f"pass {ticker}:AMOUNT to add over budget. skipping")
                continue
            target_aud = min(_target_position_size(score), remaining_budget)
            over_note = ""
        quantity = int(target_aud / entry_price_aud)
        if quantity < 1:
            print(f"[manual_open] {ticker} needs ${entry_price_aud:.0f} for 1 share "
                  f"but only ${target_aud:.0f} allocated — skipping")
            continue

        pos = {
            "opportunity_id": opp_id,
            "ticker": ticker,
            "pattern": opp.get("pattern", "unknown"),
            "market": market,
            "entry_price_usd": entry_price_usd,
            "entry_price_aud": entry_price_aud,
            "quantity": quantity,
            "brokerage_aud": brokerage,
            "entry_date": today.isoformat(),
            "entry_week_of": week_of,
            "score_at_entry": score,
            "status": "open",
        }
        insert_paper_position(pos)
        auto_fill_feedback_entry(opp_id, entry_price_aud)

        cost_aud = entry_price_aud * quantity + brokerage
        remaining_budget -= cost_aud
        print(
            f"[manual_open] OPENED {ticker} ({market}) @ ${entry_price_aud:.2f} AUD "
            f"× {quantity} = ${cost_aud:.2f} AUD (score {score}/20){over_note} — "
            f"${remaining_budget:.0f} pool budget left"
        )
        notify_opened(ticker, market, entry_price_aud, quantity, cost_aud, score,
                      opp.get("pattern", "unknown"), opp.get("plain_english", ""))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m paper_trader.manual_open TICKER[:AMOUNT] [...]")
        sys.exit(1)
    open_positions(sys.argv[1:])
