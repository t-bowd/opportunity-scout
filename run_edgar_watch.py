"""
Entry point for the hourly EDGAR watcher.
Collects last 2 hours of filings and immediately classifies them.
High-urgency signals trigger an alert email.
"""
import os
import resend
from dotenv import load_dotenv

load_dotenv(".env.local")

from collectors import edgar
from analyzer.classify import get_unprocessed_signals, classify_signal
from db.client import mark_signal_processed, get_client

resend.api_key = os.environ["RESEND_API_KEY"]
DIGEST_TO = os.environ["DIGEST_EMAIL"]

HIGH_URGENCY_PATTERNS = {"s1_filed", "activist", "pre_ipo_proxy"}


def send_alert(signal: dict, classification: dict) -> None:
    resend.Emails.send({
        "from": "Opportunity Scout <onboarding@resend.dev>",
        "to": [DIGEST_TO],
        "subject": f"🚨 Alert: {classification.get('pattern', 'Signal')} — {classification.get('ticker', 'unknown')}",
        "html": f"""
<h2>High-urgency signal detected</h2>
<p><strong>Pattern:</strong> {classification.get('pattern')}</p>
<p><strong>Entity:</strong> {classification.get('entity_name')} ({classification.get('ticker')})</p>
<p><strong>Summary:</strong> {classification.get('summary')}</p>
<p><strong>Source:</strong> {signal.get('source')}</p>
<p><a href="{signal.get('url', '#')}">View filing →</a></p>
<p style="font-size:11px;color:#999">Not financial advice.</p>
""",
    })
    print(f"[edgar_watch] alert sent for {classification.get('ticker')}")


def run() -> None:
    import time
    edgar.collect(lookback_days=1)  # EDGAR API doesn't support sub-day filters; dedup handles it

    signals = get_unprocessed_signals(limit=20)
    for signal in signals:
        result = classify_signal(signal)
        if not result:
            continue
        pattern = result.get("pattern", "irrelevant")
        mark_signal_processed(signal["id"], result.get("summary", ""), pattern)

        if pattern in HIGH_URGENCY_PATTERNS and result.get("confidence", 0) >= 70:
            send_alert(signal, result)

        time.sleep(4)  # Gemini rate limit


if __name__ == "__main__":
    run()
