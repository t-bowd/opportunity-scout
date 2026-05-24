"""
Evaluates this week's top opportunities and opens paper positions.
Runs daily after scoring.

Entry filters (all must pass):
  1. Score ≥ 16/20
  2. Underlying signal filed within tiered recency window for the pattern
  3. Fewer than 5 open positions
  4. No duplicate ticker already open
  5. Price fetchable from Yahoo Finance
  6. Price has not moved >8% since scoring (avoids chasing)
  7. Position size yields at least 1 share at $200 AUD target
"""

import requests
from datetime import date, timedelta

from db.client import (
    get_top_opportunities,
    get_open_paper_positions,
    insert_paper_position,
    insert_paper_skipped,
    paper_position_exists_for_opportunity,
)

# Days since the opportunity was scored — proxy for signal recency.
# Urgent patterns require the score to be fresh; slower patterns tolerate older data.
RECENCY_WINDOWS: dict[str, int] = {
    "s1_filed":       2,
    "activist":       2,
    "insider_buy":    5,
    "smart_money":    5,
    "thematic_etf":   5,
    "etf_launch":     5,
    "pre_ipo_proxy":  5,
    "spin_off":       7,
    "index_inclusion": 7,
}
DEFAULT_RECENCY = 5

MIN_SCORE = 16
MAX_POSITIONS = 5
POSITION_SIZE_AUD = 200.0
MAX_PRICE_MOVE_PCT = 8.0
SLIPPAGE_PCT = 0.5          # 0.5% worse than close on entry
US_BROKERAGE_AUD = 1.0      # IBKR ~$1 AUD round trip; charge on entry
ASX_BROKERAGE_AUD = 0.0     # CMC Markets free under $1k AUD


def _fetch_price(ticker: str) -> float | None:
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            "?interval=1d&range=5d"
        )
        resp = requests.get(
            url, headers={"User-Agent": "OpportunityScout"}, timeout=10
        )
        return resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except Exception:
        return None


def _fetch_fx_rate() -> float:
    """USD per 1 AUD. e.g. 0.645 means $1 AUD = $0.645 USD."""
    try:
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/AUDUSD=X"
            "?interval=1d&range=5d"
        )
        resp = requests.get(
            url, headers={"User-Agent": "OpportunityScout"}, timeout=10
        )
        return resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except Exception:
        print("[paper/entry] FX rate fetch failed, using fallback 0.65")
        return 0.65


def _recency_ok(opp: dict) -> tuple[bool, str]:
    """True if the opportunity is fresh enough for its pattern type."""
    pattern = opp.get("pattern", "")
    window = RECENCY_WINDOWS.get(pattern, DEFAULT_RECENCY)
    cutoff = date.today() - timedelta(days=window)

    created_str = opp.get("created_at", "")
    if not created_str:
        return True, ""  # can't check, allow through

    try:
        scored_on = date.fromisoformat(created_str[:10])
        if scored_on < cutoff:
            return False, f"stale:{pattern}_window_{window}d_scored_{scored_on}"
    except Exception:
        pass
    return True, ""


def run_entries(week_of: str | None = None) -> None:
    if week_of is None:
        today = date.today()
        week_of = (today - timedelta(days=today.weekday())).isoformat()

    # Fetch this week's top 10 (we filter down to 5 after gates)
    opportunities = get_top_opportunities(week_of, limit=10)
    open_positions = get_open_paper_positions()
    open_count = len(open_positions)
    open_tickers = {p["ticker"] for p in open_positions}

    if open_count >= MAX_POSITIONS:
        print(f"[paper/entry] {open_count} positions open — no slots available, skipping")
        return

    fx_rate = _fetch_fx_rate()
    entered = 0

    for opp in opportunities:
        ticker = opp.get("vehicle", "")
        score = opp.get("total_score", 0)
        opp_id = opp["id"]

        def skip(reason: str) -> None:
            insert_paper_skipped({
                "opportunity_id": opp_id,
                "ticker": ticker,
                "score": score,
                "skip_reason": reason,
                "week_of": week_of,
            })
            print(f"[paper/entry] SKIP {ticker} (score {score}) — {reason}")

        # 1. Score gate
        if score < MIN_SCORE:
            skip(f"score_{score}_below_{MIN_SCORE}")
            continue

        # 2. Position cap
        if open_count >= MAX_POSITIONS:
            skip("max_positions_reached")
            break  # no point checking further opportunities

        # 3. Duplicate ticker
        if ticker in open_tickers:
            skip("ticker_already_open")
            continue

        # 4. Already entered this exact opportunity
        if paper_position_exists_for_opportunity(opp_id):
            continue  # silent — normal on re-runs

        # 5. Recency
        ok, reason = _recency_ok(opp)
        if not ok:
            skip(reason)
            continue

        # 6. Fetch current price
        current_price = _fetch_price(ticker)
        if current_price is None:
            skip("price_fetch_failed")
            continue

        # 7. Price movement since scoring
        price_at_score = opp.get("price_at_score")
        if price_at_score and float(price_at_score) > 0:
            move_pct = (
                abs(current_price - float(price_at_score)) / float(price_at_score) * 100
            )
            if move_pct > MAX_PRICE_MOVE_PCT:
                skip(f"price_moved_{move_pct:.1f}pct")
                continue

        # Build position
        is_asx = ticker.endswith(".AX")

        if is_asx:
            # Yahoo Finance returns ASX prices in AUD
            entry_price_aud = round(current_price * (1 + SLIPPAGE_PCT / 100), 4)
            entry_price_usd = round(entry_price_aud * fx_rate, 4)
            brokerage = ASX_BROKERAGE_AUD
            market = "ASX"
        else:
            # US prices in USD, convert to AUD
            entry_price_usd = round(current_price * (1 + SLIPPAGE_PCT / 100), 4)
            entry_price_aud = round(entry_price_usd / fx_rate, 4)
            brokerage = US_BROKERAGE_AUD
            market = "US"

        # 8. Minimum 1 share
        quantity = int(POSITION_SIZE_AUD / entry_price_aud)
        if quantity < 1:
            skip(f"price_too_high_aud:{entry_price_aud:.2f}")
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
            "entry_date": date.today().isoformat(),
            "entry_week_of": week_of,
            "score_at_entry": score,
            "status": "open",
        }

        insert_paper_position(pos)
        open_count += 1
        open_tickers.add(ticker)
        entered += 1

        cost_aud = entry_price_aud * quantity + brokerage
        print(
            f"[paper/entry] ENTER {ticker} ({market}) @ "
            f"${entry_price_aud:.2f} AUD × {quantity} = "
            f"${cost_aud:.2f} AUD (score {score}/20)"
        )

    print(
        f"[paper/entry] done — {entered} entered, "
        f"{open_count}/{MAX_POSITIONS} positions open"
    )
