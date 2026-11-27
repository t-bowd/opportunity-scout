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
def _check_paper_momentum_instrumentation():
    # Must never raise, whatever the ticker or network does — it runs inside the
    # real-money exit loop and an exception there would leave positions unmanaged.
    assert pexit._safe_position_momentum("") is None
    assert pexit._safe_position_momentum("$$NOT_A_TICKER$$") is None
    # Empty batch is a no-op rather than a malformed write.
    from db import client as dbc
    assert dbc.record_paper_momentum([]) is None
check("exit paper momentum capture is fail-soft + no-ops empty",
      _check_paper_momentum_instrumentation)
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
    # Paper: plain 2x base, no risk/budget/equity bound.
    assert sc(entry.BASE_POSITION_AUD, False, 0.0, 0.0) == entry.BASE_POSITION_AUD * 2
    # Live, no stop supplied: size-only ceiling, hard limits still bind.
    assert sc(base, True, 5000.0, 20000.0) == base * 2 == 400.0
    assert sc(base, True, 150.0, 20000.0) == 150.0                      # cash-bound
    assert sc(base, True, 5000.0, 1000.0) == entry.LIVE_MAX_SINGLE_NAME_FRAC * 1000.0
    # THE RISK BOUND. Replays the real NBIS case: $1,749 equity, 18% stop, risk-sized
    # target ~$126. A size-only ceiling allowed $252 and NBIS went in at $242 — 2.49%
    # of equity against a 1.30% target. The risk bound must now refuse it.
    eq, stop = 1749.0, 18.0
    target = entry.LIVE_RISK_PER_TRADE_FRAC * eq / (stop / 100.0)
    ceil_risk = sc(target, True, 5000.0, eq, stop)
    assert ceil_risk < 242.0, "NBIS-style stretch must now be refused"
    # ...and whatever it does allow must stay inside the risk multiple.
    assert ceil_risk * stop / 100.0 <= (entry.SINGLE_SHARE_STRETCH_RISK_MULT
                                       * entry.LIVE_RISK_PER_TRADE_FRAC * eq) + 1e-9
    # An expensive but CALM name (tight stop) still gets through — AMR/RSG, the cases
    # the stretch exists for. This is the behaviour the risk bound must NOT break.
    calm = 10.0
    t_calm = entry.LIVE_RISK_PER_TRADE_FRAC * eq / (calm / 100.0)
    assert sc(t_calm, True, 5000.0, eq, calm) >= 330.0, "calm expensive name must still fit"
check("entry._single_share_ceiling (risk-bounded stretch; calm expensive still fits)",
      _check_single_share_ceiling)

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
    assert bexec.account_since_inception_usd() is None
check("broker disabled without keys — all ops no-op", _check_broker_fail_soft)

def _check_exit_order_state():
    eos = bexec._exit_order_state

    class _Cli:
        def __init__(self, order): self._o = order
        def get_order(self, oid):
            if self._o is None:
                raise balpaca.BrokerError("GET /v2/orders/x -> 404: not found")
            return self._o

    def st(order, oid="o1"):
        return eos(_Cli(order), oid)

    # A queued market sell is STILL WORKING — the case that wedged BWMX for four
    # days. Submitted after the close, it waits hours for the open, and the pre-open
    # retry used to cancel it and then fail to resubmit.
    for s_ in ("new", "accepted", "pending_new", "partially_filled", "held"):
        assert st({"type": "market", "side": "sell", "status": s_}) == "pending_sell", s_
    # Terminal orders must read "gone", NOT "cancel me": Alpaca answers 422 "not
    # cancelable" for those and cancel_order only swallows 404, so trying would
    # abort the submit and keep the position wedged. This is BWMX's current state.
    for s_ in ("filled", "canceled", "expired", "rejected", "done_for_day"):
        assert st({"type": "market", "side": "sell", "status": s_}) == "gone", s_
    # A live stop / trailing stop is not a time exit — cancel it, then sell.
    assert st({"type": "stop", "side": "sell", "status": "new"}) == "live_other"
    assert st({"type": "trailing_stop", "side": "sell", "status": "new"}) == "live_other"
    # ...but a terminal stop is still "gone", not "live_other".
    assert st({"type": "stop", "side": "sell", "status": "filled"}) == "gone"
    # Unreadable / vanished order -> "gone", so we submit fresh and self-heal.
    assert st(None) == "gone"
    # An UNKNOWN status must read as still-live: guessing "finished" would cancel an
    # order about to fill, which is the expensive direction to be wrong in.
    assert st({"type": "market", "side": "sell", "status": "some_new_state"}) == "pending_sell"
