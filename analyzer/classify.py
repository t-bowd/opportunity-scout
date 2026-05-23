"""
Classifies unprocessed signals using Gemini 2.0 Flash (free tier).
Extracts: pattern type, entity name/ticker, summary, urgency.
"""
import json
import os
import time
import google.generativeai as genai
from db.client import get_unprocessed_signals, mark_signal_processed

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-2.0-flash")

PATTERNS = [
    "pre_ipo_proxy",   # vehicle providing exposure to a private company pre-IPO
    "thematic_etf",    # new ETF launched around a theme / narrative
    "s1_filed",        # company filed S-1 (direct IPO signal)
    "insider_buy",     # open-market insider purchase (bullish signal)
    "activist",        # 13D filing, activist taking stake
    "smart_money",     # 13F showing new/increased position by notable fund
    "spin_off",        # spin-off or carve-out situation
    "index_inclusion", # candidate for S&P 500 / Russell rebalance
    "short_squeeze",   # high short interest + borrow rate + catalyst
    "irrelevant",      # not actionable
]

SYSTEM_PROMPT = f"""You are a financial signal classifier for a stock market opportunity-scouting agent.

You receive raw data from SEC filings, ETF databases, or financial news.
Your job: extract structured information to help identify investment opportunities.

Patterns to identify: {", ".join(PATTERNS)}

Respond ONLY with valid JSON matching this schema:
{{
  "pattern": "<one of the patterns above>",
  "entity_name": "<company or ETF name, or null>",
  "ticker": "<ticker symbol if known, or null>",
  "summary": "<2-3 sentence summary of what this signal means for an investor>",
  "urgency": "high|medium|low",
  "catalyst_date": "<ISO date if a specific event date is mentioned, or null>",
  "confidence": 0-100
}}

Be conservative — if you can't identify a clear pattern, use "irrelevant".
"""


def classify_signal(signal: dict) -> dict | None:
    raw = json.dumps(signal["raw_data"], default=str)[:3000]  # token guard
    prompt = f"Source: {signal['source']}\nDate: {signal['signal_date']}\n\nRaw data:\n{raw}"

    try:
        resp = model.generate_content(
            [SYSTEM_PROMPT, prompt],
            generation_config={"temperature": 0.1, "response_mime_type": "application/json"},
        )
        return json.loads(resp.text)
    except Exception as e:
        print(f"[classify] error for signal {signal['id']}: {e}")
        return None


def run() -> int:
    signals = get_unprocessed_signals(limit=50)
    classified = 0

    for signal in signals:
        result = classify_signal(signal)
        if not result:
            continue

        pattern = result.get("pattern", "irrelevant")
        summary = result.get("summary", "")
        mark_signal_processed(signal["id"], summary, pattern)
        classified += 1

        # Gemini free tier: 15 RPM — stay under it
        time.sleep(4)

    print(f"[classify] classified {classified} signals")
    return classified


if __name__ == "__main__":
    run()
