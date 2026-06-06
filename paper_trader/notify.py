"""
Transactional email alerts for paper-trading events — a position opened or closed.

Replaces the old per-filing EDGAR alert spam (every S-1 IPO registration emailed
an alert). Trading is automated now, so the events worth knowing about are actual
trades, not raw filings. Fail-safe: a notification error never interrupts trading,
and if DIGEST_EMAIL isn't set it silently no-ops.
"""
import os
import resend

resend.api_key = os.environ.get("RESEND_API_KEY", "")
_TO = os.environ.get("DIGEST_EMAIL")
_FROM = "Opportunity Scout <onboarding@resend.dev>"

_EXIT_REASONS = {
    "time_exit": "28-day time exit",
    "stop_loss": "Stop loss",
    "trailing_stop": "Trailing stop",
    "manual": "Manual close",
}


def _send(subject: str, html: str) -> None:
    if not _TO:
        return
    try:
        resend.Emails.send({"from": _FROM, "to": [_TO], "subject": subject, "html": html})
        print(f"[notify] sent: {subject}")
    except Exception as e:  # never let a notification break trading
        print(f"[notify] failed to send '{subject}': {e}")


def notify_opened(ticker, market, entry_price_aud, quantity, cost_aud, score, pattern, thesis=""):
    subject = f"🟢 Opened {ticker} — ${cost_aud:.0f} AUD (score {score}/20)"
    thesis_html = (
        f"<p style='font-size:14px;line-height:1.6;color:#444;background:#f9f9f9;"
        f"padding:12px 14px;border-left:3px solid #1a1a1a;border-radius:0 6px 6px 0'>{thesis}</p>"
        if thesis else ""
    )
    _send(subject, f"""
<h2 style='margin:0 0 6px'>Opened {ticker}
  <span style='font-size:12px;color:#888'>({market})</span></h2>
<p style='margin:0 0 4px'><strong>{quantity}</strong> shares @ ${entry_price_aud:.2f} AUD
  = <strong>${cost_aud:.2f} AUD</strong></p>
<p style='color:#666;font-size:13px'>{pattern} &nbsp;·&nbsp; score {score}/20</p>
{thesis_html}
<p style='font-size:11px;color:#999'>Paper trade. Not financial advice.</p>
""")


def notify_closed(ticker, exit_reason, pnl_aud, pnl_pct, days_held):
    up = (pnl_aud or 0) >= 0
    color = "#2a7a2a" if up else "#cc3333"
    subject = f"{'🟢' if up else '🔴'} Closed {ticker} — {pnl_pct:+.1f}% (${pnl_aud:+.0f} AUD)"
    _send(subject, f"""
<h2 style='margin:0 0 6px'>Closed {ticker}</h2>
<p style='color:{color};font-weight:600;font-size:18px;margin:0 0 4px'>
  {pnl_pct:+.1f}% &nbsp; ${pnl_aud:+.2f} AUD</p>
<p style='color:#666;font-size:13px'>{_EXIT_REASONS.get(exit_reason, exit_reason)}
  &nbsp;·&nbsp; held {days_held} days</p>
<p style='font-size:11px;color:#999'>Paper trade. Not financial advice.</p>
""")