check("broker._exit_order_state — queued sell left alone, dead order not cancelled", _check_exit_order_state)

def _check_parse_inception():
    pi = bexec._parse_inception
    from datetime import datetime as _dt, timezone as _tz
    day = 86400
    # Derived, not hard-coded: a stale epoch literal fails the date assertion for
    # a reason that has nothing to do with the code under test.
    t0 = int(_dt(2026, 8, 4, tzinfo=_tz.utc).timestamp())
    ts = [t0 - 2 * day, t0 - day, t0, t0 + day, t0 + 2 * day]
    # Two zero-equity days before funding: the account wasn't invested yet, so
    # dating the book from them would hand the benchmark a free head start.
    got = pi({"timestamp": ts, "equity": [0, 0, 1000.0, 900.0, 800.0]})
    assert got["start_date"] == "2026-08-04", got
    assert (got["start_equity"], got["equity"], got["days"]) == (1000.0, 800.0, 2), got
    assert got["net_flows_usd"] == 0.0, got

    # The deposit that FUNDED the account lands on day one and already is
    # start_equity. Counting it again nets the opening balance to zero.
    same_day = pi({"timestamp": ts, "equity": [0, 0, 1000.0, 900.0, 800.0],
                   "cashflow": {"CSD": [0, 0, 1000.0, 0, 0]}})
    assert same_day["net_flows_usd"] == 0.0, same_day
    assert same_day["start_equity"] == 1000.0, same_day

    # A LATER deposit is real new capital and must be subtracted from the gain.
    later = pi({"timestamp": ts, "equity": [0, 0, 1000.0, 1500.0, 1400.0],
                "cashflow": {"CSD": [0, 0, 1000.0, 500.0, 0], "CSW": [0, 0, 0, 0, -50.0]}})
    assert later["net_flows_usd"] == 450.0, later
    # The capital base is start + later transfers, NOT start alone: $1,450 in,
    # $1,400 out = -3.4%, where Alpaca's own equity/base_value - 1 would say +40%
    # by counting the deposit as profit.
    assert later["invested_usd"] == 1450.0, later
    assert later["return_pct"] == -3.45, later
    # No transfers -> the base is just the opening equity and the % is exact.
    assert got["invested_usd"] == 1000.0 and got["return_pct"] == -20.0, got
    # Withdrawn to nothing -> no base to divide by, and no fabricated percentage.
    emptied = pi({"timestamp": ts, "equity": [0, 0, 1000.0, 500.0, 1.0],
                  "cashflow": {"CSW": [0, 0, 0, 0, -1000.0]}})
    assert emptied["invested_usd"] == 0.0 and emptied["return_pct"] is None, emptied

    # Unusable payloads yield None, never a bogus number.
    assert pi({}) is None
    assert pi({"timestamp": ts, "equity": [1.0]}) is None          # length mismatch
    assert pi({"timestamp": ts, "equity": [0, 0, 0, 0, 0]}) is None  # never funded
    assert pi({"timestamp": ts[:1], "equity": [100.0]}) is None     # one point
    # postgrest/JSON can hand back strings and nulls in the series.
    coerced = pi({"timestamp": ts, "equity": [None, "0", "1000.0", None, "800.0"]})
    assert (coerced["start_equity"], coerced["equity"]) == (1000.0, 800.0), coerced
