"""
Scores classified signals into ranked opportunities.
Runs after classify.py has processed the day's signals.
"""
import json
import os
from datetime import date, timedelta
import google.generativeai as genai
from db.client import get_client, insert_opportunity, get_unprocessed_signals

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-2.0-flash")

MIN_SCORE_TO_ALERT = 14  # out of 20; triggers immediate email

PATTERNS_TO_SCORE = {
    "pre_ipo_proxy", "thematic_etf", "s1_filed",
    "insider_buy", "activist", "smart_money", "spin_off",
}

SCORING_PROMPT = """You are scoring a stock market opportunity for a retail investor.

Score each dimension 0-5:
- conviction: How many independent signals support this? (0=one weak signal, 5=multiple strong signals)
- asymmetry: What is the upside/downside ratio given the catalyst? (0=symmetric, 5=highly asymmetric upside)
- liquidity: Can a retail investor actually trade this? (0=private/illiquid, 5=high-volume public market)
- timing: Is the catalyst dated and near-term? (0=open-ended/years away, 5=specific event within 4 weeks)

Also write:
- vehicle: the specific ticker to trade (must be publicly tradeable)
- thesis: 3-4 sentences explaining the opportunity
- catalyst: what event or development triggers the move
- invalidation: what would make you exit the position

Respond ONLY with valid JSON:
{
  "conviction": 0-5,
  "asymmetry": 0-5,
  "liquidity": 0-5,
  "timing": 0-5,
  "vehicle": "TICKER",
  "thesis": "...",
  "catalyst": "...",
  "invalidation": "...",
  "catalyst_date": "YYYY-MM-DD or null"
}
"""


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


def _get_price(ticker: str) -> float | None:
    import requests
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
        resp = requests.get(url, headers={"User-Agent": "OpportunityScout"}, timeout=10)
        return resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except Exception:
        return None


def score_week(week_of: str | None = None) -> list[str]:
    if week_of is None:
        today = date.today()
        week_of = (today - timedelta(days=today.weekday())).isoformat()

    signals = _fetch_week_signals(week_of)
    if not signals:
        print(f"[score] no signals to score for week {week_of}")
        return []

    summaries = "\n\n".join(
        f"[{s['source']} / {s['pattern']}] {s['summary'] or json.dumps(s['raw_data'])[:300]}"
        for s in signals
    )

    prompt = f"Week of {week_of}. Signals collected this week:\n\n{summaries}\n\nIdentify the top 5 most actionable opportunities and score each."

    try:
        resp = model.generate_content(
            [SCORING_PROMPT, prompt],
            generation_config={"temperature": 0.2},
        )
        raw = resp.text.strip()
        # Gemini may return a list of objects or a single object
        if raw.startswith("["):
            opportunities = json.loads(raw)
        else:
            opportunities = [json.loads(raw)]
    except Exception as e:
        print(f"[score] Gemini error: {e}")
        return []

    inserted_ids = []
    for opp in opportunities[:5]:
        ticker = opp.get("vehicle")
        price = _get_price(ticker) if ticker else None

        row = {
            "title": f"{opp.get('vehicle', 'Unknown')} — {opp.get('catalyst', 'see thesis')[:60]}",
            "thesis": opp.get("thesis", ""),
            "vehicle": opp.get("vehicle", ""),
            "pattern": signals[0]["pattern"] if signals else "unknown",
            "catalyst": opp.get("catalyst"),
            "invalidation": opp.get("invalidation"),
            "conviction": opp.get("conviction", 0),
            "asymmetry": opp.get("asymmetry", 0),
            "liquidity": opp.get("liquidity", 0),
            "timing": opp.get("timing", 0),
            "price_at_score": price,
            "catalyst_date": opp.get("catalyst_date"),
            "signal_ids": [s["id"] for s in signals],
            "week_of": week_of,
        }
        opp_id = insert_opportunity(row)
        inserted_ids.append(opp_id)
        total = row["conviction"] + row["asymmetry"] + row["liquidity"] + row["timing"]
        print(f"[score] {row['title']} — score {total}/20")

    return inserted_ids


if __name__ == "__main__":
    score_week()
