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

SCORING_PROMPT = """You are scoring stock market opportunities for a retail investor based in Australia.

You will receive signals from SEC filings (Form 4 insider buys, S-1 IPOs, 13F institutional holdings, 13D activist stakes) and financial news including Australian sources (ASX announcements, SMH, ABC Business).

Each signal names a real company. Your job:
1. Identify the top 5 most actionable opportunities from the signals
2. For each, use the REAL ticker symbol — NYSE/NASDAQ for US stocks, or ASX ticker (e.g. CBA.AX) for Australian stocks. Do NOT invent placeholder names.
3. Score each opportunity

Score each dimension 0-5:
- conviction: How many independent signals support this?
- asymmetry: What is the upside/downside ratio given the catalyst?
- liquidity: Can a retail investor actually trade this? (if no ticker known, score 0)
- timing: Is the catalyst dated and near-term?

Also write two plain English fields for a retail investor with no finance background:

- plain_english: 2-3 sentences. Explain what the signal is, why it matters, and what would need to happen for it to play out. No jargon. No words like "catalyst", "thesis", "asymmetry", "conviction", "liquidity", "invalidation". Write like you're explaining it to a smart friend who doesn't follow markets.

- signal_type_explainer: One sentence explaining what this type of signal means in general. Base it on the pattern. Examples:
  - insider_buy: "A company executive bought shares with their own money — they didn't have to, which usually means they think the stock is going higher."
  - smart_money: "A large professional investment fund recently disclosed it holds a stake in this company, which can signal they see something others don't."
  - s1_filed: "This company has filed paperwork to go public on the stock exchange — it's the first official step toward an IPO."
  - activist: "A large investor has taken a significant stake and may push for changes like a sale, restructure, or new leadership."
  - thematic_etf: "A new fund has launched that bets on a specific theme or trend, and it's gaining traction quickly."

Respond with a JSON array of up to 5 objects:
[
  {
    "conviction": 3,
    "asymmetry": 4,
    "liquidity": 5,
    "timing": 2,
    "vehicle": "REAL_TICKER",
    "thesis": "3-4 sentences explaining the opportunity.",
    "catalyst": "What triggers the move.",
    "invalidation": "What would make you exit.",
    "catalyst_date": "YYYY-MM-DD or null",
    "plain_english": "2-3 sentences, no jargon, explain it like a smart friend.",
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
            "plain_english": opp.get("plain_english", ""),
            "signal_type_explainer": opp.get("signal_type_explainer", ""),
        }
        opp_id = insert_opportunity(row)
        inserted_ids.append(opp_id)
        total = row["conviction"] + row["asymmetry"] + row["liquidity"] + row["timing"]
        print(f"[score] {row['title']} — score {total}/20")

    return inserted_ids


if __name__ == "__main__":
    score_week()
