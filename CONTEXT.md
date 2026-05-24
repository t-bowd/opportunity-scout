# Opportunity Scout — Project Context

Use this file to brief Claude on what has been built so far.

---

## What this is

A fully automated stock market opportunity-scouting agent that runs on GitHub Actions at $0/month. It collects signals from SEC EDGAR and financial news RSS feeds daily, classifies them, scores the top opportunities using Gemini AI, and emails a weekly digest every Sunday evening.

---

## What it looks for

Five signal patterns are active:

| Pattern | Source | What it means |
|---|---|---|
| `insider_buy` | SEC Form 4 | Company executives buying shares on open market with own money |
| `smart_money` | SEC 13F-HR | Institutional fund quarterly holdings disclosures |
| `s1_filed` | SEC S-1 | Company filing to go public (IPO) |
| `activist` | SEC 13D | Activist investor taking a large stake |
| `thematic_etf` / `etf_launch` | SEC N-1A + ETF.com RSS | New ETF launched around a theme |
| `news` (various) | RSS feeds | Australian and US financial news classified by Gemini |

---

## How it runs

Three GitHub Actions workflows fire automatically:

| Workflow | Schedule (AEST) | What it does |
|---|---|---|
| `daily.yml` | 6am Mon–Fri | Collects EDGAR filings + news, classifies, scores |
| `weekly-digest.yml` | Sunday 6pm | Sends email digest, creates feedback rows |
| `edgar-watch.yml` | Every hour Mon–Fri | Checks for urgent filings (S-1, activist 13D), sends immediate alert if found |

---

## Tech stack

- **Language**: Python 3.12
- **Database**: Supabase (PostgreSQL) — free tier
- **AI**: Google Gemini 2.5 Flash via `google-genai` SDK — free tier (billing attached for quota)
- **Email**: Resend — free tier sandbox (sends to verified email only)
- **CI/Hosting**: GitHub Actions — free tier (public repo, unlimited minutes)
- **Data sources**: SEC EDGAR (free API), RSS feeds (free)

---

## Data flow

```
[GitHub Actions cron]
        ↓
[Collectors: edgar.py, etf_launches.py, news.py]
        ↓ inserts rows
[signals table — Supabase]
        ↓
[classifier: classify.py]
  - EDGAR signals: rule-based (no LLM), uses display_names field for company name
  - News signals: Gemini classifies pattern + entity
        ↓ marks processed=true, writes summary
[signals table updated]
        ↓
[scorer: score.py]
  - Fetches week's processed signals
  - Sends to Gemini: "here are this week's signals, score top 5"
  - Gemini resolves real tickers (NYSE/NASDAQ or ASX .AX format)
        ↓ inserts rows
[opportunities table]
        ↓
[weekly_report.py — Sunday]
  - Pulls top 5 by total_score for the week
  - Sends HTML email via Resend
  - Creates feedback rows in Supabase
```

---

## Database schema

Four tables in Supabase:

### `entities`
Stores known companies/ETFs. Currently not heavily used — entity linking is a future improvement.

### `signals`
Raw observations from collectors. Key fields:
- `source` — `edgar_4`, `edgar_s1`, `edgar_13f_hr`, `edgar_13d`, `edgar_n1a`, `etf_launch`, `news`
- `pattern` — set by collector for EDGAR, set by Gemini for news
- `accession_no` — EDGAR accession number, used for deduplication
- `raw_data` — full EDGAR `_source` JSON, includes `entity_name` (extracted from `display_names`)
- `summary` — plain text summary (rule-based for EDGAR, Gemini-generated for news)
- `processed` — boolean, true once classified

### `opportunities`
Scored opportunities. Key fields:
- `vehicle` — the ticker to trade
- `thesis`, `catalyst`, `invalidation` — Gemini-generated
- `conviction`, `asymmetry`, `liquidity`, `timing` — 0–5 each
- `total_score` — generated column, sum of four dimensions (max 20)
- `week_of` — Monday of the ISO week, used to group by digest

