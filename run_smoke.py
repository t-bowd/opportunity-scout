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
check("broker disabled without keys — all ops no-op", _check_broker_fail_soft)

if failures:
    print(f"\nSMOKE FAILED — {len(failures)} issue(s)")
    raise SystemExit(1)
print("\nSMOKE OK")