check("broker._parse_inception dates from funding, nets later flows only", _check_parse_inception)

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
def _check_min_score_for():
    ms = entry._min_score_for
    # THE invariant: a bearish regime must NOT raise the live floor. It used to be
    # max(BEARISH_MIN_SCORE, LIVE_MIN_SCORE) = max(15, 15), which changed nothing
    # live and so hid the problem; at a floor of 13 that expression would quietly
    # push real money back into the band four samples say is the worst one.
    assert ms(live=True, bearish=True) == ms(live=True, bearish=False), \
        "the market regime must never raise the live score floor"
    assert ms(live=True, bearish=True) == entry.LIVE_MIN_SCORE
    # Paper keeps the regime bump.
    assert ms(live=False, bearish=True) == entry.BEARISH_MIN_SCORE
    assert ms(live=False, bearish=False) == entry.MIN_SCORE
    # Real money must never be held to a STRICTER bar than paper, which is what
    # cost the live book its only positive band for four months.
    assert entry.LIVE_MIN_SCORE <= entry.MIN_SCORE, \
        "live floor above paper's re-excludes the 13-14 band"
check("entry._min_score_for — regime never raises the live floor", _check_min_score_for)

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

# --- 6. Analysis helpers --------------------------------------------------------
# run_analysis / run_hold_analysis decide nothing by themselves, but the hold
# backtest is the evidence a real-money holding-period change would rest on, so its
# simulator gets the same treatment as the exit rules it mirrors.
from datetime import date, timedelta

from paper_trader import exit as sc_exit_main
import run_analysis as ra
import run_hold_analysis as rha

def _check_role_class():
    rc = ra._role_class
    assert rc(["CEO"]) == "ceo_cfo"
    assert rc(["Chief Financial Officer"]) == "ceo_cfo"
    assert rc(["President and COO"]) == "ceo_cfo"
    assert rc(["officer"]) == "other_officer"
    assert rc(["VP Sales"]) == "other_officer"
    assert rc(["director"]) == "director"
    assert rc(["10% owner"]) == "ten_pct_owner"
    # Most senior role wins when an insider wears several hats.
    assert rc(["director", "CEO"]) == "ceo_cfo"
    assert rc(["director", "VP Sales"]) == "other_officer"
    assert rc(["10% owner", "director"]) == "director"
    # edgar.py's no-flags fallback carries no information — must not become a bucket.
    assert rc(["insider"]) is None
    assert rc([]) is None and rc(None) is None
check("run_analysis._role_class ranks seniority, ignores the 'insider' fallback", _check_role_class)

def _flat_bars(entry: date, n: int, high: float, low: float, close: float):
    return [(entry + timedelta(days=i), high, low, close) for i in range(1, n + 1)]

def _check_simulate_stop():
    e = date(2026, 1, 1)
    bars = [(date(2026, 1, 2), 101.0, 99.0, 100.0),
            (date(2026, 1, 5), 95.0, 85.0, 86.0)]
    o = rha._simulate(e, 100.0, 12.0, bars, None)
    assert o["reason"] == "stop_loss", o
    # Fills AT the stop level (88), then pays exit slippage.
    assert abs(o["ret"] - (88.0 * 0.995 / 100.0 - 1) * 100) < 1e-9, o
    # A gap-down day must be caught by the LOW, not the close — a close-only poll
    # is exactly the paper/live divergence this backtest exists to avoid.
    gap = rha._simulate(e, 100.0, 12.0, [(date(2026, 1, 2), 90.0, 80.0, 89.0)], None)
    assert gap["reason"] == "stop_loss", gap

def _check_simulate_trail():
    e = date(2026, 1, 1)
    bars = [(date(2026, 1, 2), 130.0, 100.0, 128.0),    # arms the trail (+30% peak)
            (date(2026, 1, 5), 131.0, 115.0, 116.0)]    # falls through 130*0.92
    o = rha._simulate(e, 100.0, 12.0, bars, None)
    assert o["reason"] == "trailing_stop", o
    assert abs(o["ret"] - (119.6 * 0.995 / 100.0 - 1) * 100) < 1e-9, o
    # No same-bar look-ahead: day 1's high is what ARMS the trail, so day 1's own
    # low (100, far under 130*0.92) must NOT be able to trigger it.
    assert o["exit_date"] == date(2026, 1, 5), o

def _check_simulate_breakeven():
    e = date(2026, 1, 1)
    bars = [(date(2026, 1, 2), 115.0, 100.0, 114.0),   # peak +15% ≥ 12% stop → ratchet
            (date(2026, 1, 5), 114.0, 99.0, 99.5)]     # dips under entry
    o = rha._simulate(e, 100.0, 12.0, bars, None)
    assert o["reason"] == "breakeven_stop", o
    # Scratched at entry rather than riding all the way back down to −12%.
    assert abs(o["ret"] - (100.0 * 0.995 / 100.0 - 1) * 100) < 1e-9, o

