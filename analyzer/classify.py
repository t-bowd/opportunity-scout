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
MODEL = "gemini-2.5-flash"

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
    entity = raw.get("entity_name", "Unknown")
    filing_date = signal.get("signal_date", "")

    summaries = {
        "edgar_4":      (
            f"SEC Form 4 insider transaction filed on {filing_date}. "
            f"Company: {entity}. An executive or director bought or sold shares on the open market. "
            f"Use the real NYSE/NASDAQ ticker for {entity} when scoring."
        ),
        "edgar_s1":     (
            f"SEC S-1 IPO registration filed by {entity} on {filing_date}. "
            f"This company is planning to go public. "
            f"Use the real NYSE/NASDAQ ticker for {entity} if already assigned, otherwise note as pre-IPO."
        ),
        "edgar_13f_hr": (
            f"SEC 13F institutional holdings report filed by {entity} on {filing_date}. "
            f"This fund disclosed its quarterly equity positions. "
            f"Use the real NYSE/NASDAQ ticker for {entity} when scoring."
        ),
        "edgar_13d":    (
            f"SEC 13D activist filing on {filing_date}. "
            f"An activist investor has taken a significant stake in {entity}. "
            f"Use the real NYSE/NASDAQ ticker for {entity} when scoring."
        ),
        "edgar_n1a":    (
            f"SEC N-1A new ETF registration filed by {entity} on {filing_date}. "
            f"A new fund is being registered. Use the real ticker once assigned."
        ),
        "etf_launch":   (
            f"New ETF launched: {raw.get('title', entity)} on {filing_date}. "
            f"Use the real NYSE/NASDAQ ticker when scoring."
        ),
    }
    return summaries.get(source, f"Signal from {source} for {entity} on {filing_date}.")


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


def classify_signal(signal: dict) -> dict | None:
    """
    Classify a single signal — used by the EDGAR watcher for real-time alerts.
    Returns a dict with at minimum 'pattern' and 'summary', or None on failure.
    """
    source = signal.get("source", "")
    if source in RULE_BASED_SOURCES:
        return {
            "pattern": signal.get("pattern") or "irrelevant",
            "summary": _rule_based_summary(signal),
            "entity_name": signal.get("raw_data", {}).get("entity_name", "Unknown"),
            "ticker": signal.get("raw_data", {}).get("vehicle"),
            "confidence": 90,
        }
    elif source == "news":
        return classify_signal_with_llm(signal)
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
