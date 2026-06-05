"""
Scores classified signals into ranked opportunities.
Runs after classify.py has processed the day's signals.
"""
import json
import os
import re
from datetime import date, timedelta
from google import genai
from google.genai import types
from db.client import get_client, insert_opportunity, opportunity_exists

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
MODEL = "gemini-2.5-flash"

PATTERNS_TO_SCORE = {
    "pre_ipo_proxy", "thematic_etf", "etf_launch", "s1_filed",
    "insider_buy", "activist", "smart_money", "spin_off",
}

SCORING_PROMPT = """You are scoring stock market opportunities for a retail investor based in Australia.

You will receive signals from SEC filings (Form 4 insider buys, S-1 IPOs, 13F institutional holdings, 13D activist stakes) and financial news including Australian sources (ASX announcements, SMH, ABC Business).

Each signal names a real company. Your job:
1. Identify the top 5 most actionable opportunities from the signals
2. For each, use the REAL ticker symbol — NYSE/NASDAQ for US stocks, or ASX ticker (e.g. CBA.AX) for Australian stocks. Do NOT invent placeholder names.
3. Score each opportunity

IMPORTANT: Only select opportunities in publicly listed companies with a real, tradeable stock ticker (NYSE, NASDAQ, or ASX). Private companies (e.g. SpaceX, Anthropic, OpenAI, Stripe) cannot be traded and must NOT be selected — skip them entirely. If a signal references a private company and the best angle is a publicly-traded proxy (e.g. a listed supplier or partner), name the proxy company and its ticker instead.

CRITICAL — anchor every pick to its actual signal. The reason to buy IS the signal, not a generic story about the company. Your thesis, catalyst, and plain_english MUST cite the specific concrete fact in the signal:
- insider_buy: name the role of the buyer (director/officer/10% owner), that it was an open-market purchase (code P), and the filing date. e.g. "A director bought shares on the open market on 2026-06-01" — NOT "China's economy is improving".
- smart_money (13F): name the fund and that it disclosed holding the position, and acknowledge the data is up to 45 days old.
- activist: name that an activist disclosed a stake and what they may push for.
- s1_filed: name that the company filed to go public.
Do NOT select a company whose only rationale is generic macro or fundamentals ("favourable regulation", "strong product cycle", "improving conditions") with no specific signal behind it. If the signal is just "a Form 4 exists" and you cannot say anything concrete about the purchase, that is a weak pick — score conviction low or drop it. A vague large-cap bull case is exactly what we do NOT want.

Score each dimension 0-5:
- conviction: How many independent signals support this? Is the insider buying discretionary (open market, code P) or automatic (ESPP/plan)? Discretionary buys score higher. For 13F holdings, remember the data is up to 45 days stale — treat it as confirmation, not a strong standalone signal.
- asymmetry: What is the upside/downside ratio? Use the price context. A stock in a sustained downtrend — down 30%+ from its 52-week high AND trading near its 52-week low — has POOR asymmetry: an insider buying into that is high-risk "catching a falling knife" (insiders are frequently early and the stock keeps falling), so cap asymmetry at 2 unless there is a specific, dated stabilising catalyst. A stock holding up well, near its high, or basing after a decline with a clear catalyst has strong asymmetry. Negative price momentum should pull this score down even when the insider signal is strong.
- liquidity: This is a small retail position (~$200). At that size almost any listed stock is tradeable, so score GENEROUSLY and do NOT penalise a company just for being small-cap — small-caps are where the best insider-buy opportunities live. Score 5 for any normally listed NYSE/NASDAQ/ASX stock. Score 2-3 only for genuinely thin situations (nano-cap under ~$50M, OTC/pink-sheet). Score 0 only if there is no real tradeable ticker (e.g. a private company). Liquidity should rarely be the reason a good opportunity scores low.
- timing: Is there a near-term catalyst? Note: insider buys and 13F holdings often have no dated catalyst — for these, judge timing on how recent the signal is rather than expecting a specific event date, and don't zero it out just because there's no scheduled event.

Also write two plain English fields for a retail investor with no finance background:

- plain_english: 2-3 sentences. Explain what the signal is, why it matters, and what would need to happen for it to play out. No jargon. No words like "catalyst", "thesis", "asymmetry", "conviction", "liquidity", "invalidation". Write like you're explaining it to a smart friend who doesn't follow markets.

- signal_type_explainer: One sentence explaining what this type of signal means in general. Base it on the pattern. Examples:
  - insider_buy: "A company executive bought shares with their own money — they didn't have to, which usually means they think the stock is going higher."
  - smart_money: "A large professional investment fund recently disclosed it holds a stake in this company, which can signal they see something others don't."
  - s1_filed: "This company has filed paperwork to go public on the stock exchange — it's the first official step toward an IPO."
  - activist: "A large investor has taken a significant stake and may push for changes like a sale, restructure, or new leadership."
  - thematic_etf: "A new fund has launched that bets on a specific theme or trend, and it's gaining traction quickly."
  - etf_launch: "A new exchange-traded fund has been registered with regulators — an early read on a theme big institutions are preparing to offer investors."

Respond with a JSON array of up to 5 objects:
[
  {
    "conviction": 3,
    "asymmetry": 4,
    "liquidity": 5,
    "timing": 2,
    "vehicle": "REAL_TICKER",
    "pattern": "the signal pattern this pick is based on — one of: insider_buy, smart_money, s1_filed, activist, thematic_etf, etf_launch, spin_off, pre_ipo_proxy",
    "thesis": "3-4 sentences. MUST open by stating the specific signal (who bought/which fund/what filing and when), then why it matters.",
    "catalyst": "The specific signal event that triggers interest (e.g. 'Director open-market purchase filed 2026-06-01'), not a generic macro theme.",
    "invalidation": "What would make you exit.",
    "catalyst_date": "YYYY-MM-DD or null",
    "plain_english": "2-3 sentences, no jargon. MUST mention the concrete thing that happened (e.g. 'an executive just bought a chunk of stock with their own money'), not a vague company story.",
    "signal_type_explainer": "One sentence explaining what this signal type means."
  }
]

Return ONLY the JSON array. No markdown, no explanation, no code fences.
"""


