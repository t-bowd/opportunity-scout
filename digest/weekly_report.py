"""
Weekly portfolio digest.
Shows open positions with current P&L, closed trades, and graduation progress.
Opportunities section removed — trading is fully automated.
"""
import os
import requests
import resend
from datetime import date, timedelta
from db.client import (
    get_open_paper_positions,
    get_closed_paper_positions,
    get_latest_paper_snapshot,
    get_client,
)

resend.api_key = os.environ["RESEND_API_KEY"]
DIGEST_TO = os.environ["DIGEST_EMAIL"]
HEADERS = {"User-Agent": "OpportunityScout"}


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def _fetch_fx_rate() -> float:
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/AUDUSD=X?interval=1d&range=5d"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        return resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except Exception:
        return 0.65


def _fetch_current_price_aud(ticker: str, is_asx: bool, fx_rate: float) -> float | None:
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        price = resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return price if is_asx else price / fx_rate
    except Exception:
        return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_opportunities_for_positions(positions: list[dict]) -> dict[str, dict]:
    """Returns {opportunity_id: opportunity_row} for a list of positions."""
    ids = [p["opportunity_id"] for p in positions if p.get("opportunity_id")]
    if not ids:
        return {}
    db = get_client()
    result = db.table("opportunities").select("*").in_("id", ids).execute()
    return {r["id"]: r for r in result.data}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _pnl_color(pnl: float) -> str:
    return "#2a7a2a" if pnl >= 0 else "#cc3333"


def _format_position_card(pos: dict, opp: dict | None, fx_rate: float) -> str:
    ticker = pos["ticker"]
    is_asx = ticker.endswith(".AX")
    market = pos.get("market", "US")
    entry_price = float(pos["entry_price_aud"])
    quantity = pos["quantity"]
    entry_date = pos.get("entry_date", "")
    days_held = (date.today() - date.fromisoformat(entry_date)).days if entry_date else "?"
    score = pos.get("score_at_entry", "?")
    pattern = pos.get("pattern", "").replace("_", " ").title()
    trailing = pos.get("trailing_stop_active", False)
    peak = pos.get("peak_price_aud")

    # Current P&L
    current_price = _fetch_current_price_aud(ticker, is_asx, fx_rate)
    if current_price:
        pnl_pct = (current_price - entry_price) / entry_price * 100
        pnl_aud = (current_price - entry_price) * quantity - float(pos.get("brokerage_aud", 0))
        pnl_color = _pnl_color(pnl_aud)
        pnl_str = (
            f"<span style='color:{pnl_color};font-weight:600;'>"
            f"{pnl_pct:+.1f}% &nbsp; ${pnl_aud:+.2f} AUD</span>"
        )
        current_str = f"${current_price:.2f} AUD"
    else:
        pnl_str = "<span style='color:#888;'>price unavailable</span>"
        current_str = "n/a"

    # Trailing stop floor
    trail_section = ""
    if trailing and peak:
        from paper_trader.exit import TRAILING_STOP_TRAIL_PCT
        floor = float(peak) * (1 - TRAILING_STOP_TRAIL_PCT / 100)
        trail_section = (
            f"<p style='font-size:12px;background:#fff3cd;padding:6px 10px;"
            f"border-radius:4px;margin:8px 0;'>"
            f"🔒 Trailing stop active — floor ${floor:.2f} AUD "
            f"(peak ${float(peak):.2f} AUD)</p>"
        )

    # Why we entered — from the linked opportunity
    plain_english = opp.get("plain_english", "") if opp else ""
    signal_explainer = opp.get("signal_type_explainer", "") if opp else ""
    thesis_html = ""
    if plain_english:
        thesis_html = (
            f"<p style='font-size:14px;line-height:1.6;margin:10px 0;"
            f"padding:12px 14px;background:#f9f9f9;"
            f"border-left:3px solid #1a1a1a;border-radius:0 6px 6px 0;'>"
            f"{plain_english}</p>"
        )

    market_badge = (
        f"<span style='font-size:10px;background:#e8f4fd;padding:1px 6px;"
        f"border-radius:3px;color:#1a6fa8;margin-left:6px;'>{market}</span>"
    )
    pattern_badge = (
        f"<span style='font-size:11px;background:#e8e8e8;padding:2px 8px;"
        f"border-radius:4px;font-weight:600;'>{pattern}</span>"
    )

    return f"""
<div style='margin-bottom:24px;padding-bottom:24px;border-bottom:1px solid #eee;'>
  <h3 style='margin:0 0 4px;'>
    {ticker}{market_badge}
    {"&nbsp;<span style='font-size:10px;background:#fff3cd;padding:1px 5px;border-radius:3px;color:#856404;'>trailing stop</span>" if trailing else ""}
  </h3>
  <p style='margin:0 0 8px;'>{pattern_badge}
    {"&nbsp;<em style='font-size:12px;color:#666;'>" + signal_explainer + "</em>" if signal_explainer else ""}
  </p>

  {thesis_html}

  <table style='font-size:13px;color:#444;border-collapse:collapse;margin-top:8px;'>
    <tr>
      <td style='padding:3px 20px 3px 0;color:#888;'>P&amp;L</td>
      <td>{pnl_str}</td>
    </tr>
    <tr>
      <td style='padding:3px 20px 3px 0;color:#888;'>Current price</td>
      <td>{current_str}</td>
    </tr>
    <tr>
      <td style='padding:3px 20px 3px 0;color:#888;'>Entry price</td>
      <td>${entry_price:.2f} AUD &times; {quantity} shares</td>
    </tr>
    <tr>
      <td style='padding:3px 20px 3px 0;color:#888;'>Held</td>
      <td>{days_held} days &nbsp;·&nbsp; {28 - (days_held if isinstance(days_held, int) else 0)} days remaining</td>
    </tr>
    <tr>
      <td style='padding:3px 20px 3px 0;color:#888;'>Score</td>
      <td>{score}/20</td>
    </tr>
  </table>

  {trail_section}
</div>
"""


