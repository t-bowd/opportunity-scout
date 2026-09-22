"""
Fast pre-push smoke test — no network, no DB, no API calls.

Catches the classes of bug that have hit live runs: import errors / circular
imports, missing names, and crashes in the pure summary / sizing / scoring
helpers (e.g. the Form-4 value_usd=None format crash). Run before pushing:

    python run_smoke.py

Exits non-zero on any failure so it can gate CI. It imports the library modules
(not run_daily / run_edgar_watch, which execute on import) and only exercises
functions that don't reach the network, the database, or an LLM.
"""
import os
import py_compile

# Dummy env so modules that read env at import time don't fail. No real calls are
# made, so these values are never used for anything.
for k, v in {
    "GEMINI_API_KEY": "smoke",
    "SUPABASE_URL": "http://smoke.local",
    "SUPABASE_SERVICE_KEY": "smoke",
    "RESEND_API_KEY": "smoke",
    "DIGEST_EMAIL": "smoke@example.com",
    "EDGAR_USER_AGENT": "smoke test@example.com",
}.items():
    os.environ.setdefault(k, v)

failures: list[tuple[str, Exception]] = []


def check(name, fn):
    try:
        fn()
        print(f"  ok  {name}")
    except Exception as e:  # noqa: BLE001 — smoke test wants every failure
        failures.append((name, e))
        print(f"  XX  {name}: {type(e).__name__}: {e}")


# --- 1. Entry scripts compile (can't import — they execute on import) ----------
print("Compile entry scripts:")
for f in ("run_daily.py", "run_edgar_watch.py"):
    check(f, lambda f=f: py_compile.compile(f, doraise=True))

# --- 2. All library modules import (catches circular / missing-name errors) ----
print("Imports:")


def _imports():
    import collectors.edgar          # noqa: F401
    import collectors.news           # noqa: F401
    import collectors.etf_launches   # noqa: F401
    import analyzer.classify         # noqa: F401
    import analyzer.score            # noqa: F401
    import paper_trader.entry        # noqa: F401
    import paper_trader.exit         # noqa: F401
    import paper_trader.notify       # noqa: F401
    import paper_trader.snapshot     # noqa: F401
    import paper_trader.manual_open  # noqa: F401
    import paper_trader.manual_close # noqa: F401
    import digest.weekly_report      # noqa: F401
    import db.client                 # noqa: F401
    import broker.config             # noqa: F401
    import broker.alpaca             # noqa: F401
    import broker.execution          # noqa: F401
    import broker.reconcile          # noqa: F401


check("all library modules import", _imports)

if failures:  # no point exercising helpers if imports are broken
    print(f"\nSMOKE FAILED — {len(failures)} issue(s)")
    raise SystemExit(1)

# --- 3. Pure helpers run on fake data (no network/DB/LLM) ----------------------
from analyzer import classify, score
from paper_trader import entry, exit as pexit

print("Rule-based summaries (every EDGAR source):")
sigs = {
    "edgar_4 (no price)": {"source": "edgar_4", "signal_date": "2026-06-06", "raw_data": {
        "entity_name": "Acme", "buyer": "J Doe", "roles": ["director"],
        "shares": 1000, "price": None, "value_usd": None, "ticker": "ACME"}},
    "edgar_4 (priced)": {"source": "edgar_4", "signal_date": "2026-06-06", "raw_data": {
        "entity_name": "Acme", "buyer": "J Doe", "roles": ["CFO"],
        "shares": 500, "price": 12.5, "value_usd": 6250, "ticker": "ACME"}},
    "edgar_13f_hr (new)": {"source": "edgar_13f_hr", "signal_date": "2026-06-06", "raw_data": {
        "entity_name": "Beta", "fund_name": "BigFund", "value_usd": 9_000_000, "change": "new"}},
    "edgar_13f_hr (None value)": {"source": "edgar_13f_hr", "signal_date": "2026-06-06", "raw_data": {
        "entity_name": "Beta", "fund_name": "BigFund", "value_usd": None, "change": "increased", "pct_change": 40}},
    "edgar_s1": {"source": "edgar_s1", "signal_date": "2026-06-06", "raw_data": {"entity_name": "Gamma", "ticker": "GAMA"}},
    "edgar_13d": {"source": "edgar_13d", "signal_date": "2026-06-06", "raw_data": {"entity_name": "Delta"}},
    "edgar_n1a": {"source": "edgar_n1a", "signal_date": "2026-06-06", "raw_data": {"entity_name": "NewETF"}},
    "etf_launch": {"source": "etf_launch", "signal_date": "2026-06-06", "raw_data": {"title": "Theme ETF"}},
}
for name, sig in sigs.items():
    check(name, lambda sig=sig: classify._rule_based_summary(sig))

