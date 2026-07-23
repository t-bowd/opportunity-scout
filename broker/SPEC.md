# Broker Execution — Alpaca integration spec

**Status:** proposed, awaiting sign-off (2026-07-24). Build starts once §9 decisions land.

**Goal.** Move execution of the **US** portion of the main paper book from the
once-a-day Yahoo poll to **Alpaca**, so the trailing stop and the −12% hard stop
become **broker-native resting orders** that fill *intraday* — closing the
gap-through hole that let XMTR close +8pts below its floor and LRV.AX blow past
its −12% stop to −18.6%. ASX names cannot route to Alpaca and stay on the
existing simulator.

This is the "broker even if it's still paper" idea from the 2026-07-24 session:
Alpaca's **paper** account exposes the *same* API as live, so we get real
intraday stop execution with zero real money, and the path to live later is a
credentials change, not a rewrite.

---

## 1. The core shift — control inverts

Today `paper_trader/exit.py` **is** the executor: it polls a price, decides the
trailing exit, and writes the fill to Supabase. After cutover, for US names:

- **Our code submits orders; Alpaca executes them.** When a position arms
  (+20%), we submit a resting `trailing_stop` SELL (`trail_percent = 8`). Alpaca
  then tracks the intraday high-water mark itself and fills the instant price
  drops 8% — no poll, no gap-through.
- **The daily run's job changes from "decide exits" to "reconcile + submit".**
  It reads what Alpaca already did since the last run, records those fills in
  Supabase, then submits any new orders.

## 2. Source of truth — Supabase stays the book of record

**DECIDED.** Supabase `paper_positions` remains the single source of truth.
Alpaca is the *execution venue*, not the ledger. Every downstream reader —
`feedback`, `pnl_tracker`, `analysis.yml`, snapshots, graduation stats — keeps
reading Supabase unchanged. The daily run **reconciles Alpaca fills *into*
Supabase**; it never makes Alpaca the primary store. This preserves the entire
learning loop with no migration.

## 3. Scope — US only; ASX unchanged

**DECIDED.** Alpaca is US equities + crypto only. ASX names (LRV.AX etc.) keep
the current simulator (`entry.py` sizing + `exit.py` daily-poll trailing/stop).
The gap-through fix reaches the US book only; this is an accepted limitation of
staying serverless (see the IBKR rejection in the session notes — its REST API
needs an always-on authenticated gateway, incompatible with a daily cron).

## 4. The daily flow after cutover

`run_daily.py` gains a **reconcile** step and reroutes US entries/exits:

1. **`broker.reconcile()` (NEW, runs first).** List Alpaca orders filled since
   the last run. For each filled SELL, close the matching Supabase position with
   the *real* fill price, fill time, and reason (trailing_stop → `closed_trail`,
   stop → `closed_stop`). Match by stored `broker_order_id`.
2. **Exits.** ASX + any US legacy-sim positions run the existing poll logic. For
   **broker-managed US positions**, the poll no longer decides trailing/hard
   stops (Alpaca owns them); the run only:
   - **arms** — a US position that just crossed +20% with no resting trailing
     order → submit `trailing_stop` (trail 8%), cancel-and-replace the resting
     −12% stop;
   - **time-exits** — days_held ≥ pattern horizon and not armed → submit a
     market SELL (no native "sell after N days" order exists).
3. **Entries.** New US picks → `broker.submit_buy()` (market DAY order; the daily
   run fires mid-US-session at ~13:10 ET, so it fills intraday). On fill, insert
   the Supabase position with the real fill price + `broker_order_id`, then
   submit the resting −12% `stop`. ASX picks → existing simulator.
4. **Snapshot.** Reads Supabase (US now carries real Alpaca fills) for the book;
   optionally cross-checks against `broker.get_account()` for drift.

## 5. Exit orders on Alpaca — lifecycle

