"""
Smoke tests for the small-cap sleeve's PURE decision logic (no DB, no network).
Mirrors run_smoke.py's style. Exercises the exit precedence, ratcheting trail,
catalyst-date parsing, and entry gates — the parts where a bug would silently
mis-trade.
"""

from datetime import date

from smallcap.exit import (
    trail_pct_for, parse_catalyst_date, business_days_until, evaluate,
    TRAIL_ARM_PCT, DISASTER_STOP_PCT,
)
from smallcap.entry import passes_gates

TODAY = date(2026, 7, 16)
_fails = 0


def check(name, cond):
    global _fails
    print(f"  {'ok ' if cond else 'FAIL'} {name}")
    if not cond:
        _fails += 1


def _pos(**kw):
    base = {"entry_price_aud": 1.0, "entry_date": "2026-07-01",
            "peak_price_aud": 1.0, "trailing_stop_active": False, "catalyst_date": None}
    base.update(kw)
    return base


print("trail_pct_for (ratcheting tiers):")
check("baseline +50% -> 20%", trail_pct_for(50) == 20.0)
check("just armed +30% -> 20%", trail_pct_for(30) == 20.0)
check("+150% -> 17%", trail_pct_for(150) == 17.0)
check("+500% -> 15%", trail_pct_for(500) == 15.0)
check("below arm still 20% (never <15)", trail_pct_for(0) == 20.0)

print("parse_catalyst_date:")
check("full date", parse_catalyst_date("2026-08-15") == date(2026, 8, 15))
check("year-month -> 1st", parse_catalyst_date("2026-08") == date(2026, 8, 1))
check("year -> Jan 1", parse_catalyst_date("2026") == date(2026, 1, 1))
check("empty -> None", parse_catalyst_date("") is None)
check("None -> None", parse_catalyst_date(None) is None)
check("garbage -> None", parse_catalyst_date("soon") is None)

print("business_days_until:")
check("past date -> 0", business_days_until(date(2026, 7, 10), TODAY) == 0)
check("today -> 0", business_days_until(TODAY, TODAY) == 0)
check("Fri 7/16->7/17 = 1", business_days_until(date(2026, 7, 17), TODAY) == 1)
check("over a weekend counts weekdays", business_days_until(date(2026, 7, 20), TODAY) == 2)

print("evaluate — exit precedence:")
# Pre-catalyst beats everything: catalyst in 1 business day, even while up big.
c = evaluate(_pos(catalyst_date="2026-07-17", peak_price_aud=2.0, trailing_stop_active=True),
             price_aud=1.8, today=TODAY)
check("pre_catalyst fires within 2 biz days", c["reason"] == "pre_catalyst" and c["action"] == "exit")

# Pre-catalyst does NOT fire when the readout is far off.
c = evaluate(_pos(catalyst_date="2026-11-01"), price_aud=1.1, today=TODAY)
check("no pre_catalyst when far off", c["action"] == "hold")

# Trailing: armed at +30%, price falls 20% below a +40% peak -> exit.
c = evaluate(_pos(catalyst_date="2026-11-01", peak_price_aud=1.4, trailing_stop_active=True),
             price_aud=1.4 * 0.79, today=TODAY)
check("trailing_stop fires 20% below peak", c["reason"].startswith("trailing_stop") and c["action"] == "exit")

# Trailing arms latched off peak even if current price dipped below +30%.
c = evaluate(_pos(catalyst_date="2026-11-01", peak_price_aud=1.5, trailing_stop_active=False),
             price_aud=1.45, today=TODAY)
check("trail arms from peak gain, holds while near peak", c["trail_active"] and c["action"] == "hold")

# Not armed below +30% and modest drawdown -> hold (no -12% stop here).
c = evaluate(_pos(catalyst_date="2026-11-01", peak_price_aud=1.1), price_aud=0.9, today=TODAY)
check("no tight stop: -10% just holds", c["action"] == "hold")

# Disaster stop at -35%.
c = evaluate(_pos(catalyst_date="2026-11-01", peak_price_aud=1.0), price_aud=0.64, today=TODAY)
check("disaster_stop at -35%", c["reason"] == "disaster_stop" and c["action"] == "exit")

# Time limit only for non-dated (asx_ann) positions.
c = evaluate(_pos(catalyst_date=None, entry_date="2026-05-01", peak_price_aud=1.05),
             price_aud=1.0, today=TODAY)
check("time_limit fires for non-dated after 45d", c["reason"] == "time_limit" and c["action"] == "exit")

c = evaluate(_pos(catalyst_date="2026-11-01", entry_date="2026-05-01", peak_price_aud=1.05),
             price_aud=1.0, today=TODAY)
check("no time_limit for dated biotech", c["action"] == "hold")

print("passes_gates:")
held: set[str] = set()
vc: dict[str, int] = {}
bio = {"ticker": "AKBA", "vertical": "biotech", "market_cap": 370e6, "climbing": True,
       "catalyst_date": "2026-08-15", "exchange": "US"}
check("good biotech passes", passes_gates(bio, held, vc, TODAY)[0])
check("not climbing rejected", not passes_gates({**bio, "climbing": False}, held, vc, TODAY)[0])
check("cap over $500M rejected", not passes_gates({**bio, "market_cap": 600e6}, held, vc, TODAY)[0])
check("biotech past-dated rejected", not passes_gates({**bio, "catalyst_date": "2026-06-01"}, held, vc, TODAY)[0])
check("already held rejected", not passes_gates(bio, {"AKBA"}, vc, TODAY)[0])
check("vertical cap full rejected", not passes_gates(bio, held, {"biotech": 5}, TODAY)[0])
asx = {"ticker": "ERE.AX", "vertical": "asx_ann", "market_cap": 10e6, "climbing": True,
       "catalyst_date": "2026-07-14", "exchange": "ASX"}
check("fresh asx_ann passes", passes_gates(asx, held, vc, TODAY)[0])
check("stale asx_ann (>10d) rejected", not passes_gates({**asx, "catalyst_date": "2026-06-01"}, held, vc, TODAY)[0])

print()
if _fails:
    print(f"SMOKE FAILED — {_fails} check(s)")
    raise SystemExit(1)
print("SMALLCAP SMOKE OK")
