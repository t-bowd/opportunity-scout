"""
Generates the weekly digest email and populates feedback rows.
Runs Sunday evening AEST.
"""
import os
import resend
from datetime import date, timedelta
from db.client import (
    get_top_opportunities,
    insert_feedback_rows,
    get_open_paper_positions,
    get_closed_paper_positions,
    get_latest_paper_snapshot,
)

resend.api_key = os.environ["RESEND_API_KEY"]
DIGEST_TO = os.environ["DIGEST_EMAIL"]
SUPABASE_PROJECT = os.environ.get("SUPABASE_PROJECT_REF", "your-project-ref")


def _score_bar(score: int, max_score: int = 20) -> str:
    filled = round((score / max_score) * 10)
    return "█" * filled + "░" * (10 - filled)


def _format_opportunity(opp: dict, rank: int) -> str:
    entity = opp.get("entities") or {}
    ticker = opp.get("vehicle", "?")
    name = entity.get("name", ticker)
    total = opp.get("total_score", 0)
    price = opp.get("price_at_score")
    price_str = f"${price:.2f}" if price else "n/a"
    pattern_label = opp.get("pattern", "?").replace("_", " ").title()
    plain_english = opp.get("plain_english", "")
    signal_type_explainer = opp.get("signal_type_explainer", "")

    lines = [
        # Header
        f"<h3 style='margin-bottom:4px;'>#{rank} — {ticker}</h3>",

        # Signal type badge + explainer
        f"<p style='margin-top:0;'>",
        f"  <span style='background:#e8e8e8;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600;'>{pattern_label}</span>",
        (f"  <br><em style='font-size:12px;color:#666;'>{signal_type_explainer}</em>" if signal_type_explainer else ""),
        f"</p>",

        # Plain English — the "so what", front and centre
        (f"<p style='font-size:16px;line-height:1.6;margin:16px 0;padding:14px 16px;"
         f"background:#f9f9f9;border-left:3px solid #1a1a1a;border-radius:0 6px 6px 0;'>"
         f"{plain_english}</p>" if plain_english else ""),

        # Score bar + price
        f"<p style='font-size:13px;color:#444;'>"
        f"<strong>Score: {total}/20</strong> &nbsp; {_score_bar(total)} &nbsp;&nbsp; "
        f"Price when spotted: {price_str}"
        f"</p>",

        # Detail block — smaller, muted, for those who want to dig in
        f"<div style='margin-top:12px;font-size:13px;color:#555;'>",
        f"<p><strong>Full thesis:</strong> {opp.get('thesis', '')}</p>",
        f"<p><strong>What triggers it:</strong> {opp.get('catalyst', 'n/a')}</p>",
        f"<p><strong>What kills it:</strong> {opp.get('invalidation', 'n/a')}</p>",
        f"<table style='font-size:12px;color:#888;margin-top:8px;border-collapse:collapse;'>",
        f"<tr><td style='padding:2px 16px 2px 0'>Conviction</td><td>{opp.get('conviction')}/5</td></tr>",
        f"<tr><td style='padding:2px 16px 2px 0'>Asymmetry</td><td>{opp.get('asymmetry')}/5</td></tr>",
        f"<tr><td style='padding:2px 16px 2px 0'>Liquidity</td><td>{opp.get('liquidity')}/5</td></tr>",
        f"<tr><td style='padding:2px 16px 2px 0'>Timing</td><td>{opp.get('timing')}/5</td></tr>",
        f"</table>",
        f"</div>",
        f"<hr style='border:none;border-top:1px solid #eee;margin:24px 0;'>",
    ]
    return "\n".join(lines)