def _check_simulate_time_and_window():
    e = date(2026, 1, 1)
    flat = _flat_bars(e, 70, 101.0, 99.0, 100.0)
    o = rha._simulate(e, 100.0, 12.0, flat, 60)
    assert o["reason"] == "time_exit" and o["days_held"] == 60, o
    # No calendar limit → runs to the edge of the available path.
    o2 = rha._simulate(e, 100.0, 12.0, flat, None)
    assert o2["reason"] == "window_end" and o2["days_held"] == 70, o2
    # Shortening the limit must actually shorten the hold (the whole point of the sweep).
    assert rha._simulate(e, 100.0, 12.0, flat, 20)["days_held"] == 20

def _check_simulate_reprieve():
    # Green (+15% peak), still pinned to its peak, past the 60d limit: the reprieve
    # must spare it rather than time-exiting a still-developing winner.
    e = date(2026, 1, 1)
    bars = _flat_bars(e, 70, 115.0, 110.0, 114.0)
    o = rha._simulate(e, 100.0, 12.0, bars, 60)
    assert o["reason"] == "window_end", o
    # ...but the reprieve is BOUNDED — give it a longer path and it still exits at
    # the limit + TIME_EXIT_REPRIEVE_DAYS, never held forever.
    longer = _flat_bars(e, 140, 115.0, 110.0, 114.0)
    o2 = rha._simulate(e, 100.0, 12.0, longer, 60)
    assert o2["reason"] == "time_exit", o2
    assert o2["days_held"] == 60 + rha.TIME_EXIT_REPRIEVE_DAYS, o2

check("run_hold_analysis._simulate — stop fills on the LOW", _check_simulate_stop)
check("run_hold_analysis._simulate — trail arms on the high, no same-bar exit", _check_simulate_trail)
check("run_hold_analysis._simulate — breakeven ratchet scratches at entry", _check_simulate_breakeven)
check("run_hold_analysis._simulate — time exit honours the swept limit", _check_simulate_time_and_window)
check("run_hold_analysis._simulate — reprieve spares a green climber, but is bounded", _check_simulate_reprieve)

def _check_simulate_geometry_knobs():
    e = date(2026, 1, 1)
    # A name that runs to +15% then rolls back to entry. With the LIVE geometry
    # (ratchet at 1.0x an 18% stop) the ratchet never arms — +15% < +18% — so it
    # rides all the way down to the full stop. This is the near-dead-ratchet case.
    bars = [(date(2026, 1, 2), 115.0, 100.0, 114.0),
            (date(2026, 1, 5), 114.0, 80.0, 81.0)]
    live = rha._simulate(e, 100.0, 18.0, bars, 60)
    assert live["reason"] == "stop_loss", live
    # Arming at 0.6x (i.e. +10.8%) instead turns that same path into a scratch.
    early = rha._simulate(e, 100.0, 18.0, bars, 60, ratchet_mult=0.6)
    assert early["reason"] == "breakeven_stop", early
    assert early["ret"] > live["ret"], (early, live)

    # Trail arm: a +25% peak arms under the live +20% but not under a +30% arm.
    climb = [(date(2026, 1, 2), 125.0, 100.0, 124.0),
             (date(2026, 1, 5), 124.0, 100.0, 101.0)]
    assert rha._simulate(e, 100.0, 12.0, climb, 60)["reason"] == "trailing_stop"
    assert rha._simulate(e, 100.0, 12.0, climb, 60,
                         trail_arm=30.0)["reason"] != "trailing_stop"

    # Trail width: a wider trail must survive a dip that a narrow one sells into.
    narrow = rha._simulate(e, 100.0, 12.0, climb, 60, trail_pct=5.0)
    wide = rha._simulate(e, 100.0, 12.0, climb, 60, trail_pct=25.0)
    assert narrow["reason"] == "trailing_stop" and wide["reason"] != "trailing_stop"

    # Vol-scaled trail: 3x a 5% ATR == a 15% trail, so they must agree exactly.
    scaled = rha._simulate(e, 100.0, 12.0, climb, 60, trail_atr_mult=3.0, atr_pct=5.0)
    flat15 = rha._simulate(e, 100.0, 12.0, climb, 60, trail_pct=15.0)
    assert scaled["reason"] == flat15["reason"], (scaled, flat15)
    assert abs(scaled["ret"] - flat15["ret"]) < 1e-9
    # ...and the 4% floor must hold, so a near-zero ATR can't produce a hair trigger.
    floored = rha._simulate(e, 100.0, 12.0, climb, 60, trail_atr_mult=3.0, atr_pct=0.01)
    assert abs(floored["ret"] - rha._simulate(e, 100.0, 12.0, climb, 60,
                                              trail_pct=4.0)["ret"]) < 1e-9
    # A missing ATR must fall back to the flat width, never to 0.
    no_atr = rha._simulate(e, 100.0, 12.0, climb, 60, trail_atr_mult=3.0, atr_pct=None)
    assert abs(no_atr["ret"] - rha._simulate(e, 100.0, 12.0, climb, 60)["ret"]) < 1e-9

    # Defaults must reproduce the live book exactly — every sweep leans on this.
    assert rha.LIVE_GEOMETRY["trail_arm"] == sc_exit_main.TRAILING_STOP_ACTIVATE_PCT
    assert rha.LIVE_GEOMETRY["trail_pct"] == sc_exit_main.TRAILING_STOP_TRAIL_PCT
    assert rha.LIVE_GEOMETRY["ratchet_mult"] == 1.0
    assert rha.LIVE_GEOMETRY["trail_atr_mult"] is None
