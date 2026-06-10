"""
Evaluates recent top opportunities (last 10 days) and opens paper positions.
Runs daily after scoring.

Entry filters (all must pass):
  1.  Score ≥ 13/20 (raised to 15 in a bearish market regime). Lowered from 16
      once liquidity stopped being a heavy score penalty; the market-cap floor
      that guards quality now lives at score time in analyzer/score.py.
  2.  Underlying signal filed within tiered recency window for the pattern
  3.  Fewer than MAX_POSITIONS open positions (hard ceiling). The $2k pool is a
      SOFT cap — once full, only score >= HIGH_CONVICTION_SCORE picks may go over it
  4.  No duplicate ticker already open
  5.  Not already entered for this exact opportunity
  6.  Price fetchable from Yahoo Finance
  7.  Price has not moved >8% since scoring (avoids chasing)
  8.  Not within 7 days of scheduled earnings (avoids binary event risk)
  8a. Not a SPAC / unit / warrant (hard block — blank-check shells aren't tradeable picks)
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

# Conviction-scaled position sizing. The $2,000 pool is a SOFT cap (a test budget,
# not a hard risk limit): the default trade is ~$200, stronger picks get more, and
# while budget remains everything is capped by it. Once the pool is full, only
# high-conviction picks (score >= HIGH_CONVICTION_SCORE) may go OVER it — so the
# pool throttles marginal picks but never blocks a standout. MAX_POSITIONS is the
# real hard ceiling. Highest-scoring opportunities are processed first (capital
# priority). (For real money this soft/hard split should become an explicit dollar
# risk limit, not just a position count.)
TOTAL_POOL_AUD = 2000.0
BASE_POSITION_AUD = 200.0
MIN_TRADE_AUD = 150.0           # don't open a position smaller than this
SIZE_TIERS = [(18, 400.0), (16, 300.0)]  # score >= threshold -> target size; else BASE
HIGH_CONVICTION_SCORE = 18      # picks at/above this may enter over the soft pool
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
FALLING_KNIFE_ABOVE_LOW_PCT = 10.0       # within 10% of 52w low ...
FALLING_KNIFE_MIN_DRAWDOWN_PCT = -15.0   # ... AND >=15% off the high (real downtrend, not a flat/SPAC stock)
FALLING_KNIFE_DEEP_DD_PCT = -40.0        # or >=40% below 52w high ...
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
    Last fully-closed session's volume / trailing average. Returns None if
    unavailable. The daily cron runs mid US session, so today's bar is partial —
    we compare the last COMPLETE session (volumes[-2]) against the trailing
    average, rather than the in-progress bar which would understate volume.
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1mo"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        volumes = resp.json()["chart"]["result"][0]["indicators"]["quote"][0].get("volume", [])
        volumes = [v for v in volumes if v]  # drop None and 0 bars
        if len(volumes) < 6:
            return None
        recent = volumes[-2]                       # last fully-closed session
        baseline = sum(volumes[:-2]) / len(volumes[:-2])
        return recent / baseline if baseline > 0 else None
    except Exception:
        return None


def _price_screen(ticker: str) -> tuple[bool, bool]:
    """
    One 1-year fetch, two verdicts: (is_falling_knife, is_spac).

    - falling knife: trading within FALLING_KNIFE_ABOVE_LOW_PCT of the 52-week low
      AND meaningfully off the high, or a deep unrecovered drawdown.
    - spac/unit: a unit/warrant/rights ticker (suffix U/W/R on a multi-letter
      symbol, e.g. IPVVU), or a blank-check shell trading dead flat at ~$10.

    The SPAC check is also a deterministic ENTRY gate — the score-time SPAC filter
    only stops NEW opportunities, so a SPAC scored before that filter (or sitting
    in the pool) could still be entered. Fails open (False, False) on data issues,
    except the ticker-suffix SPAC check which needs no data.
    """
    is_spac = len(ticker) >= 5 and ticker[-1] in ("U", "W", "R")
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1y"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        data = resp.json()["chart"]["result"][0]
        closes = [c for c in data["indicators"]["quote"][0].get("close", []) if c]
        price = data["meta"].get("regularMarketPrice")
        if not closes or not price:
            return False, is_spac
        hi, lo = max(closes), min(closes)
        from_high = (price - hi) / hi * 100
        above_low = (price - lo) / lo * 100
        near_low = above_low <= FALLING_KNIFE_ABOVE_LOW_PCT and from_high <= FALLING_KNIFE_MIN_DRAWDOWN_PCT
        deep_dd = from_high <= FALLING_KNIFE_DEEP_DD_PCT and above_low <= FALLING_KNIFE_DEEP_DD_ABOVE_LOW_PCT
        flat_at_ten = lo > 0 and (hi - lo) / lo < 0.08 and 9.0 <= price <= 11.0
        return (near_low or deep_dd), (is_spac or flat_at_ten)
    except Exception:
        return False, is_spac  # fail open on the knife; keep the suffix SPAC check


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
        # Soft pool is full — don't stop. High-conviction picks (score >=
        # HIGH_CONVICTION_SCORE) may still enter over the pool; marginal ones skip.
        print(f"[paper/entry] soft pool full (${remaining_budget:.0f} of ${TOTAL_POOL_AUD:.0f} left) "
              f"— only score >={HIGH_CONVICTION_SCORE} picks may go over")

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
        knife, is_spac = _price_screen(ticker)

        # 8a. SPAC / unit guard — never trade blank-check shells or units/warrants.
        # Hard block (no override): score-time filter only stops NEW opportunities,
        # so a SPAC scored earlier (e.g. IPVVU) can still reach entry from the pool.
        if is_spac:
            skip("spac_or_unit")
            continue

        # 8b. Falling-knife guard — don't buy into a sustained downtrend. A genuine
        # multi-insider conviction cluster overrides the block; a lone insider does not.
        if knife:
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

        # 10. Position sizing — conviction-scaled. Within the soft pool, cap by
        # remaining budget. Once the pool is full, only high-conviction picks
        # (score >= HIGH_CONVICTION_SCORE) may enter, sized at full conviction and
        # deliberately over the pool; marginal picks skip. MAX_POSITIONS (checked
        # above) is the hard ceiling either way.
        conviction_size = _target_position_size(score)
        if remaining_budget >= MIN_TRADE_AUD:
            target_aud = min(conviction_size, remaining_budget)
        elif score >= HIGH_CONVICTION_SCORE:
            target_aud = conviction_size  # high conviction — go over the soft pool
        else:
            skip("soft_pool_full")
            continue
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
        budget_note = (
            f"${remaining_budget:.0f} budget left" if remaining_budget >= 0
            else f"${-remaining_budget:.0f} over soft pool [high-conviction]"
        )
        print(
            f"[paper/entry] ENTER {ticker} ({market}) @ "
            f"${entry_price_aud:.2f} AUD × {quantity} = ${cost_aud:.2f} AUD "
            f"(score {score}/20{regime_note}) — {budget_note}"
        )
        notify_opened(ticker, market, entry_price_aud, quantity, cost_aud, score,
                      opp.get("pattern", "unknown"), opp.get("plain_english", ""))

    print(
        f"[paper/entry] done — {entered} entered, {open_count}/{MAX_POSITIONS} open, "
        f"${TOTAL_POOL_AUD - remaining_budget:.0f}/${TOTAL_POOL_AUD:.0f} deployed"
    )
