"""
Classifies unprocessed signals.

EDGAR signals (edgar_4, edgar_s1, edgar_13f_hr, edgar_13d, edgar_n1a) are
pre-classified by the collector — we just mark them processed and generate
a plain summary without LLM.

Only news signals need Gemini, since they require actual NLP to extract
the pattern and entity. This keeps API calls to ~5-10/day.
"""
import json
import os
import time
from google import genai
from google.genai import types
from db.client import get_unprocessed_signals, mark_signal_processed

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
MODEL = "gemini-2.0-flash-001"

# These sources have pattern already set by the collector — no LLM needed
RULE_BASED_SOURCES = {
    "edgar_4",      # insider_buy
    "edgar_s1",     # s1_filed
    "edgar_13f_hr", # smart_money
    "edgar_13d",    # activist
    "edgar_n1a",    # etf_launch
    "etf_launch",   # thematic_etf
}

PATTERNS = [
    "pre_ipo_proxy", "thematic_etf", "s1_filed", "insider_buy",
    "activist", "smart_money", "spin_off", "index_inclusion",
    "short_squeeze", "irrelevant",
]

SYSTEM_PROMPT = f"""You are a financial signal classifier for a stock market opportunity-scouting agent.

You receive a news article title and summary.
Your job: extract structured information to help identify investment opportunities.

Patterns to identify: {", ".join(PATTERNS)}

Respond ONLY with valid JSON:
{{
  "pattern": "<one of the patterns above>",
  "entity_name": "<company or ETF name, or null>",
  "ticker": "<ticker symbol if known, or null>",
  "summary": "<2-3 sentence summary of what this means for an investor>",
  "urgency": "high|medium|low",
  "catalyst_date": "<ISO date if a specific event date is mentioned, or null>",
  "confidence": 0-100
}}

Be conservative — if you can't identify a clear pattern, use "irrelevant".
"""


def _rule_based_summary(signal: dict) -> str:
    """Generate a plain summary for EDGAR signals without calling Gemini."""
    raw = signal.get("raw_data", {})
    source = signal.get("source", "")
    entity = raw.get("entity_name", raw.get("companyName", "Unknown entity"))
    date = signal.get("signal_date", "")

    summaries = {
        "edgar_4":      f"Insider transaction filed for {entity} on {date}.",
        "edgar_s1":     f"{entity} filed an S-1 registration statement on {date}, indicating a planned IPO.",
        "edgar_13f_hr": f"Institutional holdings report (13F) filed by {entity} on {date}.",
        "edgar_13d":    f"Activist stake (13D) filed in {entity} on {date}.",
        "edgar_n1a":    f"New ETF registration (N-1A) filed by {entity} on {date}.",
        "etf_launch":   f"New ETF launched: {raw.get('title', entity)} on {date}.",
    }
    return summaries.get(source, f"Signal from {source} on {date}.")


def classify_signal_with_llm(signal: dict) -> dict | None:
    """Call Gemini for news signals only. Fails fast — no retries."""
    raw = signal["raw_data"]
    text = f"{raw.get('title', '')} {raw.get('summary', '')}".strip()[:2000]
    prompt = f"Date: {signal['signal_date']}\n\nArticle:\n{text}"

    try:
        resp = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
                system_instruction=SYSTEM_PROMPT,
            ),
        )
        return json.loads(resp.text)
    except Exception as e:
        print(f"[classify] skipping signal {signal['id']}: {e}")
        return None


def run(limit: int = 50) -> int:
    signals = get_unprocessed_signals(limit=limit)
    classified = 0

    for signal in signals:
        source = signal.get("source", "")

        if source in RULE_BASED_SOURCES:
            # No LLM needed — pattern already set by collector
            summary = _rule_based_summary(signal)
            pattern = signal.get("pattern") or "irrelevant"
            mark_signal_processed(signal["id"], summary, pattern)
            classified += 1

        elif source == "news":
            result = classify_signal_with_llm(signal)
            if result:
                mark_signal_processed(
                    signal["id"],
                    result.get("summary", ""),
                    result.get("pattern", "irrelevant"),
                )
                classified += 1
            time.sleep(5)  # rate limit only applies to LLM calls

        else:
            # Unknown source — mark processed as irrelevant so it doesn't block
            mark_signal_processed(signal["id"], "", "irrelevant")

    print(f"[classify] classified {classified}/{len(signals)} signals")
    return classified


if __name__ == "__main__":
    run()