check("run_hold_analysis._simulate geometry knobs (ratchet/arm/trail/vol-scaled)",
      _check_simulate_geometry_knobs)

def _check_alpha_per_month():
    apm = rha._alpha_per_month
    # Total alpha / total days: +10% over 30d and +20% over 60d is +10%/month.
    assert abs(apm([{"a_IWM": 10.0, "days_held": 30},
                    {"a_IWM": 20.0, "days_held": 60}], "a_IWM") - 10.0) < 1e-9
    # A 3-day −18% stop must be weighted by its 3 days, not annualised into −180%.
    # Mean-of-ratios would report ≈+4.4% here; day-weighting gives ≈+1%.
    mixed = apm([{"a_IWM": -18.0, "days_held": 3},
                 {"a_IWM": 20.0, "days_held": 60}], "a_IWM")
    assert 0.5 < mixed < 1.5, mixed
    assert apm([], "a_IWM") is None
    assert apm([{"a_IWM": None, "days_held": 5}], "a_IWM") is None
    assert apm([{"a_IWM": 5.0, "days_held": 0}], "a_IWM") is None   # no div-by-zero
check("run_hold_analysis._alpha_per_month day-weights, never annualises a stop",
      _check_alpha_per_month)

def _check_dual_benchmark():
    # Both benchmark symbols must be distinct and wired into the row formatter, or a
    # table would silently print one benchmark twice.
    assert ra.BENCH_SPY != ra.BENCH_IWM
    assert ra._mean_or_none([None, None]) is None
    assert ra._mean_or_none([0.1, None, 0.3]) == 0.2
    # _row reads both alpha keys; a record missing one must not raise.
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        ra._row("t", [{"ret": 0.1, "alpha": 0.02, "alpha_iwm": None}] * ra.MIN_N)
    assert "n/a" in buf.getvalue(), buf.getvalue()   # the missing IWM alpha shows as n/a
check("run_analysis dual SPY/IWM benchmark plumbing", _check_dual_benchmark)

def _check_hold_sweep_shape():
    # The sweep's longest limit must fit inside the required path, or short limits
    # would be scored on names the long limit never finished.
    assert rha.MAX_SWEEP_DAYS == max(h for h in rha.HOLD_SWEEP if h is not None)
    assert rha.REQUIRED_PATH_DAYS >= rha.MAX_SWEEP_DAYS + rha.TIME_EXIT_REPRIEVE_DAYS
check("run_hold_analysis sweep fits inside the required price path", _check_hold_sweep_shape)