A position can't rest **both** a −12% stop and a trailing stop as two independent
SELLs (they'd oversell). So:

- **On entry fill:** submit a resting `stop` SELL at entry − 12% (USD).
- **On arm (+20%):** cancel that stop, submit a `trailing_stop` SELL
  (`trail_percent = 8`). From here Alpaca trails the peak.
- **Cancel-and-replace is safe** because we reconcile once daily and guard with
  `client_order_id`; there's no window where the position is both un-stopped and
  believed-stopped that a daily run wouldn't catch.

Net effect: **the −12% hard stop also becomes intraday-native for US names**, so
the LRV-style gap-through is fixed for both stop types, not just the trailing.

## 6. Legacy open positions at cutover — the one nuance of "direct cutover"

There are ~13 US positions **already open in the simulator**. We do **not** fake
a backfill into Alpaca (no honest entry basis). **DECISION NEEDED (§9-A):**

- **A1 (recommended): wind-down.** New US entries go through Alpaca immediately;
  the existing open US positions ride out on the simulator until they close
  naturally. No fake fills; the book converges to fully-Alpaca as legacy names
  exit. "Direct cutover" then means *new execution* is Alpaca from day one.
- **A2: adopt.** Buy the existing US positions in Alpaca paper at today's price.
  Rejected unless you want it — it rewrites entry bases and corrupts the P&L
  history and the feedback rows.

## 7. Config, secrets, safety

- **Secrets (you provide — I never touch them):** `ALPACA_API_KEY`,
  `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`. Added to `.env.local` and GitHub
  Actions secrets, exactly like the Supabase/Gemini keys.
- **Paper vs live is one env var.** `ALPACA_BASE_URL` =
  `https://paper-api.alpaca.markets` (default) or the live host. **Going live is
  never automatic** — it requires changing that URL and the keys by hand.
- **Fail soft.** If the Alpaca keys are absent or the API errors, broker ops
  no-op and log; the ASX simulator, snapshot, and the rest of the daily run
  continue. Absent keys ⇒ the book behaves exactly as it does today (so nothing
  breaks before you've set the account up).
- **Idempotent.** Every submitted order carries a `client_order_id` derived from
  the opportunity/position id, so a re-run or retry never double-buys or
  double-sells.
- **Thin REST client, no heavy SDK.** `broker/alpaca.py` wraps the handful of
  endpoints we use with `requests` (matches the repo's "duplicate, don't drag in
  deps" ethos; avoids the `alpaca-py` dependency).

## 8. Schema — migration 004

`paper_positions` gains broker bookkeeping (all nullable; ASX + legacy rows leave
them NULL and behave as today):

```sql
ALTER TABLE paper_positions
  ADD COLUMN IF NOT EXISTS broker            TEXT DEFAULT 'sim',  -- 'sim' | 'alpaca'
  ADD COLUMN IF NOT EXISTS broker_order_id   TEXT,   -- the entry BUY order
  ADD COLUMN IF NOT EXISTS broker_exit_order_id TEXT; -- current resting stop / trailing SELL
```

No new status values needed — broker fills map onto the existing
`closed_trail` / `closed_stop` / `closed_time`.

## 9. Decisions to resolve before build

- **A. Legacy US positions** — A1 wind-down (recommended) vs A2 adopt. See §6.
- **B. Native −12% hard stop for US** — recommended YES (§5): it fixes the
  LRV-style gap-through on the hard stop too. Say no and we keep the −12% stop
  poll-driven and only the trailing stop goes native.
- **C. Entry order type** — market DAY (recommended; fills intraday at ~13:10 ET)
  vs limit at last-close + buffer (avoids a bad market fill on a thin name, but
  may not fill).

## 10. Build order (once §9 lands)

1. `broker/config.py` + `broker/alpaca.py` (thin REST client) + fake client for tests.
2. Migration 004 (you run it in the Supabase SQL editor, like 002/003).
3. `broker/reconcile.py` — fills → Supabase closes.
4. Wire `run_daily.py`: reconcile step; US route in entry/exit; ASX untouched.
5. Pure-logic smoke additions (arm/stop/replace decision, reconcile mapping)
   against the fake client — no network. Integration test needs your paper keys.
6. First live paper run; verify a resting order appears in the Alpaca dashboard.

## 11. What we're honest about

- Alpaca **paper** fills are simulated off real quotes — far better than our
  Yahoo-close model, but still not live liquidity.
- **No ASX.** The gap-through fix does not reach ASX micro-caps; those remain the
  worst-case names and stay on the daily poll.
- Going live later adds AUD→USD funding FX (Rapyd, USD-only balance) and the
  usual W-8BEN / PDT considerations — out of scope here; this spec is paper.