def _format_paper_section(week_of: str) -> str:
    open_pos = get_open_paper_positions()
    closed_pos = get_closed_paper_positions()
    snap = get_latest_paper_snapshot()

    # Closed trades from the last 7 days
    cutoff = (date.fromisoformat(week_of) - timedelta(days=1)).isoformat()
    recent_closed = [
        p for p in closed_pos
        if p.get("exit_date") and p["exit_date"] >= cutoff
    ]

    # --- Open positions table ---
    if open_pos:
        rows = ""
        for p in open_pos:
            entry_date = p.get("entry_date", "")
            days_held = (date.today() - date.fromisoformat(entry_date)).days if entry_date else "?"
            rows += (
                f"<tr>"
                f"<td style='padding:4px 12px 4px 0'><strong>{p['ticker']}</strong></td>"
                f"<td style='padding:4px 12px 4px 0'>${float(p['entry_price_aud']):.2f}</td>"
                f"<td style='padding:4px 12px 4px 0'>{days_held}d</td>"
                f"<td style='padding:4px 12px 4px 0'>{p['score_at_entry']}/20</td>"
                f"<td style='padding:4px 0'>{p['pattern'].replace('_', ' ').title()}</td>"
                f"</tr>"
            )
        open_html = f"""
<table style='font-size:13px;border-collapse:collapse;width:100%;'>
  <tr style='color:#888;font-size:11px;'>
    <th style='text-align:left;padding:4px 12px 4px 0'>Ticker</th>
    <th style='text-align:left;padding:4px 12px 4px 0'>Entry (AUD)</th>
    <th style='text-align:left;padding:4px 12px 4px 0'>Held</th>
    <th style='text-align:left;padding:4px 12px 4px 0'>Score</th>
    <th style='text-align:left;padding:4px 0'>Pattern</th>
  </tr>
  {rows}
</table>"""
    else:
        open_html = "<p style='color:#888;font-size:13px;'>No open positions.</p>"

    # --- Recent closes ---
    if recent_closed:
        close_rows = ""
        for p in recent_closed:
            pnl = float(p.get("pnl_aud") or 0)
            pnl_pct = float(p.get("pnl_pct") or 0)
            color = "#2a9d2a" if pnl >= 0 else "#cc3333"
            reason = p.get("exit_reason", "").replace("_", " ").title()
            close_rows += (
                f"<tr>"
                f"<td style='padding:4px 12px 4px 0'><strong>{p['ticker']}</strong></td>"
                f"<td style='padding:4px 12px 4px 0;color:{color}'>{pnl_pct:+.1f}%</td>"
                f"<td style='padding:4px 12px 4px 0;color:{color}'>${pnl:+.2f}</td>"
                f"<td style='padding:4px 0;color:#888;font-size:12px'>{reason}</td>"
                f"</tr>"
            )
        closes_html = f"""
<h3 style='margin:16px 0 8px;'>Closed This Week</h3>
<table style='font-size:13px;border-collapse:collapse;width:100%;'>
  <tr style='color:#888;font-size:11px;'>
    <th style='text-align:left;padding:4px 12px 4px 0'>Ticker</th>
    <th style='text-align:left;padding:4px 12px 4px 0'>Return</th>
    <th style='text-align:left;padding:4px 12px 4px 0'>P&amp;L (AUD)</th>
    <th style='text-align:left;padding:4px 0'>Exit</th>
  </tr>
  {close_rows}
</table>"""
    else:
        closes_html = ""

    # --- Expectancy progress bar ---
    if snap:
        closed_count = snap.get("closed_trades", 0)
        expectancy = snap.get("expectancy_aud")
        win_rate = snap.get("win_rate")
        total_pnl = snap.get("total_pnl_aud", 0)
        needed = max(0, 20 - closed_count)

        exp_str = f"${float(expectancy):+.2f}" if expectancy is not None else "n/a"
        wr_str = f"{float(win_rate):.0f}%" if win_rate is not None else "n/a"
        progress_pct = min(100, int(closed_count / 20 * 100))
        bar_filled = round(progress_pct / 10)

        stats_html = f"""
<h3 style='margin:16px 0 8px;'>Expectancy Progress</h3>
<p style='font-size:13px;'>
  <strong>{closed_count}/20 trades</strong> completed
  &nbsp;{"█" * bar_filled + "░" * (10 - bar_filled)}&nbsp;
  {progress_pct}%
</p>
<p style='font-size:13px;color:#444;'>
  Avg P&amp;L per trade: <strong>{exp_str} AUD</strong> &nbsp;|&nbsp;
  Win rate: <strong>{wr_str}</strong> &nbsp;|&nbsp;
  Total P&amp;L: <strong>${float(total_pnl):+.2f} AUD</strong>
</p>
<p style='font-size:12px;color:#888;'>
  {needed} more trades needed before graduation review.
  Positive expectancy across 20 trades = move to real money.
</p>"""
    else:
        stats_html = "<p style='font-size:13px;color:#888;'>No trades yet — paper portfolio starting this week.</p>"

    return f"""
<hr style='border:none;border-top:2px solid #1a1a1a;margin:32px 0 16px;'>
<h2 style='margin-bottom:4px;'>Paper Portfolio</h2>
<p style='font-size:12px;color:#888;margin-top:0;'>
  Simulated $200 AUD positions. Tracks whether these picks would make money before real money is deployed.
</p>

<h3 style='margin:16px 0 8px;'>Open Positions ({len(open_pos)}/5 slots)</h3>
{open_html}
{closes_html}
{stats_html}
"""


def _build_html(opportunities: list[dict], week_of: str) -> str:
    opp_html = "\n<br>\n".join(
        _format_opportunity(o, i + 1) for i, o in enumerate(opportunities)
    )
    feedback_url = f"https://supabase.com/dashboard/project/{SUPABASE_PROJECT}/editor"
    paper_html = _format_paper_section(week_of)

    return f"""
<html>
<body style="font-family: -apple-system, sans-serif; max-width: 680px; margin: 0 auto; color: #1a1a1a;">
  <h1 style="border-bottom: 2px solid #1a1a1a; padding-bottom: 8px;">
    Opportunity Scout — Week of {week_of}
  </h1>
  <p>Top {len(opportunities)} opportunities scored this week. Grade them in Supabase to improve future picks.</p>

  {opp_html}

  <br>
  <p style="background:#f5f5f5; padding:12px; border-radius:6px;">
    <strong>Grade this week:</strong><br>
    Open the <a href="{feedback_url}">feedback table in Supabase</a> and fill in
    <code>acted</code> (true/false) and <code>grade</code> (1–5) for each row.<br>
    Or run: <code>python grade.py --list</code> from the repo.
  </p>

  {paper_html}

  <p style="font-size:11px; color:#999; border-top: 1px solid #eee; padding-top: 8px;">
    Not financial advice. This is a signal-identification tool. Do your own research before acting on any opportunity.
  </p>
</body>
</html>
"""


def send(week_of: str | None = None) -> None:
    if week_of is None:
        today = date.today()
        week_of = (today - timedelta(days=today.weekday())).isoformat()

    opportunities = get_top_opportunities(week_of)
    if not opportunities:
        print(f"[digest] no opportunities for week {week_of}, skipping email")
        return

    html = _build_html(opportunities, week_of)

    resend.Emails.send({
        "from": "Opportunity Scout <onboarding@resend.dev>",
        "to": [DIGEST_TO],
        "subject": f"📊 Weekly Opportunities — {week_of} ({len(opportunities)} picks)",
        "html": html,
    })

    # Populate feedback table for this week's picks
    opp_ids = [o["id"] for o in opportunities]
    insert_feedback_rows(opp_ids)
    print(f"[digest] sent digest with {len(opportunities)} opportunities, feedback rows created")


if __name__ == "__main__":
    send()