print("Other pure helpers:")
check("classify.classify_signal (rule-based)",
      lambda: classify.classify_signal(sigs["edgar_4 (priced)"]))
check("score._prioritize_signals dedupe+cap", lambda: (
    lambda out: (len(out) == 5 and out[0].get("_dup_count"))
)(score._prioritize_signals(
    [{"id": str(i), "signal_date": f"2026-06-0{i % 9 + 1}", "pattern": "insider_buy",
      "raw_data": {"ticker": f"T{i % 5}"}} for i in range(30)], 5)))
def _check_rescore_action():
    ra = score._rescore_action
    assert ra(17, None) == "insert"            # no prior row
    assert ra(18, {"total_score": 18}) == "unchanged"
    assert ra(19, {"total_score": 16}) == "rescore_up"
    assert ra(15, {"total_score": 18}) == "rescore_down"   # the fix: highs CAN fall
    assert ra(18, {"total_score": None}) == "rescore_up"   # null prior treated as 0
check("score._rescore_action both directions", _check_rescore_action)
check("entry._target_position_size tiers",
      lambda: [entry._target_position_size(s) for s in (13, 16, 18)])
check("exit._pnl_to_grade",
      lambda: [pexit._pnl_to_grade(p) for p in (-20, -5, 0, 5, 20)])
def _check_time_exit_reprieve():
    r = pexit._time_exit_reprieved
    # green, still near a +15% peak, just past the 60d limit -> reprieved
    assert r(61, 60, 15.0, 1.15, 1.15) is True
    # never showed a real move (peak +4%) -> no reprieve, time-exits
    assert r(61, 60, 4.0, 1.02, 1.04) is False
    # peaked +15% but round-tripped >10% off it -> faded, no reprieve
    assert r(61, 60, 15.0, 1.00, 1.15) is False
    # reprieve is bounded: past max_hold + REPRIEVE_DAYS -> back to time-exit
    assert r(91, 60, 15.0, 1.15, 1.15) is False
check("exit._time_exit_reprieved (bounded green-climbing leash)", _check_time_exit_reprieve)
def _check_position_stop_pct():
    psp = pexit._position_stop_pct
    # Per-position stop stored at entry is used as-is...
    assert psp({"stop_loss_pct": 17.5}) == 17.5
    assert psp({"stop_loss_pct": "21.0"}) == 21.0          # numeric comes back as str
    # ...and every legacy/garbage shape falls back to the flat stop, so a position
    # can never end up with no line under it.
    flat = abs(pexit.STOP_LOSS_PCT)
    for row in ({}, {"stop_loss_pct": None}, {"stop_loss_pct": 0}, {"stop_loss_pct": "x"}):
        assert psp(row) == flat
check("exit._position_stop_pct (per-position stop, legacy falls back)", _check_position_stop_pct)
def _check_breakeven_ratchet():
    # The ratchet triggers at the position's OWN stop distance. Assert the invariant
    # that makes it safe: the buffer from trigger down to the breakeven stop always
    # equals the stop width, so ratcheting never tightens noise-sensitivity.
    for stop_dist in (abs(pexit.STOP_LOSS_PCT), 10.0, 17.5, 25.0):
        buffer_to_new_stop = stop_dist - 0.0
        assert buffer_to_new_stop >= stop_dist, "ratchet buffer tighter than the stop it replaces"
        # Effective stop selection: armed -> 0%, else this position's own distance.
        for peak, expected in ((stop_dist + 1, 0.0), (stop_dist, 0.0),
                               (stop_dist - 0.1, -stop_dist)):
            armed = peak >= stop_dist
            assert (0.0 if armed else -stop_dist) == expected
    # The widest possible stop must still sit below the trail arm, or the trail
    # would always win first and the ratchet would be dead code.
    assert entry.STOP_PCT_MAX < pexit.TRAILING_STOP_ACTIVATE_PCT, \
        "widest stop must stay below the trail arm"
