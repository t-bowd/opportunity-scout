"""
Manually close named paper positions at the current market price.

Usage:
    python -m paper_trader.manual_close YUMC FLUT

Closes any OPEN position whose ticker matches (case-insensitive), recording the
exit at current market with the standard slippage and exit_reason='manual' —
same accounting as an automated exit, but status 'closed_manual'.

Deliberately does NOT write a feedback grade: a manual close is an intervention
(e.g. flushing a position opened on a mislabeled signal), not a strategy outcome,
so it shouldn't teach the feedback loop. snapshot.py also excludes 'closed_manual'
from expectancy / win rate / the graduation sample for the same reason.
"""
import sys
from datetime import date, datetime, timezone

from db.client import get_open_paper_positions, close_paper_position
from paper_trader.exit import _fetch_price, _fetch_fx_rate, SLIPPAGE_PCT


def close_positions(tickers: list[str]) -> None:
    wanted = {t.upper() for t in tickers}
    positions = [p for p in get_open_paper_positions() if p["ticker"].upper() in wanted]
    if not positions:
        print(f"[manual_close] no open positions match {sorted(wanted)}")
        return

    fx_rate = _fetch_fx_rate()
    now_iso = datetime.now(timezone.utc).isoformat()
    today = date.today()

    for pos in positions:
        ticker = pos["ticker"]
        current_price = _fetch_price(ticker)
        if current_price is None:
            print(f"[manual_close] {ticker} — price fetch failed, skipping")
            continue

        entry_price_aud = float(pos["entry_price_aud"])
        quantity = pos["quantity"]
        brokerage = float(pos.get("brokerage_aud", 0))
        is_asx = ticker.endswith(".AX")

        if is_asx:
            exit_price_aud = round(current_price * (1 - SLIPPAGE_PCT / 100), 4)
            exit_price_usd = round(exit_price_aud * fx_rate, 4)
        else:
            exit_price_usd = round(current_price * (1 - SLIPPAGE_PCT / 100), 4)
            exit_price_aud = round(exit_price_usd / fx_rate, 4)

        pnl_aud = round((exit_price_aud - entry_price_aud) * quantity - brokerage, 2)
        pnl_pct = round((exit_price_aud - entry_price_aud) / entry_price_aud * 100, 2)

        close_paper_position(pos["id"], {
            "status": "closed_manual",
            "exit_price_usd": exit_price_usd,
            "exit_price_aud": exit_price_aud,
            "exit_date": today.isoformat(),
            "exit_reason": "manual",
            "pnl_aud": pnl_aud,
            "pnl_pct": pnl_pct,
            "updated_at": now_iso,
        })
        print(f"[manual_close] CLOSED {ticker} — {pnl_pct:+.1f}% / ${pnl_aud:+.2f} AUD")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m paper_trader.manual_close TICKER [TICKER ...]")
        sys.exit(1)
    close_positions(sys.argv[1:])