# --- 7. Snapshot: the realised ledger and the graduation gate -------------------

from paper_trader import snapshot as snap

# The real-money ledger as at 2026-11-06, read off the broker/reconcile CLOSED
# lines in the daily run logs. Frozen fixture: it pins the formatter's arithmetic
# against a record derived independently of this code, so a regression in the
# split shows up as a diff against known-good numbers rather than against itself.
_LIVE_LEDGER_2026_11_06 = [
    ("stop_loss", -24.01), ("stop_loss", -23.78), ("stop_loss", -23.75),
    ("stop_loss", -25.97), ("stop_loss", -27.73), ("stop_loss", -24.74),
    ("stop_loss", -21.88), ("stop_loss", -20.93), ("stop_loss", -16.11),
    ("stop_loss", -23.71), ("stop_loss", -24.75), ("stop_loss", -14.28),
    ("stop_loss", -48.81), ("stop_loss", -20.70),
    ("time_exit", -11.33), ("time_exit", -2.91), ("time_exit", 9.22),
    ("time_exit", -5.00), ("time_exit", 7.06), ("time_exit", 9.28),
    ("trailing_stop", 36.06),
]


def _closes(rows, broker="alpaca", status="closed"):
    return [{"pnl_aud": pnl, "exit_reason": reason, "broker": broker, "status": status}
            for reason, pnl in rows]


def _realised_lines(closes):
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        snap._print_realised("LIVE", closes)
    return buf.getvalue().strip().splitlines()


def _check_realised_ledger():
    lines = _realised_lines(_closes(_LIVE_LEDGER_2026_11_06))
    assert len(lines) == 2, lines
    # 21 closes, 4 winners, -$298.77 total -> -$14.23 a trade.
    assert "21 closed, 19% win, $-299 total, expectancy $-14.23" in lines[0], lines[0]
    # Worst reason first: stops are the whole loss, the two run-it-out exits are ahead.
    assert lines[1].endswith(
        "stop_loss 14× 0w $-341  time_exit 6× 3w $+6  trailing_stop 1× 1w $+36"
    ), lines[1]
check("snapshot._print_realised splits by exit reason, worst first", _check_realised_ledger)


def _check_realised_edges():
    # No live closes yet -> silent, not a divide-by-zero.
    assert _realised_lines([]) == []
    # A row with no exit_reason must bucket, not raise.
    lines = _realised_lines([{"pnl_aud": -5.0, "exit_reason": None, "broker": "alpaca"}])
    assert "unknown 1× 0w $-5" in lines[1], lines[1]
    # pnl_aud can arrive as a Decimal/str from postgrest — the formatter coerces.
    lines = _realised_lines([{"pnl_aud": "12.60", "exit_reason": "time_exit"}])
    assert "1 closed, 100% win, $+13 total" in lines[0], lines[0]
check("snapshot._print_realised edges (empty, no reason, string pnl)", _check_realised_edges)


def _check_graduation_gate():
    # The gate counts the LIVE book only. Sim closes and manual interventions
    # must not advance it — counting every close is what made it a dead
    # instrument, printing "0 trades until" forever once the book passed 20.
    rows = [("stop_loss", -1.0)] * 3
    mixed = (_closes(rows, broker="alpaca")
             + _closes(rows, broker="sim")
             + _closes(rows, broker="alpaca", status="closed_manual"))
    strategy = [p for p in mixed if p.get("status") != "closed_manual"]
    live = [p for p in strategy if p.get("broker") == "alpaca"]
    assert len(strategy) == 6 and len(live) == 3, (len(strategy), len(live))
    # The gate is a real threshold, and it sits below the 21 closes already banked
    # (so the live book reads DUE rather than counting down forever).
    assert snap.GRADUATION_MIN_LIVE_CLOSES > 0
    assert len(_LIVE_LEDGER_2026_11_06) >= snap.GRADUATION_MIN_LIVE_CLOSES
check("snapshot graduation gate counts real-money closes only", _check_graduation_gate)


def _inception_lines(inception, benchmarks, fx=0.80):
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        snap._print_since_inception("LIVE", inception, benchmarks, fx)
    return buf.getvalue().strip().splitlines()