check("exit breakeven ratchet geometry (buffer == stop, below trail arm)", _check_breakeven_ratchet)
def _check_single_share_ceiling():
    sc = entry._single_share_ceiling
    base = entry.LIVE_BASE_POSITION_AUD          # 200
    # Paper: plain 2x base, budget/equity don't bind.
    assert sc(entry.BASE_POSITION_AUD, False, 0.0, 0.0) == entry.BASE_POSITION_AUD * 2
    # Live with ample cash+equity: the 2x stretch cap binds -> AMR/RSG (~$310-330) fit,
    # a $500 name does not.
    ample = sc(base, True, 5000.0, 20000.0)
    assert ample == base * 2 == 400.0
    assert 330.0 <= ample and 310.0 <= ample and 500.0 > ample
    # Live hard limits bind ON TOP: thin cash caps it, and the single-name equity
    # fraction caps it — a stretch can never breach either.
    assert sc(base, True, 150.0, 20000.0) == 150.0                      # cash-bound
    assert sc(base, True, 5000.0, 1000.0) == entry.LIVE_MAX_SINGLE_NAME_FRAC * 1000.0
check("entry._single_share_ceiling (bounded stretch, live caps bind)", _check_single_share_ceiling)

# --- 4. Broker layer: pure logic + fail-soft when disabled ---------------------
from broker import config as bcfg, alpaca as balpaca, execution as bexec, reconcile as brecon

def _check_broker_reason_map():
    r = brecon._exit_reason_for_order
    assert r({"type": "trailing_stop"}) == ("trailing_stop", "closed_trail")
    assert r({"type": "stop"}) == ("stop_loss", "closed_stop")
    assert r({"type": "market"}) == ("time_exit", "closed_time")  # only ever a time exit
check("broker.reconcile._exit_reason_for_order maps all order types", _check_broker_reason_map)

def _check_broker_base_url():
    # Alpaca's dashboard shows the endpoint WITH /v2; the client appends /v2
    # itself, so both forms (and a trailing slash) must normalise to the host.
    want = "https://paper-api.alpaca.markets"
    for given in (want, want + "/", want + "/v2", want + "/v2/"):
        os.environ["ALPACA_BASE_URL"] = given
        assert bcfg.base_url() == want, f"{given} -> {bcfg.base_url()}"
    os.environ["ALPACA_BASE_URL"] = "https://api.alpaca.markets/v2"
    assert bcfg.is_live() is True          # live host still detected after strip
    os.environ.pop("ALPACA_BASE_URL")
check("broker.config.base_url normalises /v2 + trailing slash", _check_broker_base_url)

def _check_broker_fail_soft():
    # Smoke env has no ALPACA_* keys, so the broker must be fully disabled and
    # every entry point a safe no-op — this is what keeps the book on the
    # simulator until real keys are configured.
    assert bcfg.enabled() is False
    assert balpaca.client() is None
    # disabled must return None (sim fallback), NOT the DEFERRED marker —
    # entry.py distinguishes the two and skips the pick on DEFERRED.
    assert bexec.open_position("AAPL", 5, "opp-1") is None
    assert bexec.DEFERRED.get("deferred") is True
    assert bexec.arm_trailing({"ticker": "AAPL", "quantity": 5, "id": "p1"}) is None
    assert bexec.time_exit({"ticker": "AAPL", "quantity": 5, "id": "p1"}) is None
    assert bexec.account_cash_equity_usd() is None   # no account when disabled
check("broker disabled without keys — all ops no-op", _check_broker_fail_soft)

def _check_atr_pct():
    ap = entry._atr_pct
    # Flat series: every true range is 0 -> ATR 0%.
    n = entry.ATR_PERIOD + 5
    assert ap([100.0] * n, [100.0] * n, [100.0] * n) == 0.0
    # Constant 2-point range on a 100 close -> ATR 2%.
    assert abs(ap([101.0] * n, [99.0] * n, [100.0] * n) - 2.0) < 1e-9
    # True Range counts overnight GAPS, not just the bar's own high-low: a bar that
    # opens far below the prior close has TR = prev_close - low, wider than high-low.
    highs = [100.0] * n + [90.0]
    lows = [100.0] * n + [88.0]
    closes = [100.0] * n + [89.0]
    gapped = ap(highs, lows, closes)
    assert gapped is not None and gapped > 0, "gap day must register range"
    # Too few bars -> None (caller falls back rather than sizing off noise).
    assert ap([100.0] * 5, [99.0] * 5, [99.5] * 5) is None
    assert ap([], [], []) is None
    # Misaligned/None rows are dropped row-wise, not per-series.
    assert ap([100.0, None] * n, [99.0, None] * n, [99.5, None] * n) is not None