### `feedback`
One row per opportunity. Filled in manually by user after reading digest.
- `acted` — boolean, did you trade it
- `grade` — 1–5, was it a good call
- `price_30d`, `price_90d` — auto-populated by `pnl_tracker.py`
- `pnl_30d_pct`, `pnl_90d_pct` — generated columns

---

## Repository structure

```
opportunity-scout/
├── collectors/
│   ├── edgar.py          # SEC EDGAR Form 4, S-1, 13F, 13D, N-1A
│   ├── etf_launches.py   # ETF.com RSS for new ETF launches
│   └── news.py           # US + Australian RSS feeds
├── analyzer/
│   ├── classify.py       # Rule-based (EDGAR) + Gemini (news) classification
│   ├── score.py          # Gemini scoring → opportunities table
│   └── pnl_tracker.py    # Auto-fills price_30d / price_90d on feedback rows
├── digest/
│   └── weekly_report.py  # HTML email via Resend
├── db/
│   ├── client.py         # Supabase client + helper functions
│   └── schema.sql        # Run once in Supabase SQL editor to init tables
├── .github/workflows/
│   ├── daily.yml         # 6am AEST Mon–Fri
│   ├── weekly-digest.yml # Sunday 6pm AEST
│   └── edgar-watch.yml   # Hourly Mon–Fri
├── run_daily.py          # Entry point for daily workflow
├── run_weekly.py         # Entry point for weekly digest workflow
├── run_edgar_watch.py    # Entry point for edgar-watch workflow
├── grade.py              # CLI tool for grading opportunities
└── requirements.txt
```

---

## Environment variables / GitHub Secrets

| Secret | Description |
|---|---|
| `SUPABASE_URL` | `https://fkuxtlgnrpyllzjrljak.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Supabase service_role key (rotated after accidental exposure) |
| `SUPABASE_PROJECT_REF` | `fkuxtlgnrpyllzjrljak` |
| `GEMINI_API_KEY` | Google AI Studio key — billing attached, using gemini-2.5-flash |
| `RESEND_API_KEY` | Resend sandbox key — sends to verified email only |
| `DIGEST_EMAIL` | `tim@timbowman.com.au` |
| `EDGAR_USER_AGENT` | `OpportunityScout tim@timbowman.com.au` (SEC requirement) |

---

## News feeds active

**US:**
- Reuters Business
- MarketWatch Top Stories
- Seeking Alpha
- Yahoo Finance
- SEC press releases (EDGAR atom feed)
- Investing.com

**Australian:**
- ABC Business (`abc.net.au/news/feed/51120/rss.xml`)
- Sydney Morning Herald Business (`smh.com.au/rss/business.xml`)

---

## Feedback loop

Every Sunday after reading the digest:
1. Open Supabase → Table Editor → `feedback` table
2. Five rows are pre-populated (one per opportunity)
3. Fill in `acted` (true/false) and `grade` (1–5)
4. `price_30d` and `price_90d` are auto-filled by the weekly cron

To see accuracy by pattern after a few weeks:
```bash
python grade.py --report
```

---

## Known issues / future improvements

- **Entity linking**: `entities` table not yet populated — opportunities don't link back to a persistent entity record
- **ASX structured filings**: ASX retired their free public API. Structured Appendix 3Y / Form 603 data requires a paid ASX data subscription. Currently covered via news RSS only.
- **Options flow**: Not yet integrated. Unusual Whales has a free tier that could add unusual options activity as a signal source.
- **Earnings calendar**: Scorer doesn't know earnings dates. Adding this would improve timing scores.
- **Web UI for grading**: Currently done in Supabase table editor. A simple web UI would reduce friction.
- **Score weight tuning**: After 8+ weeks of feedback data, score weights should be tuned based on which patterns actually predict returns (`python grade.py --report`).