def _format_closed_section(recent_closed: list[dict], opps: dict) -> str:
    if not recent_closed:
        return ""

    rows = ""
    for p in recent_closed:
        pnl = float(p.get("pnl_aud") or 0)
        pnl_pct = float(p.get("pnl_pct") or 0)
        color = _pnl_color(pnl)
        reason_map = {
            "time_exit": "28-day exit",
            "stop_loss": "Stop loss −12%",
            "trailing_stop": "Trailing stop",
            "manual": "Manual close",
        }
        reason = reason_map.get(p.get("exit_reason", ""), p.get("exit_reason", "").replace("_", " ").title())
        opp = opps.get(p.get("opportunity_id", ""))
        plain = opp.get("plain_english", "")[:120] + "…" if opp and opp.get("plain_english") else ""

        rows += f"""
<tr style='border-top:1px solid #f0f0f0;'>
  <td style='padding:8px 16px 8px 0;vertical-align:top;'>
    <strong>{p['ticker']}</strong>
    <div style='font-size:11px;color:#888;'>{plain}</div>
  </td>
  <td style='padding:8px 16px 8px 0;vertical-align:top;color:{color};font-weight:600;white-space:nowrap;'>
    {pnl_pct:+.1f}%<br>${pnl:+.2f} AUD
  </td>
  <td style='padding:8px 0;vertical-align:top;color:#888;font-size:12px;'>{reason}</td>
</tr>"""

    return f"""
<h2 style='margin:28px 0 12px;border-top:2px solid #1a1a1a;padding-top:16px;'>Closed This Week</h2>
<table style='width:100%;border-collapse:collapse;font-size:13px;'>
  <tr style='color:#888;font-size:11px;'>
    <th style='text-align:left;padding:4px 16px 4px 0;'>Position</th>
    <th style='text-align:left;padding:4px 16px 4px 0;'>Result</th>
    <th style='text-align:left;padding:4px 0;'>Exit reason</th>
  </tr>
  {rows}
</table>"""


