# Small-Cap Catalyst Sleeve — Phase 2 Spec (paper trading)

**Status:** DECIDED (2026-07-16) — Tim signed off on §9; numbers below are settled
for v1. Phase 1 (read-only screener) is live and shipped. Phase 2 (this) adds
paper trading and is ready to build. Params are grounded in the 07-13 → 07-15
screener series and are expected to be overturned by data, not by argument.

**Prime directive: this sleeve is walled off from the main paper book.** Nothing
here may read, write, or influence main-book state. See §1.

---

## 1. Isolation guarantees (non-negotiable)

The sleeve is a *separate book that happens to share a repo*.

| Concern | Guarantee |
|---|---|
| **DB tables** | New: `smallcap_positions`, `smallcap_snapshots`, `smallcap_skipped`. The sleeve **never** reads or writes `paper_positions`, `paper_portfolio_snapshots`, `paper_skipped_entries`, `opportunities`, `feedback`. |
| **Code** | New modules `smallcap/entry.py`, `smallcap/exit.py`, `smallcap/snapshot.py`. **No imports from `paper_trader/*` or `analyzer/score.py`.** Duplicate a helper rather than import one — coupling is the risk, not a few repeated lines. |
| **Budget** | Own hard pool. Cannot touch or count against the main book's $5,000. |
| **Stats** | Own expectancy / win-rate / graduation counters. Main-book graduation math is unaffected. |
| **Scheduling** | Own workflow (`smallcap-screen.yml`, extended or a sibling). **`daily.yml` is not modified.** |
| **Blast radius** | A total sleeve failure cannot break the main book — different workflow, different process, different tables. |
| **Gemini** | Not used. The sleeve scores on momentum + catalyst, not an LLM opinion. |

---

## 2. Why the rules invert (rationale)

Main book = **base hits**: most positions land near a modest gain; tight risk
control is pure profit. Sleeve = **power law**: most names go nowhere or to zero,
a few 5–20x pay for everything; tight risk control *ejects you from the winners*.

Evidence from our own 3-run series:
- **TTRX** +71% → dropped out of climbing entirely → back to +49%
- **BIVI** +39% → −1%
- **ANNX** +18% → +40%
- **PMVP** +17% → +28% → +32%

A −12% stop and a 10% trail would have shredded most of these mid-swing.

---

## 3. Pool & sizing

```
SMALLCAP_POOL_AUD      = 2000.0  # HARD cap (not soft like the main book)
SMALLCAP_POSITION_AUD  =  100.0  # EQUAL weight — no conviction scaling
SMALLCAP_MAX_POSITIONS =   20
MAX_PER_VERTICAL       =   10
```

