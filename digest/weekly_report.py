"""
Weekly portfolio digest — deliberately terse.

One compact P/L row per open position, a portfolio total, and the return vs the
S&P 500 (alpha) — nothing else. The alpha/SPY figures reuse snapshot._book_vs_spy
so this email and the daily run's snapshot line always agree. The book is split
LIVE (Alpaca real money) vs PAPER (the legacy sim book winding down), same as the
snapshot; before any live positions exist it prints a single combined block.
"""
import os
import requests
import resend
from datetime import date, timedelta

from db.client import (
    get_open_paper_positions,
    get_latest_paper_snapshot,
)
from paper_trader.exit import _fetch_price
from paper_trader.snapshot import _book_vs_spy, _daily_closes

resend.api_key = os.environ["RESEND_API_KEY"]
DIGEST_TO = os.environ["DIGEST_EMAIL"]
HEADERS = {"User-Agent": "OpportunityScout"}


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


def _pnl_color(pnl: float) -> str:
    return "#2a7a2a" if pnl >= 0 else "#cc3333"


def _position_pnl(pos: dict, fx_rate: float) -> tuple[float | None, float | None]:
    """(pnl_pct, pnl_aud) for one open position, or (None, None) if price is unavailable."""
    is_asx = pos["ticker"].endswith(".AX")
    entry = float(pos["entry_price_aud"])
    qty = pos["quantity"]
    price = _fetch_current_price_aud(pos["ticker"], is_asx, fx_rate)
    if not price or entry <= 0:
        return None, None
    pnl_pct = (price - entry) / entry * 100
    pnl_aud = (price - entry) * qty - float(pos.get("brokerage_aud", 0))
    return pnl_pct, pnl_aud


def _book_block(label: str, positions: list[dict], fx_rate: float,
                spy_hist, audusd_hist, spy_now) -> str:
    """A heading, one P/L row per position, and a total + vs-SPY summary line."""
    if not positions:
        return ""

    # Compute P/L per position, then sort best-first (unpriced sink to the bottom).
    enriched = []
    for pos in positions:
        pnl_pct, pnl_aud = _position_pnl(pos, fx_rate)
        enriched.append((pos, pnl_pct, pnl_aud))
    enriched.sort(key=lambda e: (e[1] is not None, e[1] if e[1] is not None else 0), reverse=True)

    total_pnl = sum(e[2] for e in enriched if e[2] is not None)
    deployed = sum(float(p["entry_price_aud"]) * p["quantity"] + float(p.get("brokerage_aud", 0))
                   for p in positions)

    rows = ""
    for pos, pnl_pct, pnl_aud in enriched:
        ticker = pos["ticker"]
        entry_date = pos.get("entry_date", "")
        held = (date.today() - date.fromisoformat(entry_date)).days if entry_date else "?"
        lock = " 🔒" if pos.get("trailing_stop_active") else ""
        if pnl_pct is None:
            cell = "<span style='color:#888;'>price n/a</span>"
        else:
            c = _pnl_color(pnl_aud)
            cell = (f"<span style='color:{c};font-weight:600;'>"
                    f"{pnl_pct:+.1f}%</span> "
                    f"<span style='color:{c};'>${pnl_aud:+.0f}</span>")
        rows += (
            f"<tr style='border-top:1px solid #f0f0f0;'>"
            f"<td style='padding:6px 16px 6px 0;'><strong>{ticker}</strong>{lock}</td>"
            f"<td style='padding:6px 16px 6px 0;color:#888;'>{held}d</td>"
            f"<td style='padding:6px 0;white-space:nowrap;'>{cell}</td>"
            f"</tr>"
        )

    # Total + vs S&P 500 (alpha). Same computation as the daily snapshot.
    tcolor = _pnl_color(total_pnl)
    summary = (f"<strong style='color:{tcolor};'>${total_pnl:+.0f} AUD</strong> "
               f"unrealized on ${deployed:,.0f} deployed")
    bench = _book_vs_spy(positions, fx_rate, spy_hist, audusd_hist, spy_now)
    if bench:
        book_pct, spy_pct = bench
        alpha = book_pct - spy_pct
        acolor = _pnl_color(alpha)
        summary += (f" &nbsp;·&nbsp; <strong>{book_pct:+.1f}%</strong> vs "
                    f"S&amp;P 500 {spy_pct:+.1f}% &rarr; "
                    f"<strong style='color:{acolor};'>alpha {alpha:+.1f}%</strong>")

    return f"""
<h2 style='margin:24px 0 8px;font-size:16px;'>{label} <span style='color:#888;font-weight:400;'>({len(positions)})</span></h2>
<table style='width:100%;border-collapse:collapse;font-size:14px;'>{rows}</table>
<p style='font-size:13px;margin:10px 0 0;padding-top:8px;border-top:1px solid #e0e0e0;'>{summary}</p>
"""


def _realized_line(snap: dict | None) -> str:
    if not snap or not snap.get("closed_trades"):
        return ""
    closed = snap.get("closed_trades", 0)
    total = float(snap.get("total_pnl_aud", 0))
    win = snap.get("win_rate")
    win_str = f" · win rate {float(win):.0f}%" if win is not None else ""
    c = _pnl_color(total)
    return (f"<p style='font-size:13px;color:#444;margin:20px 0 0;'>"
            f"Realized to date: <strong style='color:{c};'>${total:+.0f} AUD</strong> "
            f"across {closed} closed trades{win_str}</p>")


def _build_html(week_of: str) -> str:
    open_pos = get_open_paper_positions()
    snap = get_latest_paper_snapshot()
    fx_rate = _fetch_fx_rate()

    # Fetch the SPY / FX history once and share it across both book blocks.
    spy_hist = _daily_closes("SPY")
    audusd_hist = _daily_closes("AUDUSD=X")
    spy_now = _fetch_price("SPY")

    live_pos = [p for p in open_pos if p.get("broker") == "alpaca"]
    paper_pos = [p for p in open_pos if p.get("broker") != "alpaca"]

    if live_pos:
        body = (
            _book_block("LIVE — real money", live_pos, fx_rate, spy_hist, audusd_hist, spy_now)
            + _book_block("PAPER — winding down", paper_pos, fx_rate, spy_hist, audusd_hist, spy_now)
        )
    elif open_pos:
        body = _book_block("Open positions", open_pos, fx_rate, spy_hist, audusd_hist, spy_now)
    else:
        body = "<p style='color:#888;font-size:14px;'>No open positions.</p>"

    realized = _realized_line(snap)

    # Force a light background with explicit colors: email clients (and preview
    # panes) in dark mode would otherwise render this dark text on a dark ground.
    return f"""
<html>
<body style="margin:0;padding:0;background-color:#ffffff;">
  <div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;
              padding:20px;color:#1a1a1a;background-color:#ffffff;">
    <h1 style="border-bottom:2px solid #1a1a1a;padding-bottom:8px;font-size:20px;margin-top:0;">
      Opportunity Scout — Week of {week_of}
    </h1>
    {body}
    {realized}
    <p style="font-size:11px;color:#999;border-top:1px solid #eee;padding-top:12px;margin-top:24px;">
      🔒 = trailing stop active. Not financial advice.
    </p>
  </div>
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