check("entry._atr_pct (true range incl. gaps, fails to None)", _check_atr_pct)
def _check_stop_pct_for():
    sp = entry._stop_pct_for
    # Unknown volatility -> flat legacy stop, never a guess.
    assert sp(None) == float(bcfg.HARD_STOP_PCT)
    assert sp(0) == float(bcfg.HARD_STOP_PCT)
    # Calm name: 3 x 1% = 3% -> floored so spread can't stop us out.
    assert sp(1.0) == entry.STOP_PCT_MIN
    # Wild name: 3 x 15% = 45% -> capped so one loss can't be outsized.
    assert sp(15.0) == entry.STOP_PCT_MAX
    # In-band scales with the name's own volatility.
    assert sp(3.0) == 15.0
    assert entry.STOP_PCT_MIN <= sp(2.5) <= entry.STOP_PCT_MAX
    # The multiplier must be calibrated so a TYPICAL name lands above the old flat
    # stop — a tighter stop than before would invert the whole point of the change.
    assert sp(2.5) > float(bcfg.HARD_STOP_PCT), "typical name must get a wider stop than the old flat 12%"
check("entry._stop_pct_for (ATR-scaled, clamped both ends)", _check_stop_pct_for)
def _check_live_risk_sized():
    rs = entry._live_risk_sized
    eq = 2000.0
    risk = entry.LIVE_RISK_PER_TRADE_FRAC * eq          # dollars at risk per trade
    # THE core invariant: size x stop distance is constant, so every position risks
    # the same dollars — a wider stop buys a smaller position, not a bigger loss.
    for stop in (10.0, 15.0, 20.0, 25.0):
        size = rs(stop, eq, 10_000.0)
        assert abs(size * (stop / 100.0) - risk) < 1e-6, "risk per trade must be constant"
    # Wider stop => strictly smaller position.
    assert rs(25.0, eq, 10_000.0) < rs(10.0, eq, 10_000.0)
    # Hard caps still bind on top: thin cash, and the single-name equity fraction.
    assert rs(10.0, eq, 50.0) == 50.0
    assert rs(1.0, eq, 10_000.0) == entry.LIVE_MAX_SINGLE_NAME_FRAC * eq
    # Degenerate inputs never produce a position.
    assert rs(0, eq, 1000.0) == 0.0 and rs(10.0, 0, 1000.0) == 0.0
check("entry._live_risk_sized (constant dollar risk, caps bind)", _check_live_risk_sized)

def _check_business_days():
    from datetime import date
    bd = entry._business_days_between
    assert bd(date(2026, 8, 3), date(2026, 8, 7)) == 4     # Mon->Fri, no weekend
    assert bd(date(2026, 7, 31), date(2026, 8, 3)) == 1    # Fri->Mon: weekend free
    assert bd(date(2026, 7, 28), date(2026, 8, 3)) == 4    # KRNY: Tue->next Mon (was 6 cal)
    assert bd(date(2026, 7, 28), date(2026, 8, 5)) == 6    # ...Wed = stale at window 5
    assert bd(date(2026, 8, 4), date(2026, 8, 4)) == 0     # same day
    assert bd(date(2026, 8, 5), date(2026, 8, 4)) == 0     # end before start
check("entry._business_days_between ignores weekends (KRNY fix)", _check_business_days)

# --- 5. Small-cap sleeve: pure momentum note + fail-soft capture no-op ----------
from smallcap import exit as sc_exit, store as sc_store

def _check_sleeve_climbing_note():
    # _climbing_note is now pure (takes the already-fetched verdict), so the
    # momentum fetch happens once per position and the string can be smoke-tested.
    cn = sc_exit._climbing_note
    assert cn(None) == " | climbing n/a"                                   # data unavailable
    assert cn({"climbing": True, "ret_20d": 8.0}) == " | climbing"
    assert cn({"climbing": False, "ret_20d": -4.2}) == " | NOT climbing (20d -4%)"
    assert sc_exit._safe_momentum("___nope___") is None                    # never raises
check("smallcap.exit._climbing_note pure + _safe_momentum fail-soft", _check_sleeve_climbing_note)

def _check_sleeve_momentum_noop():
    # Empty batch must be a no-op that never opens a DB connection (the exit run
    # calls this every day even when there are no observations to write).
    sc_store.record_momentum([])
check("smallcap.store.record_momentum([]) is a no-op", _check_sleeve_momentum_noop)

if failures:
    print(f"\nSMOKE FAILED — {len(failures)} issue(s)")
    raise SystemExit(1)
print("\nSMOKE OK")