**WIDENED 2026-08-26 → 20 × $100, pool $2,000, per-vertical cap 10.** The book sat
full at 10/10 for weeks, so it stopped capturing the ongoing flow of climbing
screen names — starving the research dataset the 4–6wk review needs. Since this is
a PAPER sleeve (no real money), the only cost of more slots is more (noisy) data,
which is the point. Doubled the SLOT COUNT + pool + per-vertical cap; kept the $100
equal weight (so it's 2× the *picks*, not bigger bets). NB this deliberately
accepts more correlation within a vertical (the original concern below) — for a
data-gathering paper book that's an acceptable trade; the per-vertical cap of 10
still prevents an all-one-vertical book.

**(Original rationale, now superseded) Why 10 × $100 and not 20 × $50?** Power-law
strategies want *more* bets, so 20 would be the instinct. But the **5-per-vertical
correlation cap (§6) was the binding constraint** — only two verticals are
productive today (`biotech`, `asx_ann`), so 5+5 = 10 was the practical ceiling.
Widening was flagged as risking the fake-diversification trap the 07-14 broad
cooling exposed — accepted 2026-08-26 in exchange for the data.

- **Hard budget, not soft.** The main book lets high-conviction picks breach its
  pool. The sleeve must not: spec money is strictly bounded. (This also rehearses
  the hard-budget rule flagged for live-money migration.)
- **Equal weight, deliberately.** In a power-law regime you cannot predict which
  name moons, so upsizing on "conviction" is false confidence. This is the direct
  opposite of the main book's `SIZE_TIERS`.
- Position must yield ≥1 share; skip otherwise (many ASX micro-caps are sub-$0.10,
  so this rarely binds).

---

## 4. Entry gates

A candidate must clear **all** of:

1. **On the screen as climbing (▲).** Momentum is the entry gate — the main book
   has no such requirement, the sleeve is built on it.
2. **Has a catalyst** from a supported vertical (`biotech`, `asx_ann`, `defense`).
3. **Market cap** within `$10M–$500M` (DECIDED: tightened from $2B).
   Power-law upside needs a **small base** — a $2B biotech doubling requires $2B
   of buying, which is a main-book-shaped outcome, not a sleeve-shaped one. The
   20x lives at $10–200M. Both our best names fit (AKBA $370M, ERE.AX $10M), and
   the 07-15 run had ~14 climbers under $500M — comfortably enough to fill 10
   slots. **Accepted tradeoff:** this pushes us into the least liquid, most
   promoted end of the market — precisely where paper fills lie most (§7.1). We
   chose the segment where measurement is weakest because it's where the upside
   is. That was deliberate. The $10M floor stays, to skip dead shells.
4. **Catalyst freshness — by vertical** (the two feeds behave differently):
   - `biotech` (scheduled readouts): catalyst date must be **in the future**.
     (DECIDED: keep strict.) The real reason isn't that already-fired names can't
     work — it's that **the pre-catalyst exit (§5.1) needs a future date to
     exist**. An already-fired biotech name has no upcoming event to sell into, so
     it would fall through to trail/time exits — a *different strategy*
     (post-news momentum drift) quietly mixed into the same book, muddying the
     data. One book, one hypothesis. If post-news drift looks interesting later,
     it earns its own tracked cohort. TTRX-type movers are excluded on purpose.
   - `asx_ann` (event-driven, inherently past-dated): announcement within the
     **last 10 days**. Requiring "future" would kill this vertical entirely.
5. **Not already held**; no duplicate ticker.
6. **Per-vertical cap** (see §6).
7. **Free slot + budget** under the hard pool.

Explicitly **NOT** gates (unlike the main book): no liquidity floor, no minimum
Gemini score, no earnings blackout (the catalyst *is* the event we care about).

---

## 5. Exits — the heart of the sleeve

Precedence order: **pre-catalyst → trailing → disaster → time.**

```
PRE_CATALYST_EXIT_DAYS   = 2      # exit N business days BEFORE a dated binary event
TRAIL_ARM_PCT            = 30.0   # DECIDED: arm at +30% (was proposed +50%)
DISASTER_STOP_PCT        = -35.0  # "thesis broken", not risk management
TIME_LIMIT_DAYS          = 45     # for non-dated catalysts only

# DECIDED: ratcheting trail — starts at 20%, TIGHTENS as the gain grows.
# (peak_gain_pct_threshold, trail_pct) — first match wins, highest first.
TRAIL_TIERS = [
    (300.0, 15.0),   # +300%+      -> trail 15%
    (100.0, 17.0),   # +100..299%  -> trail 17%
    (  0.0, 20.0),   # +30..99%    -> trail 20% (baseline once armed)
]
```

### 5.1 Pre-catalyst exit — the single most important rule
For any position whose catalyst is a **dated binary event** (a trial readout),
**exit the full position 2 business days before that date, regardless of P&L.**

This is the edge. We are buying the *anticipation run-up* and selling into it. We
**never hold through a readout** — that's a coin flip with a fat tail, and a gap
through any stop. The main book has no analogue (it *blackouts* around earnings;
the sleeve *exits before* the event).

Applies to `biotech` only. `asx_ann` catalysts have already fired — nothing to
exit ahead of.

### 5.2 Trailing stop — wider than the main book, and it tightens as it climbs
Arm at **+30%**, then trail per `TRAIL_TIERS` below the running peak, latched
(same latch logic as the main book — arm off the *stored peak*, never disarm on a
dip).

Two distinct mechanics, easily confused:
1. **The floor rises with the peak** — automatic, standard, and already how the
   main book behaves (GLBE ratcheted $49.55 → $50.08 over 07-13→07-15).
2. **The trail % itself tightens** as the gain grows (`TRAIL_TIERS`) — this is the
   sleeve-specific addition. 20% baseline → 17% past +100% → 15% past +300%.

Rationale: the main book's +20%/10% is calibrated for names that move a few % a
day. These move 20%+ in a session; a 10% trail would fire on noise constantly.

**Why the tightening is deliberately GENTLE (never below 15%):** in a power-law
book **the tail is the entire return**. Choking a +500% name with a 12% trail on a
stock that swings 20% in a session cuts off the one position that pays for the
other nine. Protecting gains and capturing the tail are in direct tension here,
and the tail wins. **This tier table is the #1 thing to tune once we have
give-back data.**

### 5.3 Disaster stop
**−35%**, not −12%. This is a "the thesis is broken" brake, not a risk tool. Our
own data shows these names routinely draw down 30%+ and recover; anything tighter
guarantees we sell the bottom of a swing.

### 5.4 Time exit
**45 days**, for `asx_ann` positions with no scheduled event. If an event-driven
name hasn't moved in ~6 weeks, the catalyst is spent.

---

## 6. Correlation caps — fake diversification is the real risk

```
MAX_PER_VERTICAL = 10     # of 20 positions (widened 2026-08-26; was 5 of 10)
```

**Evidence:** on **07-14** nearly the entire biotech list cooled together. Ten
small-cap biotech positions is **not ten bets — it's one leveraged bet on
small-cap biotech sentiment.**

US biotech and ASX micro-cap miners are genuinely uncorrelated (different market,
different drivers), so a 5/5 split across the two live verticals gives real
diversification while still filling the book. `defense` currently yields ~0
matches, so it does not meaningfully compete for slots yet.

---

## 7. What we're honest about up front

1. **Paper fills will flatter us, badly.** This matters far more here than in the
   main book. A **$10M ASX micro-cap** (ERE.AX at +67%) will not fill at the
   screen price, and will *definitely* not let you out when it gaps down. **The
   sleeve's paper P&L will overstate the most exciting names.** Treat the result
   as directional, never as achievable.
2. **The sample will be too small to prove an edge.** 10 tiny bets over a few
   weeks is a mechanics rehearsal, not statistical evidence. Power-law strategies
   need many bets over a long time to show their shape.
3. **The trail parameters (§5.2) are educated guesses.** They are the first thing
   the data should overturn.
4. **Survivorship bias in the premise.** The 2000%-winner anecdote that motivated
   this is memorable precisely because it worked. The zeros aren't memorable.
   That's exactly what a measured paper sleeve is for.

---

## 8. Success criteria (how we judge it)

Not "did it make money" — the sample is too small. Instead, after ~4–6 weeks:
- **Did the pre-catalyst exit rule fire cleanly**, and did those names actually
  run up into their dates?
- **Did any name arm the +50% trail?** If none did, the arm is too high.
- **What was peak-vs-exit give-back?** (Same measurement as the main book's
  hypothesis (d) — reuse the approach, separate data.)
- **Did the −35% stop ever fire on a name that later recovered?** If yes, still
  too tight.
- **Did the vertical cap actually bind**, and did the two verticals move
  independently?

---

## 9. Decisions — RESOLVED 2026-07-16

| # | Question | Decision |
|---|---|---|
| 1 | Pool size | **$2,000** → 20 × $100 equal weight (widened 2026-08-26 from $1,000/10 for data capture; per-vertical cap raised 5→10). |
| 2 | Trail | **Arm +30%, trail 20%, ratcheting tighter** (17% past +100%, 15% past +300%). Gentle on purpose — the tail is the whole return (§5.2). |
| 3 | Biotech future-dated only | **Yes, strict.** The pre-catalyst exit needs a future date to exist; already-fired names would silently be a different strategy (§4.4). |
| 4 | Cap band | **$10M–$500M.** Power-law upside needs a small base. Accepted tradeoff: least liquid, most promoted end (§4.3, §7.1). |

### Still open (deferred, not blocking the build)
- Whether the **+30% arm ever fires at all.** If nothing arms across 4–6 weeks,
  the arm is too high and should drop to ~+20%. This is a success criterion (§8),
  not a pre-build decision.
- Whether `defense` ever earns a slot (currently ~0 matches).
- Whether a **post-news drift** cohort (the excluded TTRX-types) deserves its own
  separate tracked book later.

---

## 10. Build order (once decisions land)

1. `smallcap/entry.py` — gates + equal-weight sizing + vertical cap
2. `smallcap/exit.py` — pre-catalyst → trail → disaster → time
3. `smallcap/snapshot.py` — own stats
4. `run_smallcap_trade.py` + workflow wiring (separate from `daily.yml`)
5. DB tables `smallcap_*`
6. Smoke coverage for the pure helpers (mirroring `run_smoke.py` style)
