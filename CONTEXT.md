# Opportunity Scout — Project Context

Brief for Claude on what has been built. Keep this current when design decisions change.

---

## What this is

A fully automated stock-market opportunity agent + **paper-trading** layer, running on
GitHub Actions at ~$0/month. Daily it: collects signals from SEC EDGAR and financial
news, classifies them, scores the best with Gemini, then **automatically opens and
manages paper positions** against a $2,000 AUD virtual pool. The goal is to validate
the strategy on paper (20 closed trades, positive expectancy = "graduation") before any
real capital. Trading is fully automated — there is no manual opportunity review.

---

## Pipeline (daily)

```
collect (EDGAR + news)  →  classify  →  score (Gemini)  →  paper exits  →  paper entries  →  snapshot
```

`run_daily.py` runs all of it. Entries/exits also emit position email alerts. A weekly
job sends a portfolio digest. An hourly EDGAR watcher collects+classifies early (no alerts).

---

## Signal patterns & sources

| Pattern | Source | Meaning |
|---|---|---|
| `insider_buy` | SEC Form 4 | Insider **open-market purchase** (code P only — see below) |
| `smart_money` | SEC 13F-HR | Fund **newly initiated / materially increased** a holding (quarter diff) |
| `activist` | SEC 13D | Activist took a large stake |
| `s1_filed` | SEC S-1 | Company filing to go public |
| `spin_off` | news | Corporate spin-off |
| `thematic_etf` / `etf_launch` | SEC N-1A + ETF RSS | New themed ETF |
| `pre_ipo_proxy` | news | Private-company news with a listed proxy angle |

---

## Signal collection — key decisions (collectors/edgar.py)

- **Form 4 = genuine buys only.** Each Form 4 is fetched and parsed; we emit a signal
  ONLY if it contains a `nonDerivativeTransaction` with **code P** (discretionary
  open-market common-stock purchase). Grants (A), sales (S), option exercises (M),
  gifts (G), tax (F), and derivative/swap "P"s are all rejected. ~8% of Form 4s pass.
  Enriched with buyer name, role(s), shares, price. (History: a naive `"Open Market
  Purchase"` text filter was too tight; then "ingest everything + let Gemini sort it"
  was too loose and entered YUMC grants / a Flutter total-return-swap. Code-P parsing is
  the correct deterministic fix.)
