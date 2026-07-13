"""
Phase 1 entry point — small-cap catalyst screener (READ-ONLY).

Prints a ranked watchlist of small-caps (US + ASX) that have a near-term or
just-hit catalyst and are already climbing. Opens no positions. Intended to run
daily so we can eyeball whether it surfaces sane, tradeable setups before any
paper sleeve is wired up.

Usage:
  python run_smallcap_screen.py                 # US + ASX, cap <= $2B
  python run_smallcap_screen.py --max-cap 500   # micro-cap end only (<= $500M)
  python run_smallcap_screen.py --climbing-only # hide non-climbing catalysts
  python run_smallcap_screen.py --no-asx        # US only (skips the slow ASX scan)
  python run_smallcap_screen.py --asx-scan 100  # widen the ASX announcement scan
"""

import argparse

from smallcap.screen import run_screen


def main() -> None:
    ap = argparse.ArgumentParser(description="Small-cap catalyst screener (read-only)")
    ap.add_argument("--max-cap", type=float, default=2000.0,
                    help="max market cap in $M (default 2000 = $2B)")
    ap.add_argument("--min-cap", type=float, default=10.0,
                    help="min market cap in $M (default 10 = $10M, skips dead shells)")
    ap.add_argument("--no-us", action="store_true", help="exclude US names")
    ap.add_argument("--no-asx", action="store_true", help="exclude ASX names (skips slow scan)")
    ap.add_argument("--asx-scan", type=int, default=60,
                    help="how many ASX materials/energy names to scan for filings")
    ap.add_argument("--climbing-only", action="store_true",
                    help="only show names currently climbing")
    ap.add_argument("--limit", type=int, default=40, help="max rows to print")
    args = ap.parse_args()

    rows = run_screen(
        max_cap=args.max_cap * 1e6,
        min_cap=args.min_cap * 1e6,
        include_us=not args.no_us,
        include_asx=not args.no_asx,
        asx_scan_limit=args.asx_scan,
        climbing_only=args.climbing_only,
    )

    print("\n" + "=" * 100)
    print(f"SMALL-CAP CATALYST WATCHLIST — {len(rows)} names")
    print("=" * 100)
    hdr = f"{'':1} {'TICKER':11} {'EXCH':4} {'VERTICAL':8} {'CAP':>7} {'20d%':>6}  {'CATALYST':42} {'DATE':10}"
    print(hdr)
    print("-" * len(hdr))
    for c in rows:
        flag = "▲" if c["climbing"] else " "
        cap = _cap(c["market_cap"])
        ret = f"{c['ret_20d']:+.0f}" if c["ret_20d"] is not None else "  ?"
        cat = (c["catalyst"] or "")[:42]
        phase = f"[{c['phase']}] " if c.get("phase") else ""
        print(f"{flag} {c['ticker']:11} {c['exchange']:4} {c['vertical']:8} {cap:>7} "
              f"{ret:>6}  {(phase + cat)[:42]:42} {c['catalyst_date']:10}")
    print("\n▲ = climbing (up over ~1mo, above 50-day avg, off its lows)")
    print("READ-ONLY screen — no positions opened. Phase 1.")


def _cap(v: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6)):
        if v >= div:
            return f"${v/div:.1f}{unit}"
    return f"${v:.0f}"


if __name__ == "__main__":
    main()
