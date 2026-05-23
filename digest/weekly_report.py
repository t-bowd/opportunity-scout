"""
Generates the weekly digest email and populates feedback rows.
Runs Sunday evening AEST.
"""
import os
import resend
from datetime import date, timedelta
from db.client import get_top_opportunities, insert_feedback_rows

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

    lines = [
        f"<h3>#{rank} — {ticker} ({name})</h3>",
        f"<p><strong>Score: {total}/20</strong> &nbsp; {_score_bar(total)}</p>",
        f"<p><strong>Pattern:</strong> {opp.get('pattern', '?').replace('_', ' ').title()}</p>",
        f"<p><strong>Price at scoring:</strong> {price_str}</p>",
        f"<p><strong>Thesis:</strong> {opp.get('thesis', '')}</p>",
        f"<p><strong>Catalyst:</strong> {opp.get('catalyst', 'n/a')}</p>",
        f"<p><strong>Invalidation:</strong> {opp.get('invalidation', 'n/a')}</p>",
        f"<hr>",
        f"<table style='font-size:12px;color:#666'>",
        f"<tr><td>Conviction</td><td>{opp.get('conviction')}/5</td></tr>",
        f"<tr><td>Asymmetry</td><td>{opp.get('asymmetry')}/5</td></tr>",
        f"<tr><td>Liquidity</td><td>{opp.get('liquidity')}/5</td></tr>",
        f"<tr><td>Timing</td><td>{opp.get('timing')}/5</td></tr>",
        f"</table>",
    ]
    return "\n".join(lines)


def _build_html(opportunities: list[dict], week_of: str) -> str:
    opp_html = "\n<br>\n".join(
        _format_opportunity(o, i + 1) for i, o in enumerate(opportunities)
    )
    feedback_url = f"https://supabase.com/dashboard/project/{SUPABASE_PROJECT}/editor"

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