- **13F = quarter diff.** Each 13F is diffed against the fund's prior 13F (via the SEC
  submissions API); we emit only **new** or **+20% increased** positions (top 5 by value,
  >$5M), tagged with the change type. Plain top-holdings-by-value just surfaced stale
  mega-caps (Berkshire's Apple) and was pure prompt bloat.
- **Ticker enrichment.** EDGAR signals resolve the issuer CIK → real current ticker via
  SEC's `company_tickers.json`, stored in `raw_data.ticker`. This feeds price context to
  the scorer and avoids stale-ticker guessing (e.g. ZI→GTM for renamed ZoomInfo).
- `signal_exists` / `filing_has_signals` pre-checks avoid re-fetching filing bodies
  already processed (the 3-day lookback overlaps daily).

---

## Scoring (analyzer/score.py)

- One Gemini 2.5 Flash call ranks the top 5 opportunities. **Signals are deduped by
  company and capped at `MAX_SIGNALS_TO_SCORE=60`** (freshest first) — sending every
  weekly signal (400+) burned credits and fired a price fetch per signal. Insider
  **clusters** (multiple buyers, same ticker) are surfaced to Gemini as stronger conviction.
- Dimensions (0–5 each, `total_score` max 20, a DB generated column):
  - **conviction** — number of independent signals; discretionary buy > plan; 13F is stale.
  - **asymmetry** — penalises **falling knives** hard (cap 2 if down ≥30% & near 52w low).
  - **liquidity** — scored GENEROUSLY (at ~$200/trade almost anything listed is tradeable;
    don't punish small-caps). Real tradeability is gated separately (below).
  - **timing** — don't zero out insider buys for lacking a dated catalyst.
- **Anchoring (critical):** thesis/catalyst/plain_english MUST cite the concrete signal
  ("a director bought N shares on DATE"), not generic macro. Vague large-cap bull cases
  are rejected. (Fixed after YUMC/FLUT entered on "China recovery"-type theses.)
- **Insert-time gates** (using `_get_price_context`, Yahoo chart API):
  - skip if no price (stale/changed ticker)
  - skip if **avg daily $ volume < $1M** (the tradeability floor — replaces market cap,
    which the chart API doesn't expose; ASPS ~$0.2M/day was the cautionary tale)
  - skip **SPACs/units** (flat at ~$10 with near-zero 52w range, or unit/warrant/rights
    ticker suffix U/W/R on a 5+ char symbol like IPVVU)
  - validate Gemini's per-pick `pattern`; attribute only same-pattern `signal_ids`
- Private companies (SpaceX, Anthropic) are prohibited in the prompt.

---

## Paper trading

### Entry filters (paper_trader/entry.py) — all must pass
1. Score ≥ **13** (15 in a bearish regime — index >10% below 52w high)
2. Underlying opportunity within the per-pattern **recency window** (s1/activist 2d,
   insider/smart_money/thematic/etf/pre_ipo 5d, spin_off 7d) — measured from scoring time
3. Under the position cap (`MAX_POSITIONS=10`) and budget not exhausted
4. No duplicate open ticker / not already entered for this opportunity
5. Price fetchable; price hasn't moved >8% since scoring (no chasing)
6. Not within 7 days of earnings
7. **Not a SPAC/unit** (hard block, no override — deterministic at entry, because the
   score-time SPAC filter only stops *new* opportunities; IPVVU slipped in from the pool)
8. **Not a falling knife** (within 10% of 52w low & ≥15% off high, or deep unrecovered
   drawdown) — UNLESS a **multi-insider cluster** (≥2 distinct buyers), which overrides
9. **Sector cap** — ≤3 open positions per SIC major group (via SEC SIC codes), so insider
   buying that clusters by sector (e.g. regional banks) can't take over the book
10. Relative volume ≥1.5× — **news/thematic only**; EDGAR signals are exempt (the filing
    is the signal, not today's tape; liquidity already gated by the $ volume floor)
11. Position sizes to ≥1 share within remaining budget

Entry pulls from the **last 10 days** of opportunities (`get_recent_opportunities`), not
the calendar week — so a Friday pick is still actionable Monday; per-pattern recency gates freshness.

### Position sizing — conviction-scaled within a pool
- **$2,000 AUD pool**, base **$200/trade**, scaled up: score ≥16 → $300, ≥18 → $400,
  capped by remaining budget. Highest-scoring picks are processed first (capital priority).
  `MIN_TRADE_AUD=150`. US ≈ $1 brokerage, ASX $0; 0.5% slippage. FX from Yahoo (USD per AUD, fallback 0.65).

### Exit logic (paper_trader/exit.py), priority order
1. **Trailing stop** — **arms at +20%** (latched — stays armed once hit), then exits if
   price falls **10%** below the running peak. (Was +30%/15%; tightened to bank more of a
   winner and recycle capital faster. Latched activation fixes a bug where the trail
   silently disarmed whenever P&L dipped back under the activation line.) The paper logic
   evaluates this on the daily poll; live trading should use a broker-native trailing-stop
   order — same params, intraday execution.
2. **Stop loss** — **−12%**
3. **Time exit** — per-pattern: insider/smart_money/activist/spin_off **60d**, s1/pre_ipo
   **45d**, thematic/etf **30d**, default 45d. **Exempt if the trailing stop is active**
   (let winners run; don't cut by the calendar). (Replaced a static 28d, which was far
   shorter than the months-long horizon of insider-buy edges.)

### Manual overrides
- `paper_trader/manual_open.py` / `manual_close.py` + workflows (`manual-open.yml`,
  `manual-close.yml`, `workflow_dispatch` with a tickers input). `TICKER:AMOUNT` overrides size.
- Manual closes are `closed_manual` and **excluded** from expectancy / win rate /
  graduation (they're interventions, not strategy outcomes) and don't write a feedback grade.

---

## Notifications & digest

- **Position alerts** (paper_trader/notify.py): emails on every open/close. Fail-safe
  (never breaks trading; no-ops without `DIGEST_EMAIL`). Replaced the old per-filing IPO
  alert spam from the EDGAR watcher.
- **Weekly portfolio digest** (digest/weekly_report.py): open positions with live P&L,
  per-pattern days-to-time-limit, closed-this-week, graduation progress. Portfolio-focused
  — no opportunity listings (trading is automated).

---

## Feedback loop & graduation

- **Automated.** On entry, the feedback row is marked `acted`; on (automated) exit it's
  auto-graded 1–5 from realised P&L. No manual grading.
- **Graduation review** at 20 closed trades with positive expectancy. Slower turnover from
  the 60-day holds means this takes ~months — accepted, to measure the real strategy.

---

## Workflows (.github/workflows/)

| Workflow | Schedule | Purpose |
|---|---|---|
| `daily.yml` | **16:00 UTC** (mid US session), 7 days/week | collect → classify → score → paper trade |
| `weekly-digest.yml` | Sunday 6pm AEST | portfolio digest |
| `edgar-watch.yml` | hourly | early collect+classify (no alerts) |
| `manual-open.yml` / `manual-close.yml` | manual dispatch | open/close named tickers |
| `smoke.yml` | every push/PR | runs `run_smoke.py` |

GitHub Actions scheduling is unreliable (often 1–2h late, sometimes skipped); manual
dispatch is the reliable fallback.

---

## Tech stack & data-source gotchas

- Python 3.12 · Supabase (Postgres) · Gemini 2.5 Flash (`google-genai`) · Resend (sandbox)
  · GitHub Actions · SEC EDGAR + Yahoo Finance unofficial API.
- **Yahoo chart API** (`/v8/finance/chart`) works without auth → use it for price, 52w
  high/low, volume. **`quoteSummary` and `v7/quote` are auth-walled (401/429)** → do NOT
  rely on them. Consequences: market cap isn't available (use **avg $ volume** as the
  tradeability proxy); sector comes from **SEC SIC codes**, not Yahoo.
- The daily cron runs **mid US session** (16:00 UTC) so paper entries/exits fill at a
  live, tradeable price — the paper P&L reflects fills we could actually get, not the
  close. Consequence: today's daily volume bar is partial, so the relative-volume helper
  uses the last fully-closed session (volumes[-2]). (US and ASX market hours don't
  overlap, so ASX fills still have an open-gap — acceptable, ASX is a minority of picks.)
- **Cost is negligible** (~cents/day after the dedup/cap); credits depleting is a
  zero-balance prepay gate, not high spend. A $10 top-up lasts ~a year.

---

## Database schema (Supabase)

- `signals` — raw observations. `source`, `pattern`, `accession_no` (dedup), `raw_data`
  (incl. `entity_name`, `ticker`, and for Form 4: buyer/roles/shares/price; for 13F:
  fund_name/cusip/value_usd/change/pct_change), `summary`, `processed`.
- `opportunities` — scored picks. `vehicle`, `pattern`, four score dims + `total_score`
  (generated), `price_at_score`, `plain_english`, `signal_type_explainer`, `week_of`, `created_at`.
- `feedback` — one per opportunity; `acted`, `grade` (auto), `price_30d/90d`.
- `paper_positions` — `ticker`, `pattern`, `market`, entry/exit prices (USD+AUD), `quantity`,
  `brokerage_aud`, `entry_date`, `score_at_entry`, `status`
  (`open`/`closed_time`/`closed_stop`/`closed_trail`/`closed_manual`), `peak_price_aud`,
  `trailing_stop_active`, `pnl_aud`, `pnl_pct`, `exit_reason`. Migrations in `db/migrations/`.
- `paper_portfolio_snapshots` — daily: open count, deployed, closed, win rate, expectancy.
- `paper_skipped_entries` — why each candidate was skipped (audit trail).

---

## Repo structure

```
collectors/   edgar.py (Form4 code-P, 13F diff, CIK→ticker), etf_launches.py, news.py
analyzer/     classify.py (dispatch by source), score.py (dedup/cap, gates), pnl_tracker.py
paper_trader/ entry.py, exit.py, snapshot.py, notify.py, manual_open.py, manual_close.py
digest/       weekly_report.py (portfolio digest)
db/           client.py, schema.sql, migrations/
run_daily.py · run_weekly.py · run_edgar_watch.py · run_smoke.py
```

---

## Workflow conventions (for Claude)

- **Commit after every change; never push** — the user pushes. End commit messages with the
  Co-Authored-By trailer.
- **Run `.venv/bin/python run_smoke.py` before saying "ready to push"** — catches
  import/crash regressions in the pure helpers. Needs Python **3.12** (the code uses
  `X | None` annotations that fail on 3.9) and the deps installed: a repo-local `.venv`
  (gitignored) built with `uv venv .venv --python 3.12 && uv pip install -p .venv/bin/python
  -r requirements.txt`. The system `python3` is 3.9 and will fail — use the venv.
- Don't read `.env*` / secrets.
- After sharing each daily run, the user expects new picks **chart-checked** (52w position,
  YTD, $ volume) to catch falling knives / SPACs / junk before trusting them.

---

## Known issues / future

- **Status view** — no live "what do I hold" view; reading run logs is the only way.
  Proposed: a tiny `status` workflow (open positions + live P&L + budget), or a Streamlit
  dashboard. Not built yet.
- **Recency uses scoring time, not filing date** — fine for live runs (same day), but a
  backfill re-score would falsely reset freshness; gate on `signal_date` if backfilling.
- **CUSIP→ticker for 13F** — holdings carry CUSIP + name; Gemini resolves the ticker
  (no CUSIP→ticker map), so 13F picks don't get pre-prompt price context.
- **ASX coverage** — only via news RSS (no free structured ASX filings API). ASX tickers
  aren't in SEC data, so they skip the sector cap and SIC-based checks.
- **Score-weight tuning** — once enough closed trades exist, weight patterns by realised P&L.
```