def _format_stats(snap: dict | None, open_count: int) -> str:
    if not snap:
        return "<p style='font-size:13px;color:#888;'>No closed trades yet.</p>"

    closed = snap.get("closed_trades", 0)
    expectancy = snap.get("expectancy_aud")
    win_rate = snap.get("win_rate")
    total_pnl = float(snap.get("total_pnl_aud", 0))
    needed = max(0, 20 - closed)
    progress_pct = min(100, int(closed / 20 * 100))
    bar = "█" * round(progress_pct / 10) + "░" * (10 - round(progress_pct / 10))

    exp_str = f"${float(expectancy):+.2f}" if expectancy is not None else "n/a"
    wr_str = f"{float(win_rate):.0f}%" if win_rate is not None else "n/a"

    return f"""
<h2 style='margin:28px 0 12px;border-top:2px solid #1a1a1a;padding-top:16px;'>Graduation Progress</h2>
<p style='font-size:13px;'><strong>{closed}/20 trades</strong> &nbsp; {bar} &nbsp; {progress_pct}%</p>
<table style='font-size:13px;color:#444;border-collapse:collapse;'>
  <tr><td style='padding:3px 24px 3px 0;color:#888;'>Avg P&amp;L per trade</td><td><strong>{exp_str} AUD</strong></td></tr>
  <tr><td style='padding:3px 24px 3px 0;color:#888;'>Win rate</td><td><strong>{wr_str}</strong></td></tr>
  <tr><td style='padding:3px 24px 3px 0;color:#888;'>Total P&amp;L</td><td><strong>${total_pnl:+.2f} AUD</strong></td></tr>
</table>
<p style='font-size:12px;color:#888;margin-top:8px;'>
  {needed} more closed trades needed. Positive expectancy across 20 = graduate to real money.
</p>"""


def _build_html(week_of: str) -> str:
    open_pos = get_open_paper_positions()
    closed_pos = get_closed_paper_positions()
    snap = get_latest_paper_snapshot()

    cutoff = (date.fromisoformat(week_of) - timedelta(days=1)).isoformat()
    recent_closed = [p for p in closed_pos if p.get("exit_date") and p["exit_date"] >= cutoff]

    all_positions = open_pos + recent_closed
    opps = _get_opportunities_for_positions(all_positions)

    fx_rate = _fetch_fx_rate()

    # Open position cards
    from paper_trader.entry import MAX_POSITIONS
    if open_pos:
        cards = "\n".join(
            _format_position_card(p, opps.get(p.get("opportunity_id", "")), fx_rate)
            for p in open_pos
        )
        open_html = f"<h2 style='margin:0 0 16px;'>Open Positions ({len(open_pos)}/{MAX_POSITIONS} slots)</h2>{cards}"
    else:
        open_html = (
            f"<h2 style='margin:0 0 8px;'>Open Positions (0/{MAX_POSITIONS} slots)</h2>"
            "<p style='color:#888;font-size:13px;'>No open positions this week — "
            "all candidates failed entry filters.</p>"
        )

    closed_html = _format_closed_section(recent_closed, opps)
    stats_html = _format_stats(snap, len(open_pos))

    return f"""
<html>
<body style="font-family:-apple-system,sans-serif;max-width:680px;margin:0 auto;color:#1a1a1a;">
  <h1 style="border-bottom:2px solid #1a1a1a;padding-bottom:8px;">
    Opportunity Scout — Week of {week_of}
  </h1>
  <p style="color:#888;font-size:13px;margin-top:0;">
    Paper portfolio update · $2,000 pool · ~$200 AUD base, scaled by conviction · automated entry &amp; exit
  </p>

  {open_html}
  {closed_html}
  {stats_html}

  <p style="font-size:11px;color:#999;border-top:1px solid #eee;padding-top:12px;margin-top:28px;">
    Not financial advice. This is a paper trading system. Do your own research before deploying real capital.
  </p>
</body>
</html>
"""


def send(week_of: str | None = None) -> None:
    if week_of is None:
        today = date.today()
        week_of = (today - timedelta(days=today.weekday())).isoformat()

    open_pos = get_open_paper_positions()
    html = _build_html(week_of)

    resend.Emails.send({
        "from": "Opportunity Scout <onboarding@resend.dev>",
        "to": [DIGEST_TO],
        "subject": f"📈 Portfolio Update — Week of {week_of} ({len(open_pos)} open)",
        "html": html,
    })
    print(f"[digest] sent portfolio digest — {len(open_pos)} open positions")


if __name__ == "__main__":
    send()
