"""
Evaluates recent top opportunities (last 10 days) and opens paper positions.
Runs daily after scoring.

Entry filters (all must pass):
  1.  Score ≥ 13/20 (raised to 15 in a bearish market regime). Lowered from 16
      once liquidity stopped being a heavy score penalty; the market-cap floor
      that guards quality now lives at score time in analyzer/score.py.
  2.  Underlying signal filed within tiered recency window for the pattern
  3.  Fewer than MAX_POSITIONS open positions
  4.  No duplicate ticker already open
  5.  Not already entered for this exact opportunity
  6.  Price fetchable from Yahoo Finance
  7.  Price has not moved >8% since scoring (avoids chasing)
  8.  Not within 7 days of scheduled earnings (avoids binary event risk)
  8b. Not a falling knife (at/near 52-week low or deep unrecovered drawdown),
      unless it's a multi-insider conviction cluster
  8c. Sector not already at MAX_SECTOR_POSITIONS open positions (SIC major group)
  9.  Relative volume ≥ 1.5× for news/thematic signals (EDGAR signals exempt —
      the filing is the signal, not today's tape; liquidity gated at score time)
  10. Position size yields at least 1 share at $200 AUD target
"""

import requests
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from collectors.edgar import get_sector_key
from paper_trader.notify import notify_opened
from db.client import (
    get_recent_opportunities,
    get_open_paper_positions,
    insert_paper_position,
    insert_paper_skipped,
    paper_position_exists_for_opportunity,
    auto_fill_feedback_entry,
    count_recent_insider_buyers,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECENCY_WINDOWS: dict[str, int] = {
    "s1_filed":        2,
    "activist":        2,
    "insider_buy":     5,
    "smart_money":     5,
    "thematic_etf":    5,
    "etf_launch":      5,
    "pre_ipo_proxy":   5,
    "spin_off":        7,
    "index_inclusion": 7,
}
DEFAULT_RECENCY = 5

# Score gate. Lowered from 16 once liquidity stopped being a heavy score penalty
# (see analyzer/score.py): a quality small-cap insider buy now lands ~13-15, and
# at 16 those were being filtered out entirely — the main reason the pool was dry.
# The full stack of downstream entry filters (recency, volume, earnings blackout,
# price-move, dollar-volume floor at score time) does the quality work.
MIN_SCORE = 13
BEARISH_MIN_SCORE = 15          # higher bar when broad market is in drawdown
MARKET_REGIME_THRESHOLD = 0.90  # >10% below 52w high = bearish
MAX_POSITIONS = 10              # secondary cap; the $2,000 pool is the real limit

# Conviction-scaled position sizing. Total paper capital is a $2,000 pool; the
# default trade is ~$200, but stronger-conviction picks get a larger slice as
# long as budget remains. Highest-scoring opportunities are processed first
# (get_recent_opportunities orders by score desc), so they get capital priority.
TOTAL_POOL_AUD = 2000.0
BASE_POSITION_AUD = 200.0
MIN_TRADE_AUD = 150.0           # don't open a position smaller than this
SIZE_TIERS = [(18, 400.0), (16, 300.0)]  # score >= threshold -> target size; else BASE
MAX_PRICE_MOVE_PCT = 8.0
EARNINGS_BLACKOUT_DAYS = 7
SLIPPAGE_PCT = 0.5
US_BROKERAGE_AUD = 1.0
ASX_BROKERAGE_AUD = 0.0

# Relative-volume gate applies to news/thematic signals only — those are current
# and the market should react same-day. EDGAR signals are driven by the filing,
# not today's tape, so they are exempt (liquidity is gated by the avg-dollar-volume
# floor at score time instead).
EDGAR_PATTERNS = {"insider_buy", "smart_money", "s1_filed", "activist", "etf_launch", "spin_off"}
MIN_RELATIVE_VOLUME_NEWS = 1.5   # news/thematic: must be 1.5× avg

# Falling-knife guard: skip picks in a sustained downtrend (pinned to the 52-week
# low, or a deep unrecovered drawdown). Buying these underperforms even with an
# insider signal — EXCEPT a genuine multi-insider conviction cluster, which is
# allowed to override the block.
FALLING_KNIFE_ABOVE_LOW_PCT = 10.0       # within 10% of 52w low = downtrend
FALLING_KNIFE_DEEP_DD_PCT = -40.0        # >=40% below 52w high ...
FALLING_KNIFE_DEEP_DD_ABOVE_LOW_PCT = 25.0  # ... and not meaningfully recovered
CLUSTER_MIN_BUYERS = 2                    # distinct insiders to override the block

# Sector diversification — cap open positions per SIC major group. Insider buying
# clusters by sector (right now: regional banks), so without this the book could
# load up on one industry. None for tickers SEC doesn't classify (e.g. ASX).
MAX_SECTOR_POSITIONS = 3

HEADERS = {"User-Agent": "OpportunityScout"}

# ---------------------------------------------------------------------------
# Helpers — all fail open (return None/False) so data issues don't block trades
# ---------------------------------------------------------------------------

def _fetch_fx_rate() -> float:
    """USD per 1 AUD. Falls back to 0.65 on failure."""
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/AUDUSD=X?interval=1d&range=5d"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        return resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except Exception:
        print("[paper/entry] FX rate fetch failed, using fallback 0.65")
        return 0.65


def _fetch_price_and_earnings(ticker: str) -> tuple[float | None, int | None]:
    """
    Returns (current_price, days_to_earnings).
    days_to_earnings is None if unknown, negative if already passed.
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        meta = resp.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        ts = meta.get("earningsTimestamp")
        days = None
        if ts:
            earnings_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            days = (earnings_date - date.today()).days
        return price, days
    except Exception:
        return None, None


def _fetch_relative_volume(ticker: str) -> float | None:
    """
    Most recent complete session's volume / trailing average. Returns None if
    unavailable. Drops zero-volume bars — the daily cron runs around US market
    close, so the latest bar is often empty/incomplete and would otherwise read
    as 0.00x and spuriously fail the filter.
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1mo"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        volumes = resp.json()["chart"]["result"][0]["indicators"]["quote"][0].get("volume", [])
        volumes = [v for v in volumes if v]  # drop None and 0 (incomplete bars)
        if len(volumes) < 5:
            return None
        avg = sum(volumes[:-1]) / len(volumes[:-1])
        return volumes[-1] / avg if avg > 0 else None
    except Exception:
        return None


def _is_falling_knife(ticker: str) -> bool:
    """
    True if the stock is in a sustained downtrend: trading within
    FALLING_KNIFE_ABOVE_LOW_PCT of its 52-week low, or in a deep drawdown that
    hasn't recovered. Fails open (returns False) on any data issue.
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1y"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        data = resp.json()["chart"]["result"][0]
        closes = [c for c in data["indicators"]["quote"][0].get("close", []) if c]
        price = data["meta"].get("regularMarketPrice")
        if not closes or not price:
            return False
        hi, lo = max(closes), min(closes)
        from_high = (price - hi) / hi * 100
        above_low = (price - lo) / lo * 100
        near_low = above_low <= FALLING_KNIFE_ABOVE_LOW_PCT
        deep_dd = from_high <= FALLING_KNIFE_DEEP_DD_PCT and above_low <= FALLING_KNIFE_DEEP_DD_ABOVE_LOW_PCT
        return near_low or deep_dd
    except Exception:
        return False  # fail open — don't block a trade on a data hiccup


def _market_is_bearish(is_asx: bool) -> bool:
    """True if the relevant index is >10% below its 52-week high."""
    index = "^AXJO" if is_asx else "SPY"
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{index}?interval=1d&range=1y"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        data = resp.json()["chart"]["result"][0]
        closes = [c for c in data["indicators"]["quote"][0].get("close", []) if c]
        price = data["meta"].get("regularMarketPrice")
        if closes and price:
            return price < max(closes) * MARKET_REGIME_THRESHOLD
    except Exception:
        pass
    return False  # fail open


def _target_position_size(score: int) -> float:
    """Conviction-scaled target trade size in AUD (capped by budget at call site)."""
    for threshold, size in SIZE_TIERS:
        if score >= threshold:
            return size
    return BASE_POSITION_AUD


def _recency_ok(opp: dict) -> tuple[bool, str]:
    """True if the opportunity is fresh enough for its pattern type."""
    pattern = opp.get("pattern", "")
    window = RECENCY_WINDOWS.get(pattern, DEFAULT_RECENCY)
    cutoff = date.today() - timedelta(days=window)
    created_str = opp.get("created_at", "")
    if not created_str:
        return True, ""
    try:
        scored_on = date.fromisoformat(created_str[:10])
        if scored_on < cutoff:
            return False, f"stale:{pattern}_window_{window}d_scored_{scored_on}"
    except Exception:
        pass
    return True, ""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_entries(week_of: str | None = None) -> None:
    if week_of is None:
        today = date.today()
        week_of = (today - timedelta(days=today.weekday())).isoformat()

    # Pull from the last 10 days rather than the current calendar week so a
    # Friday-scored opportunity is still actionable on Monday. Per-pattern
    # recency windows below still gate how fresh each individual pick must be.
    opportunities = get_recent_opportunities(days=10, limit=25)
    open_positions = get_open_paper_positions()
    open_count = len(open_positions)
    open_tickers = {p["ticker"] for p in open_positions}

    # Budget = the $2,000 pool minus what's already deployed in open positions.
    deployed = sum(
        float(p["entry_price_aud"]) * p["quantity"] + float(p.get("brokerage_aud", 0))
        for p in open_positions
    )
    remaining_budget = TOTAL_POOL_AUD - deployed

    # Sector counts across open positions (for the diversification cap)
    open_sectors: Counter = Counter()
    for p in open_positions:
        sk = get_sector_key(p["ticker"])
        if sk:
            open_sectors[sk] += 1

    if open_count >= MAX_POSITIONS:
        print(f"[paper/entry] {open_count} positions open — no slots, skipping")
        return
    if remaining_budget < MIN_TRADE_AUD:
        print(f"[paper/entry] ${remaining_budget:.0f} budget left of ${TOTAL_POOL_AUD:.0f} — pool full, skipping")
        return

    fx_rate = _fetch_fx_rate()

    # Market regime — compute once per run for each market
    bearish_us = _market_is_bearish(is_asx=False)
    bearish_asx = _market_is_bearish(is_asx=True)
    if bearish_us:
        print(f"[paper/entry] US market regime: BEARISH — min score raised to {BEARISH_MIN_SCORE}")
    if bearish_asx:
        print(f"[paper/entry] ASX market regime: BEARISH — min score raised to {BEARISH_MIN_SCORE}")

    entered = 0

    for opp in opportunities:
        ticker = opp.get("vehicle", "")
        score = opp.get("total_score", 0)
        opp_id = opp["id"]
        is_asx = ticker.endswith(".AX")

        def skip(reason: str) -> None:
            insert_paper_skipped({
                "opportunity_id": opp_id,
                "ticker": ticker,
                "score": score,
                "skip_reason": reason,
                "week_of": week_of,
            })
            print(f"[paper/entry] SKIP {ticker} (score {score}) — {reason}")

        # 1. Score gate (regime-aware)
        bearish = bearish_asx if is_asx else bearish_us
        min_score = BEARISH_MIN_SCORE if bearish else MIN_SCORE
        if score < min_score:
            skip(f"score_{score}_below_{min_score}{'_bearish_regime' if bearish else ''}")
            continue

        # 2. Position cap
        if open_count >= MAX_POSITIONS:
            skip("max_positions_reached")
            break

        # 3. Duplicate ticker
        if ticker in open_tickers:
            skip("ticker_already_open")
            continue

        # 4. Already entered this opportunity
        if paper_position_exists_for_opportunity(opp_id):
            continue  # silent — normal on daily re-runs

        # 5. Recency
        ok, reason = _recency_ok(opp)
        if not ok:
            skip(reason)
            continue

        # 6. Price + earnings date (single API call)
        current_price, days_to_earnings = _fetch_price_and_earnings(ticker)
        if current_price is None:
            skip("price_fetch_failed")
            continue

        # 7. Price movement since scoring
        price_at_score = opp.get("price_at_score")
        if price_at_score and float(price_at_score) > 0:
            move_pct = abs(current_price - float(price_at_score)) / float(price_at_score) * 100
            if move_pct > MAX_PRICE_MOVE_PCT:
                skip(f"price_moved_{move_pct:.1f}pct")
                continue

        # 8. Earnings blackout
        if days_to_earnings is not None and 0 <= days_to_earnings <= EARNINGS_BLACKOUT_DAYS:
            skip(f"earnings_in_{days_to_earnings}d")
            continue

        pattern = opp.get("pattern", "")

        # 8b. Falling-knife guard — don't buy into a sustained downtrend. A genuine
        # multi-insider conviction cluster overrides the block; a lone insider does not.
        if _is_falling_knife(ticker):
            buyers = count_recent_insider_buyers(ticker) if pattern == "insider_buy" else 0
            if buyers >= CLUSTER_MIN_BUYERS:
                print(f"[paper/entry] {ticker} is a falling knife but a {buyers}-insider cluster — allowing")
            else:
                skip("falling_knife")
                continue

        # 8c. Sector diversification — don't over-concentrate in one industry
        sector = get_sector_key(ticker)
        if sector and open_sectors.get(sector, 0) >= MAX_SECTOR_POSITIONS:
            skip(f"sector_full:{sector}")
            continue

        # 9. Relative volume — momentum confirmation for NEWS/THEMATIC signals only.
        # EDGAR signals (insider buys, 13F, activist) are driven by the filing, not
        # today's tape, and the avg-dollar-volume floor at score time already gates
        # liquidity. Applying an intraday relative-volume gate to them wrongly
        # rejected very liquid names (RYAN ~$84M/day, XMTR ~$100M/day) on a merely
        # quiet session — exactly the genuine insider buys we want to enter.
        if pattern not in EDGAR_PATTERNS:
            rel_vol = _fetch_relative_volume(ticker)
            if rel_vol is not None and rel_vol < MIN_RELATIVE_VOLUME_NEWS:
                skip(f"low_relative_volume_{rel_vol:.2f}x")
                continue

        # Build position
        if is_asx:
            entry_price_aud = round(current_price * (1 + SLIPPAGE_PCT / 100), 4)
            entry_price_usd = round(entry_price_aud * fx_rate, 4)
            brokerage = ASX_BROKERAGE_AUD
            market = "ASX"
        else:
            entry_price_usd = round(current_price * (1 + SLIPPAGE_PCT / 100), 4)
            entry_price_aud = round(entry_price_usd / fx_rate, 4)
            brokerage = US_BROKERAGE_AUD
            market = "US"

        # 10. Position sizing — conviction-scaled, capped by remaining budget
        target_aud = min(_target_position_size(score), remaining_budget)
        if target_aud < MIN_TRADE_AUD:
            skip("budget_exhausted")
            break
        quantity = int(target_aud / entry_price_aud)
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
        auto_fill_feedback_entry(opp_id, entry_price_aud)

        cost_aud = entry_price_aud * quantity + brokerage
        remaining_budget -= cost_aud
        open_count += 1
        open_tickers.add(ticker)
        if sector:
            open_sectors[sector] += 1
        entered += 1

        regime_note = " [bearish regime]" if bearish else ""
        print(
            f"[paper/entry] ENTER {ticker} ({market}) @ "
            f"${entry_price_aud:.2f} AUD × {quantity} = ${cost_aud:.2f} AUD "
            f"(score {score}/20{regime_note}) — ${remaining_budget:.0f} budget left"
        )
        notify_opened(ticker, market, entry_price_aud, quantity, cost_aud, score,
                      opp.get("pattern", "unknown"), opp.get("plain_english", ""))

    print(
        f"[paper/entry] done — {entered} entered, {open_count}/{MAX_POSITIONS} open, "
        f"${TOTAL_POOL_AUD - remaining_budget:.0f}/${TOTAL_POOL_AUD:.0f} deployed"
    )
