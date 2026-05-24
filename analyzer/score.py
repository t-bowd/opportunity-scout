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
from db.client import get_client, insert_opportunity

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
MODEL = "gemini-2.5-flash"

PATTERNS_TO_SCORE = {
    "pre_ipo_proxy", "thematic_etf", "s1_filed",
    "insider_buy", "activist", "smart_money", "spin_off",
}

SCORING_PROMPT = """You are scoring stock market opportunities for a retail investor.

You will receive a list of signals collected this week from SEC filings and news.
Identify the top 5 most actionable opportunities and score each.

Score each dimension 0-5:
- conviction: How many independent signals support this?
- asymmetry: What is the upside/downside ratio given the catalyst?
- liquidity: Can a retail investor actually trade this?
- timing: Is the catalyst dated and near-term?

Respond with a JSON array of exactly 5 objects:
[
  {
    "conviction": 3,
    "asymmetry": 4,
    "liquidity": 5,
    "timing": 2,
    "vehicle": "TICKER",
    "thesis": "3-4 sentences explaining the opportunity.",
    "catalyst": "What triggers the move.",
    "invalidation": "What would make you exit.",
    "catalyst_date": "YYYY-MM-DD or null"
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