def _check_since_inception():
    clean = {"start_date": "2026-08-04", "start_equity": 1000.0, "equity": 800.0,
             "net_flows_usd": 0.0, "invested_usd": 1000.0, "return_pct": -20.0,
             "days": 107}
    lines = _inception_lines(clean, [("SPY", 5.0), ("IWM", 10.0)])
    assert len(lines) == 2, lines
    # USD is the account's own currency; the AUD value is reconciliation only.
    assert lines[0] == ("[paper/snapshot] LIVE on capital: $1,000 in -> $800 USD "
                        "($1,000 AUD) = -20.0% since 2026-08-04 (107d)"), lines[0]
    # The whole point of the line: down 20% while the market rose is -25% alpha,
    # a figure no per-position comparison in this file can produce.
    assert lines[1] == ("[paper/snapshot] LIVE vs market over the same days: "
                        "SPY +5.0%  IWM +10.0%  ->  alpha SPY -25.0%  IWM -30.0%"), lines[1]
check("snapshot since-inception return + alpha vs SPY/IWM", _check_since_inception)


def _check_since_inception_edges():
    base = {"start_date": "2026-08-04", "start_equity": 1000.0, "equity": 1800.0,
            "net_flows_usd": 500.0, "invested_usd": 1500.0, "return_pct": 20.0,
            "days": 107}
    # A later deposit must NOT be reported as profit: $1,500 in, $1,800 out = +20%,
    # not the +80% Alpaca's own profit_loss_pct would give (equity/base_value - 1).
    lines = _inception_lines(base, [("SPY", 5.0)])
    assert "(+500 USD net transfers)" in lines[0], lines[0]
    assert "= +20.0%" in lines[0], lines[0]
    assert "profit $+300" in lines[0] and "transfer timing unknown" in lines[0], lines[0]
    assert "alpha SPY +15.0%" in lines[1], lines[1]

    # No benchmark data (Yahoo down) -> the capital line still prints alone.
    assert len(_inception_lines(base, [("SPY", None), ("IWM", None)])) == 1
    # No FX -> drop the AUD reconciliation rather than crash on a None divide.
    assert "AUD" not in _inception_lines(base, [], fx=None)[0]
    # No capital base (withdrawn to nothing) -> silent, not a fabricated figure.
    assert _inception_lines({**base, "invested_usd": 0.0, "return_pct": None}, []) == []
check("snapshot since-inception edges (transfers, no bench, no fx)", _check_since_inception_edges)

# --- 8. Graduation review: the benchmark currency leg ---------------------------

import run_graduation_review as grad

def _check_bench_leg_aud():
    bl = grad.bench_leg_aud
    # Index flat, AUD flat -> nothing happened.
    assert bl(100.0, 100.0, 0.70, 0.70) == 0.0
    # Index +10%, AUD flat -> +10% for an AUD investor too.
    assert abs(bl(100.0, 110.0, 0.70, 0.70) - 10.0) < 1e-9
    # Index flat, AUD WEAKENS 0.70 -> 0.63: a US holding is worth MORE in AUD.
    # fx0/fx1 = 1.111 -> +11.1%. Inverting this is the bug the test exists for:
    # it would report -10% and turn a currency tailwind into underperformance.
    assert abs(bl(100.0, 100.0, 0.70, 0.63) - 11.1111) < 1e-3
    # Index flat, AUD STRENGTHENS 0.63 -> 0.70 -> a US holding loses in AUD.
    assert bl(100.0, 100.0, 0.63, 0.70) < 0
    # Missing or zero inputs yield None, never a divide-by-zero or a fake 0%.
    for args in ((None, 110.0, 0.7, 0.7), (100.0, None, 0.7, 0.7),
                 (100.0, 110.0, None, 0.7), (100.0, 110.0, 0.7, None),
                 (0.0, 110.0, 0.7, 0.7), (100.0, 110.0, 0.7, 0.0)):
        assert bl(*args) is None, args
check("graduation review benchmark leg carries the FX move", _check_bench_leg_aud)

if failures:
    print(f"\nSMOKE FAILED — {len(failures)} issue(s)")
    raise SystemExit(1)
print("\nSMOKE OK")
