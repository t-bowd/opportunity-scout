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


def _13f_action(raw: dict, entity: str) -> str:
    """Describe what the fund did with this position, based on the quarter diff."""
    fund = raw.get("fund_name", "a large institutional investor")
    change = raw.get("change")
    if change == "new":
        return f"the fund {fund} INITIATED a brand-new position in {entity}"
    if change == "increased":
        pct = raw.get("pct_change")
        # A huge % off a small base is misleading — describe it qualitatively instead.
        if pct and pct > 300:
            bump = " substantially (a large add this quarter)"
        elif pct:
            bump = f" by ~{pct}%"
        else:
            bump = ""
        return f"the fund {fund} INCREASED its position in {entity}{bump}"
    return f"the fund {fund} disclosed holding a position in {entity}"


def _rule_based_summary(signal: dict) -> str:
    """Generate a plain summary for EDGAR signals without calling Gemini."""
    raw = signal.get("raw_data", {})
    source = signal.get("source", "")
    entity = raw.get("entity_name", "Unknown")
    filing_date = signal.get("signal_date", "")

    # When the collector resolved the issuer's real ticker from its CIK, state it
    # explicitly so Gemini scores the correct, current symbol instead of guessing
    # from the company name (which produced stale tickers like ZI for ZoomInfo).
    ticker = raw.get("ticker")
    ticker_clause = (
        f"The tradeable ticker is {ticker} — use exactly this symbol when scoring."
        if ticker else
        f"Use the real NYSE/NASDAQ ticker for {entity} when scoring."
    )

    # Dispatch by source — build ONLY the matching summary. (Building all f-strings
    # eagerly previously crashed: a Form 4 with no price stores value_usd=None, and
    # the 13F line's "value_usd:," format blew up on it even for Form 4 signals.)
    if source == "edgar_4":
        buyer = raw.get("buyer", "An insider")
        roles = ", ".join(raw.get("roles") or ["insider"])
        shares = raw.get("shares")
        price = raw.get("price")
        value = raw.get("value_usd")
        f4 = f"{buyer} ({roles}) made an open-market purchase"
        if shares:
            f4 += f" of {int(shares):,} shares"
        f4 += f" of {entity}"
        if price:
            f4 += f" at ~${price}/share"
        if value:
            f4 += f" (~${int(value):,} total)"
        txn_date = raw.get("transaction_date")
        when = (
            f"purchased {txn_date}, filed {filing_date}"
            if txn_date else f"filed on {filing_date}"
        )
        return (
            f"SEC Form 4 open-market purchase (transaction code P) {when}. "
            f"{f4}. The insider bought shares with their own money — a discretionary, "
            f"bullish signal, not a grant, ESPP, or option exercise. {ticker_clause}"
        )

    if source == "edgar_s1":
        return (
            f"SEC S-1 IPO registration filed by {entity} on {filing_date}. "
            f"This company is planning to go public. "
            + (ticker_clause if ticker
               else f"Use the real NYSE/NASDAQ ticker for {entity} if already assigned, otherwise note as pre-IPO.")
        )

    if source == "edgar_13f_hr":
        value = raw.get("value_usd") or 0
        return (
            f"SEC 13F filing on {filing_date}: {_13f_action(raw, entity)} "
            f"(reported value ~${value:,} USD). "
            f"A newly initiated or materially increased position is a real conviction signal; "
            f"note 13F data is filed up to 45 days after quarter-end, so it is backward-looking. "
            f"Use the real NYSE/NASDAQ ticker for {entity} (the held company, not the fund) when scoring."
        )

    if source == "edgar_13d":
        return (
            f"SEC 13D activist filing on {filing_date}. "
            f"An activist investor has taken a significant stake in {entity}. {ticker_clause}"
        )

    if source == "edgar_n1a":
        return (
            f"SEC N-1A new ETF registration filed by {entity} on {filing_date}. "
            f"A new fund is being registered. "
            + (ticker_clause if ticker else "Use the real ticker once assigned.")
        )

    if source == "etf_launch":
        return (
            f"New ETF launched: {raw.get('title', entity)} on {filing_date}. {ticker_clause}"
        )

    return f"Signal from {source} for {entity} on {filing_date}."


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
            # On failure, leave as unprocessed so tomorrow's run retries it.
            # Signals older than 7 days are excluded by get_unprocessed_signals
            # so failed signals age out naturally rather than accumulating.
            time.sleep(5)  # rate limit only applies to LLM calls

        else:
            # Unknown source — mark processed as irrelevant so it doesn't block
            mark_signal_processed(signal["id"], "", "irrelevant")
            classified += 1

    print(f"[classify] classified {classified}/{len(signals)} signals")
    return classified


if __name__ == "__main__":
    run()