def _extract_json(text: str) -> str:
    """Strip markdown code fences if present, return raw JSON string."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        return match.group(1).strip()
    return text


def _fetch_week_signals(week_of: str) -> list[dict]:
    db = get_client()
    week_end = (date.fromisoformat(week_of) + timedelta(days=6)).isoformat()
    result = (
        db.table("signals")
        .select("*")
        .eq("processed", True)
        .gte("signal_date", week_of)
        .lte("signal_date", week_end)
        .in_("pattern", list(PATTERNS_TO_SCORE))
        .neq("pattern", "irrelevant")
        .execute()
    )
    return result.data


def _log_week_signal_breakdown(week_of: str) -> None:
    """Print a full breakdown of processed signals for the week — diagnostic only."""
    db = get_client()
    week_end = (date.fromisoformat(week_of) + timedelta(days=6)).isoformat()
    all_processed = (
        db.table("signals")
        .select("pattern, source")
        .eq("processed", True)
        .gte("signal_date", week_of)
        .lte("signal_date", week_end)
        .execute()
        .data
    )
    if not all_processed:
        print(f"[score] no processed signals found for week {week_of} — collection may not have run yet")
        return

    from collections import Counter
    by_pattern = Counter(s["pattern"] for s in all_processed)
    scoreable = {p: c for p, c in by_pattern.items() if p in PATTERNS_TO_SCORE}
    skipped = {p: c for p, c in by_pattern.items() if p not in PATTERNS_TO_SCORE}

    print(f"[score] week {week_of} — {len(all_processed)} processed signals total")
    if scoreable:
        print(f"[score]   scoreable: " + ", ".join(f"{p}×{c}" for p, c in sorted(scoreable.items())))
    if skipped:
        print(f"[score]   not scored: " + ", ".join(f"{p}×{c}" for p, c in sorted(skipped.items())))


def _get_price_context(ticker: str) -> dict:
    """
    Fetch price, 52-week high/low, YTD, and average daily DOLLAR volume from
    Yahoo's chart API (the one endpoint that works without auth — quoteSummary
    and v7/quote now require a cookie+crumb and return 401/429).

    Average dollar volume (price × avg daily share volume) is our tradeability
    proxy in place of market cap, which the chart API does not expose. It cleanly
    separates real names (AAPL ~$14B/day) from nano-cap junk (ASPS ~$0.2M/day).

    All fields may be None if the fetch fails.
    """
    import requests
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1y"
        resp = requests.get(url, headers={"User-Agent": "OpportunityScout"}, timeout=10)
        data = resp.json()["chart"]["result"][0]
        meta = data["meta"]
        quote = data.get("indicators", {}).get("quote", [{}])[0]
        closes = [c for c in quote.get("close", []) if c is not None]
        volumes = [v for v in quote.get("volume", []) if v is not None]

        price = meta.get("regularMarketPrice")
        week52_high = max(closes) if closes else None
        week52_low = min(closes) if closes else None
        ytd_pct = (
            round((price - closes[0]) / closes[0] * 100, 1)
            if closes and closes[0] and price
            else None
        )
        # Average daily dollar volume over the last ~60 sessions
        recent_vols = volumes[-60:] if volumes else []
        avg_dollar_volume = (
            round(price * (sum(recent_vols) / len(recent_vols)))
            if recent_vols and price
            else None
        )
        return {
            "price": price,
            "week52_high": round(week52_high, 2) if week52_high else None,
            "week52_low": round(week52_low, 2) if week52_low else None,
            "ytd_change_pct": ytd_pct,
            "avg_dollar_volume": avg_dollar_volume,
        }
    except Exception:
        return {
            "price": None, "week52_high": None, "week52_low": None,
            "ytd_change_pct": None, "avg_dollar_volume": None,
        }


def _get_price(ticker: str) -> float | None:
    return _get_price_context(ticker).get("price")


def score_week(week_of: str | None = None) -> list[str]:
    if week_of is None:
        today = date.today()
        week_of = (today - timedelta(days=today.weekday())).isoformat()

    _log_week_signal_breakdown(week_of)

    signals = _fetch_week_signals(week_of)
    if not signals:
        print(f"[score] no scoreable signals for week {week_of} — all classified as irrelevant/non-scoreable patterns")
        return []
    print(f"[score] sending {len(signals)} signals to Gemini for scoring")

    # Build per-signal summaries enriched with price context where available.
    # Tickers are extracted from raw_data entity_name hints; Gemini resolves
    # the real ticker during scoring, so we do a best-effort lookup here.
    def _signal_summary(s: dict) -> str:
        base = f"[{s['source']} / {s['pattern']}] {s['summary'] or json.dumps(s['raw_data'])[:300]}"
        # Try to get a ticker hint from raw_data for price context
        raw = s.get("raw_data", {})
        ticker_hint = raw.get("vehicle") or raw.get("ticker")
        if ticker_hint:
            px = _get_price_context(ticker_hint)
            if px.get("price"):
                price_now = px["price"]
                parts = [f"Current price: ${price_now}"]
                pct_from_high = pct_from_low = None
                if px.get("week52_high"):
                    pct_from_high = round((price_now - px["week52_high"]) / px["week52_high"] * 100, 1)
                    parts.append(f"52-week high: ${px['week52_high']} ({pct_from_high:+.1f}% from high)")
                if px.get("week52_low"):
                    pct_from_low = round((price_now - px["week52_low"]) / px["week52_low"] * 100, 1)
                    parts.append(f"52-week low: ${px['week52_low']} ({pct_from_low:+.1f}% above low)")
                if px.get("ytd_change_pct") is not None:
                    parts.append(f"YTD: {px['ytd_change_pct']:+.1f}%")
                # Falling-knife flag. Being pinned to the 52-week low is itself a
                # downtrend signal (primary), as is a very deep drawdown that hasn't
                # meaningfully recovered (secondary). A stock that has based and
                # rallied well off its low is spared — that's a recovery, not a knife.
                near_low = pct_from_low is not None and pct_from_low <= 10
                deep_dd = (
                    pct_from_high is not None and pct_from_high <= -40
                    and pct_from_low is not None and pct_from_low <= 25
                )
                if near_low or deep_dd:
                    parts.append(
                        "⚠ DOWNTREND — trading at/near its 52-week low in a sustained "
                        "decline; an insider buying here is high-risk 'catching a falling "
                        "knife', so score asymmetry conservatively (cap at 2) and don't let "
                        "timing rescue it unless there is a specific stabilising catalyst"
                    )
                adv = px.get("avg_dollar_volume")
                if adv:
                    if adv < 1_000_000:
                        liq = f"${adv/1_000_000:.2f}M/day traded — very thin, likely nano-cap, treat with caution"
                    elif adv < 20_000_000:
                        liq = f"${adv/1_000_000:.1f}M/day traded — small-cap, fine for a small retail position"
                    else:
                        liq = f"${adv/1_000_000:.0f}M/day traded — highly liquid"
                    parts.append(f"Avg daily volume: {liq}")
                base += "\n  Price context: " + " | ".join(parts)
        elif ticker_hint:
            base += "\n  Price context: unavailable (ticker may be stale or delisted) — score liquidity conservatively"
        return base

    summaries = "\n\n".join(_signal_summary(s) for s in signals)
    prompt = f"Week of {week_of}. Signals:\n\n{summaries}"

    try:
        resp = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
                system_instruction=SCORING_PROMPT,
            ),
        )
        raw = _extract_json(resp.text or "")
        if not raw:
            print("[score] empty response from Gemini")
            return []
        opportunities = json.loads(raw)
        if isinstance(opportunities, dict):
            opportunities = [opportunities]
    except Exception as e:
        print(f"[score] Gemini error: {e}")
        return []

    picks = [o.get("vehicle", "?") for o in opportunities[:5]]
    scores = [
        o.get("conviction", 0) + o.get("asymmetry", 0) + o.get("liquidity", 0) + o.get("timing", 0)
        for o in opportunities[:5]
    ]
    print(f"[score] Gemini returned {len(picks)} picks: " + ", ".join(
        f"{t} ({s}/20)" for t, s in zip(picks, scores)
    ))

    # Minimum average daily dollar volume to bother trading. Below this is
    # nano-cap / pink-sheet territory — high fraud risk and the kind of thin name
    # (ASPS trades ~$0.2M/day) we got burned on. This is our tradeability gate
    # now that liquidity is scored generously; market cap isn't available from
    # the chart API so dollar volume stands in for it.
    MIN_DOLLAR_VOLUME = 1_000_000
    valid_patterns = PATTERNS_TO_SCORE
    fallback_pattern = signals[0]["pattern"] if signals else "unknown"

    inserted_ids = []
    already_scored = 0
    no_price = 0
    too_small = 0

    for opp in opportunities[:5]:
        ticker = opp.get("vehicle")
        if not ticker:
            continue

        # Skip if already scored this ticker this week (daily re-runs create duplicates)
        if opportunity_exists(ticker, week_of):
            print(f"[score] {ticker} already scored for week {week_of}, skipping")
            already_scored += 1
            continue

        px = _get_price_context(ticker)
        price = px.get("price")

        # Skip if we can't verify the price — likely a stale or changed ticker.
        # ZI→GTM is the canonical example: Gemini suggests the old ticker,
        # we can't fetch data for it, and entry.py will also fail. No point inserting.
        if price is None:
            print(f"[score] {ticker} price unavailable — skipping (stale ticker?)")
            no_price += 1
            continue

        # Hard liquidity floor — the real tradeability/quality gate now that the
        # liquidity score dimension is generous. Only skip when we actually know
        # volume is below the floor (fail open if unknown).
        adv = px.get("avg_dollar_volume")
        if adv is not None and adv < MIN_DOLLAR_VOLUME:
            print(f"[score] {ticker} avg volume ${adv/1e6:.2f}M/day below floor — skipping")
            too_small += 1
            continue

        # Use the pattern Gemini attributed to this specific pick (validated
        # against the known set); fall back to the dominant signal pattern.
        pattern = opp.get("pattern")
        if pattern not in valid_patterns:
            pattern = fallback_pattern

        # Attribute only same-pattern signals to this opportunity for traceability,
        # rather than dumping every signal of the week onto every pick.
        matched_signal_ids = [s["id"] for s in signals if s["pattern"] == pattern]
        if not matched_signal_ids:
            matched_signal_ids = [s["id"] for s in signals]

        row = {
            "title": f"{opp.get('vehicle', 'Unknown')} — {opp.get('catalyst', 'see thesis')[:60]}",
            "thesis": opp.get("thesis", ""),
            "vehicle": opp.get("vehicle", ""),
            "pattern": pattern,
            "catalyst": opp.get("catalyst"),
            "invalidation": opp.get("invalidation"),
            "conviction": opp.get("conviction", 0),
            "asymmetry": opp.get("asymmetry", 0),
            "liquidity": opp.get("liquidity", 0),
            "timing": opp.get("timing", 0),
            "price_at_score": price,
            "catalyst_date": opp.get("catalyst_date"),
            "signal_ids": matched_signal_ids,
            "week_of": week_of,
            "plain_english": opp.get("plain_english", ""),
            "signal_type_explainer": opp.get("signal_type_explainer", ""),
        }
        opp_id = insert_opportunity(row)
        inserted_ids.append(opp_id)
        total = row["conviction"] + row["asymmetry"] + row["liquidity"] + row["timing"]
        print(f"[score] {row['title']} [{pattern}] — score {total}/20")

    print(
        f"[score] done — {len(inserted_ids)} inserted, "
        f"{already_scored} already scored this week, "
        f"{no_price} skipped (no price), {too_small} skipped (too small)"
    )
    return inserted_ids


if __name__ == "__main__":
    score_week()
