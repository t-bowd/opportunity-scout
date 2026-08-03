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

## 9. Decisions — RESOLVED 2026-07-24

- **A. Legacy US positions → A1 wind-down.** New US entries route to Alpaca
  immediately; the ~13 existing open US positions ride out on the simulator
  until they close naturally. No fake backfill.
- **B. Native −12% hard stop for US → YES.** The −12% stop rests as an intraday
  Alpaca `stop` order, swapped for the `trailing_stop` on arm (§5). Fixes the
  LRV-style gap-through on the hard stop too.
- **C. Entry order type → market DAY.** Fills intraday at ~13:10 ET.

## 10. Build order (once §9 lands)

1. `broker/config.py` + `broker/alpaca.py` (thin REST client) + fake client for tests.
2. Migration 004 (you run it in the Supabase SQL editor, like 002/003).
3. `broker/reconcile.py` — fills → Supabase closes.
4. Wire `run_daily.py`: reconcile step; US route in entry/exit; ASX untouched.
5. Pure-logic smoke additions (arm/stop/replace decision, reconcile mapping)
   against the fake client — no network. Integration test needs your paper keys.
6. First live paper run; verify a resting order appears in the Alpaca dashboard.

## 10a. Live (real-money) sizing — added 2026-07-31

Going live is a **different book with hard rules**, gated entirely on
`config.is_live()` (true when `ALPACA_BASE_URL` is the live host, not paper). The
key swap flips both execution AND sizing in one action — paper behaviour is
byte-for-byte unchanged (`live=False`). Decided with Tim (fully autonomous, but
hard-capped; shape = the original $2k/10, ramping up as funded):

- **Hard budget = the broker's actual cash** (`account_cash_equity_usd()`), not a
  hardcoded pool. It already nets out open positions, so it **auto-ramps** as the
  account is funded and as winners realise — no config change to grow. A broker
  read failure ⇒ 0 budget ⇒ deploy nothing this run (never guess with real money).
- **Equal-weight base ($200 AUD), NO conviction upsizing, NO over-budget override.**
  The opposite of the paper soft pool — when cash can't fund another base
  position, stop (`live_budget_exhausted`).
- **Single-name cap** = `LIVE_MAX_SINGLE_NAME_FRAC` (25%) of account equity, so a
  small account can't over-concentrate on one high-scored name.
- **10 slots** (`LIVE_MAX_POSITIONS`), vs paper's 20.
- **Higher score gate** (`LIVE_MIN_SCORE` = 15, vs paper's MIN_SCORE 13). Added
  2026-08-03 after the first live book took a score-13 smart_money name (CVNA)
  when the top pick (KRNY 19) went stale over a weekend — the 30d study flagged
  the 13-14 bucket (47% win) and smart_money (-3.3%) as the weakest profiles.
  Real money shouldn't fund them on a 4-5 position book.
- **US-only** — ASX picks are skipped (`asx_unsupported_live`); no real-money
  venue, and we must never fall back to a fake sim fill on a live book.
- **Legacy paper positions are ignored** for live counting/budget — they wind down
  in exit.py; only `broker='alpaca'` positions consume live slots.
- Result at ~$600 USD: ~4 positions; fills more on its own as the balance grows to
  the ~$2k/10 shape. Effective floor: below ~$600 AUD equity the 25% cap drops the
  size under MIN_TRADE and it stops trading (account too small).

**Sequencing rule (critical):** this build must be merged/pushed BEFORE the keys
are swapped to live — otherwise the paper $5k-pool sizer would run against the
real account. Because sizing keys off `is_live()`, the swap itself is the trigger;
nothing else to toggle.

## 11. What we're honest about

- Alpaca **paper** fills are simulated off real quotes — far better than our
  Yahoo-close model, but still not live liquidity.
- **No ASX.** The gap-through fix does not reach ASX micro-caps; those remain the
  worst-case names and stay on the daily poll.
- Going live later adds AUD→USD funding FX (Rapyd, USD-only balance) and the
  usual W-8BEN / PDT considerations — out of scope here; this spec is paper.
