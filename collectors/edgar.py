"""
Polls SEC EDGAR for new filings across 5 form types.
Respects the SEC's rate limit: max 10 requests/second, user-agent required.
"""
import os
import re
import time
import requests
from datetime import date, timedelta
from db.client import insert_signal, signal_exists, filing_has_signals

ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# 13F holdings: how many of the largest positions to emit per filing, and the
# minimum position value (USD) worth bothering with. Keeps us from inserting a
# fund's entire long tail of tiny positions.
THIRTEEN_F_TOP_N = 5
THIRTEEN_F_MIN_VALUE_USD = 5_000_000
THIRTEEN_F_INCREASE_THRESHOLD = 0.20  # +20% shares vs prior quarter counts as "increased"
# Only treat a 13F filer as "smart money" if it manages real size. The 13F net
# catches any filer >$100M AUM, including small RIAs (e.g. Positano Wealth Mgmt
# nudging a position 14846% off a tiny base) — not the institutional-conviction
# signal we want. Floor is the fund's total reported 13F value (its long-equity AUM).
THIRTEEN_F_MIN_FUND_AUM = 1_000_000_000

EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"
EDGAR_BASE = "https://www.sec.gov"
HEADERS = {
    "User-Agent": os.environ.get(
        "EDGAR_USER_AGENT", "OpportunityScout contact@example.com"
    ),
    "Accept": "application/json",
}

FORM_PATTERNS = {
    "S-1":    "s1_filed",       # IPO registration
    "N-1A":   "etf_launch",     # New ETF registration
    "SC 13D": "activist",       # Activist 13D
    "4":      "insider_buy",    # Form 4 insider transactions
    "13F-HR": "smart_money",    # Quarterly institutional holdings
}

# SEC's official CIK->ticker map, loaded once and cached. Lets us attach the real
# tradeable ticker to EDGAR signals (Form 4 issuer, S-1/13D/N-1A filer) so price
# and volume context reaches the scorer instead of relying on Gemini to resolve a
# company name (which produced stale tickers like ZI for a renamed ZoomInfo).
_CIK_TICKER: dict[int, str] | None = None
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def _cik_to_ticker(cik) -> str | None:
    """Resolve a CIK to its current ticker via SEC's company_tickers.json."""
    global _CIK_TICKER
    if _CIK_TICKER is None:
        _CIK_TICKER = {}
        try:
            data = requests.get(COMPANY_TICKERS_URL, headers=HEADERS, timeout=20).json()
            _CIK_TICKER = {int(row["cik_str"]): row["ticker"] for row in data.values()}
        except Exception as e:
            print(f"[edgar] CIK->ticker map load failed: {e}")
            _CIK_TICKER = {}
    try:
        return _CIK_TICKER.get(int(cik))
    except (TypeError, ValueError):
        return None


_TICKER_CIK: dict[str, int] | None = None
_SIC_CACHE: dict[str, str | None] = {}


def get_sector_key(ticker: str) -> str | None:
    """
    Coarse sector key — the SIC major group (first 2 digits of the SIC code) for a
    ticker, via SEC submissions data. Used to cap how many open positions we hold
    in one sector. Returns None for tickers the SEC doesn't cover (e.g. ASX), so
    those simply aren't sector-capped. Cached per process.
    """
    global _TICKER_CIK
    if ticker in _SIC_CACHE:
        return _SIC_CACHE[ticker]
    if _TICKER_CIK is None:
        _cik_to_ticker(0)  # ensure the CIK->ticker map is loaded
        _TICKER_CIK = {v.upper(): k for k, v in (_CIK_TICKER or {}).items()}
    sic2 = None
    cik = _TICKER_CIK.get(ticker.upper())
    if cik:
        try:
            url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
            sic = requests.get(url, headers=HEADERS, timeout=15).json().get("sic")
            if sic:
                sic2 = str(sic)[:2]
        except Exception:
            pass
    _SIC_CACHE[ticker] = sic2
    return sic2


def _edgar_search(form_type: str, start_date: str, end_date: str) -> list[dict]:
    params = {
        "forms": form_type,
        "dateRange": "custom",
        "startdt": start_date,
        "enddt": end_date,
        "from": 0,
        "size": 40,
    }
    # No text filter on Form 4 — "Open Market Purchase" as a full-text query was
    # too restrictive because many Form 4 filings use only the transaction code "P"
    # with no human-readable description, causing the collector to find almost nothing.
    # Filtering for discretionary buys is handled at the scoring stage: the rule-based
    # summary explicitly states "transaction code P" and the scoring prompt instructs
    # Gemini to score discretionary buys higher than ESPP/plan purchases.

    resp = requests.get(EDGAR_SEARCH, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    hits = resp.json().get("hits", {}).get("hits", [])
    return hits


def _extract_entity_name(src: dict, form_type: str) -> str:
    """
    EDGAR returns display_names as a list like:
      Form 4:  ["Insider Name (CIK 000...)", "Company Name (CIK 000...)"]
      S-1:     ["Company Name (CIK 000...)"]
      13F/13D: ["Fund/Filer Name (CIK 000...)"]

    For Form 4 the issuer (company being traded) is the last entry.
    For all others the filer is the first (and usually only) entry.
    Strip the trailing CIK portion.
    """
    display_names = src.get("display_names", [])
    if not display_names:
        return "Unknown"

    if form_type == "4" and len(display_names) >= 2:
        raw = display_names[-1]   # issuer/company
    else:
        raw = display_names[0]    # filer

    # Strip " (CIK 0001234567)" suffix
    return raw.split("(CIK")[0].strip()


def _build_signal(hit: dict, form_type: str, pattern: str) -> dict:
    src = hit.get("_source", {})
    accession = hit.get("_id", "").replace(":", "-")
    entity_name = _extract_entity_name(src, form_type)
    adsh = src.get("adsh", "")
    url = f"{EDGAR_BASE}/cgi-bin/browse-edgar?action=getcompany&filenum={adsh}&type={form_type}"

    # Store cleaned entity name alongside raw data for easy access downstream
    raw_data = dict(src)
    raw_data["entity_name"] = entity_name

    # Resolve the real ticker from the issuer/filer CIK. For Form 4 the issuer
    # (the company whose stock was traded) is the last CIK; for everything else
    # the filer is the first. Insiders' personal CIKs resolve to None, as expected.
    ciks = src.get("ciks", [])
    if ciks:
        target_cik = ciks[-1] if (form_type == "4" and len(ciks) >= 2) else ciks[0]
        ticker = _cik_to_ticker(target_cik)
        if ticker:
            raw_data["ticker"] = ticker

    return {
        "source": f"edgar_{form_type.lower().replace(' ', '_').replace('-', '_')}",
        "accession_no": accession,
        "pattern": pattern,
        "signal_date": src.get("file_date") or date.today().isoformat(),
        "url": url,
        "raw_data": raw_data,
    }


def _fetch_form4_purchase(hit: dict) -> dict | None:
    """
    Fetch a Form 4 filing and return open-market purchase detail, or None.

    Only returns a result if the filing contains a non-derivative transaction
    with code P (discretionary open-market purchase). Grants (code A), sales (S),
    option exercises (M), gifts (G), tax withholding (F) etc. all return None —
    they are not the bullish insider-buy signal we want. This is the deterministic
    replacement for the old "Open Market Purchase" full-text filter, which both
    missed real buys and let grants/sales through (YUMC director grants slipped in
    as a 'pick'; FLUT mixed buys and sells).
    """
    src = hit.get("_source", {})
    accession = hit.get("_id", "").split(":")[0]
    ciks = src.get("ciks", [])
    if not accession or not ciks:
        return None

    accnd = accession.replace("-", "")
    txt = None
    for c in ciks:
        url = f"{EDGAR_BASE}/Archives/edgar/data/{int(c)}/{accnd}/{accession}.txt"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200 and "transactionCode" in r.text:
                txt = r.text
                break
        except Exception:
            continue
    if not txt:
        return None

    # Sum shares across non-derivative code-P transactions only
    total_shares = 0.0
    price = None
    for block in re.findall(
        r"<nonDerivativeTransaction>.*?</nonDerivativeTransaction>", txt, re.DOTALL
    ):
        code = re.search(r"<transactionCode>(\w)</transactionCode>", block)
        if not code or code.group(1) != "P":
            continue
        sh = re.search(r"<transactionShares>\s*<value>([\d.]+)", block)
        pr = re.search(r"<transactionPricePerShare>\s*<value>([\d.]+)", block)
        if sh:
            total_shares += float(sh.group(1))
        if pr and price is None:
            price = float(pr.group(1))

    if total_shares <= 0:
        return None  # no open-market purchase in this filing

    owner = re.search(r"<rptOwnerName>(.*?)</rptOwnerName>", txt, re.DOTALL)
    is_dir = re.search(r"<isDirector>\s*(1|true)\s*</isDirector>", txt, re.I)
    is_off = re.search(r"<isOfficer>\s*(1|true)\s*</isOfficer>", txt, re.I)
    is_ten = re.search(r"<isTenPercentOwner>\s*(1|true)\s*</isTenPercentOwner>", txt, re.I)
    title = re.search(r"<officerTitle>(.*?)</officerTitle>", txt, re.DOTALL)
    roles = []
    if is_dir:
        roles.append("director")
    if is_off:
        roles.append(title.group(1).strip() if title and title.group(1).strip() else "officer")
    if is_ten:
        roles.append("10% owner")

    return {
        "buyer": owner.group(1).strip() if owner else "Insider",
        "roles": roles or ["insider"],
        "shares": int(total_shares),
        "price": round(price, 2) if price else None,
        "value_usd": int(total_shares * price) if price else None,
    }


def _build_form4_signal(hit: dict) -> list[dict]:
    """One signal per Form 4 — but only if it's an open-market purchase (code P)."""
    base = _build_signal(hit, "4", "insider_buy")
    # Skip the (expensive) XML fetch for filings we've already stored
    if signal_exists(base["accession_no"]):
        return []
    purchase = _fetch_form4_purchase(hit)
    if not purchase:
        return []  # grant / sale / exercise — not a bullish buy signal
    base["raw_data"].update(purchase)
    return [base]


def _parse_info_table(xml: str) -> list[dict]:
    """
    Extract holdings from a 13F information table XML.
    Namespace-agnostic (matches <nameOfIssuer> or <ns1:nameOfIssuer>).
    Returns a list of {issuer, cusip, value_usd, shares}.
    """
    holdings = []
    for block in re.findall(r"<(?:\w+:)?infoTable[ >].*?</(?:\w+:)?infoTable>", xml, re.DOTALL):
        def _tag(name: str) -> str | None:
            m = re.search(rf"<(?:\w+:)?{name}>(.*?)</(?:\w+:)?{name}>", block, re.DOTALL)
            return m.group(1).strip() if m else None

        issuer = _tag("nameOfIssuer")
        cusip = _tag("cusip")
        value_raw = _tag("value")
        shares_raw = _tag("sshPrnamt")
        if not issuer or not value_raw:
            continue
        try:
            value_usd = int(re.sub(r"[^\d]", "", value_raw))
        except ValueError:
            continue
        try:
            shares = int(re.sub(r"[^\d]", "", shares_raw)) if shares_raw else 0
        except ValueError:
            shares = 0
        holdings.append({
            "issuer": issuer,
            "cusip": cusip,
            "value_usd": value_usd,
            "shares": shares,
        })
    return holdings


def _fetch_13f_all_holdings(cik: str, accession: str) -> list[dict]:
    """
    Fetch and parse the FULL information table for a single 13F filing —
    every holding, unfiltered and unsorted. Fails soft (returns []).
    """
    cik_int = str(int(cik))  # strip zero-padding for the archive path
    acc_nodash = accession.replace("-", "")
    index_url = f"{ARCHIVES_BASE}/{cik_int}/{acc_nodash}/index.json"

    try:
        idx = requests.get(index_url, headers=HEADERS, timeout=15).json()
        items = idx.get("directory", {}).get("item", [])
        xml_names = [it["name"] for it in items if it.get("name", "").lower().endswith(".xml")]

        for name in xml_names:
            # primary_doc.xml is the cover page, not the holdings table — skip it
            if name.lower() == "primary_doc.xml":
                continue
            url = f"{ARCHIVES_BASE}/{cik_int}/{acc_nodash}/{name}"
            xml = requests.get(url, headers=HEADERS, timeout=15).text
            if "infoTable" in xml:
                holdings = _parse_info_table(xml)
                if holdings:
                    return holdings
            time.sleep(0.2)
    except Exception as e:
        print(f"[edgar] 13F holdings fetch failed for {accession}: {e}")
    return []


def _fetch_prior_13f_shares(fund_cik: str, current_accession: str) -> dict[str, int] | None:
    """
    Return {cusip: shares} from the fund's PREVIOUS 13F-HR filing, used to diff
    against the current one. Returns None if there is no prior filing or it can't
    be fetched — caller then falls back to top-holdings-by-value.
    """
    try:
        url = f"https://data.sec.gov/submissions/CIK{int(fund_cik):010d}.json"
        data = requests.get(url, headers=HEADERS, timeout=15).json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accs = recent.get("accessionNumber", [])
        # recent arrays are newest-first; the first 13F-HR that isn't the current
        # filing is the prior quarter's.
        prior_acc = next(
            (a for f, a in zip(forms, accs)
             if f in ("13F-HR", "13F-HR/A") and a != current_accession),
            None,
        )
        if not prior_acc:
            return None
        prior = _fetch_13f_all_holdings(fund_cik, prior_acc)
        if not prior:
            return None
        shares_by_cusip: dict[str, int] = {}
        for h in prior:
            if h.get("cusip"):
                shares_by_cusip[h["cusip"]] = shares_by_cusip.get(h["cusip"], 0) + h["shares"]
        return shares_by_cusip
    except Exception as e:
        print(f"[edgar] prior 13F fetch failed for CIK {fund_cik}: {e}")
        return None


def _build_13f_holding_signals(hit: dict) -> list[dict]:
    """
    Turn one 13F filing into signals — but only for positions the fund NEWLY
    INITIATED or materially INCREASED versus its prior quarter. A fund's long-held
    mega-cap stakes (Berkshire's Apple, etc.) are not actionable; a fresh or
    growing position is the actual smart-money signal. Falls back to top holdings
    by value only when there's no prior filing to diff against.
    """
    src = hit.get("_source", {})
    accession = hit.get("_id", "").split(":")[0]
    ciks = src.get("ciks", [])
    if not ciks or not accession:
        return []

    base_acc = accession.replace(":", "-")
    # Skip the (expensive) holdings fetch + diff for filings already processed
    if filing_has_signals(base_acc):
        return []

    fund_cik = ciks[0]
    fund_name = _extract_entity_name(src, "13F-HR")
    file_date = src.get("file_date") or date.today().isoformat()

    current = _fetch_13f_all_holdings(fund_cik, accession)
    if not current:
        return []

    # Fund-size floor — skip small RIAs whose moves aren't an institutional signal.
    fund_aum = sum(h["value_usd"] for h in current)
    if fund_aum < THIRTEEN_F_MIN_FUND_AUM:
        return []

    prior = _fetch_prior_13f_shares(fund_cik, accession)

    interesting: list[dict] = []
    for h in current:
        if h["value_usd"] < THIRTEEN_F_MIN_VALUE_USD:
            continue
        cusip = h.get("cusip")
        if prior is None:
            change, prior_shares, pct = "held", None, None  # no baseline — keep top by value
        else:
            prior_shares = prior.get(cusip, 0)
            if prior_shares == 0:
                change, pct = "new", None
            elif h["shares"] > prior_shares * (1 + THIRTEEN_F_INCREASE_THRESHOLD):
                change, pct = "increased", round((h["shares"] / prior_shares - 1) * 100)
            else:
                continue  # unchanged or trimmed — not a signal
        interesting.append({**h, "change": change, "prior_shares": prior_shares, "pct_change": pct})

    interesting.sort(key=lambda h: h["value_usd"], reverse=True)

    signals = []
    for h in interesting[:THIRTEEN_F_TOP_N]:
        # Unique accession per holding so insert_signal's dedup doesn't collapse
        # all holdings from one filing into a single row.
        holding_key = h.get("cusip") or h["issuer"][:12]
        signals.append({
            "source": "edgar_13f_hr",
            "accession_no": f"{base_acc}-{holding_key}",
            "pattern": "smart_money",
            "signal_date": file_date,
            "url": f"{ARCHIVES_BASE}/{str(int(fund_cik))}/{accession.replace('-', '')}/",
            "raw_data": {
                "entity_name": h["issuer"],     # the held company — what gets scored
                "fund_name": fund_name,          # who holds it
                "cusip": h.get("cusip"),
                "value_usd": h["value_usd"],
                "shares": h["shares"],
                "change": h["change"],           # new | increased | held
                "prior_shares": h.get("prior_shares"),
                "pct_change": h.get("pct_change"),
            },
        })
    return signals


def collect(lookback_days: int = 3) -> int:
    end = date.today()
    start = end - timedelta(days=lookback_days)
    start_str = start.isoformat()
    end_str = end.isoformat()

    inserted = 0
    for form_type, pattern in FORM_PATTERNS.items():
        try:
            hits = _edgar_search(form_type, start_str, end_str)
            form_inserted = 0
            for hit in hits:
                # 13F: expand into one signal per top holding (the fund name alone
                # is not tradeable). Form 4: only keep open-market purchases (code P).
                # All other forms: one signal per filing.
                if form_type == "13F-HR":
                    signals = _build_13f_holding_signals(hit)
                    time.sleep(0.3)  # extra calls per 13F — stay under rate limit
                elif form_type == "4":
                    signals = _build_form4_signal(hit)
                    time.sleep(0.15)  # XML fetch per new Form 4 — stay under rate limit
                else:
                    signals = [_build_signal(hit, form_type, pattern)]
                for signal in signals:
                    if insert_signal(signal):
                        inserted += 1
                        form_inserted += 1
            print(f"[edgar] {form_type} ({pattern}): {form_inserted} new / {len(hits)} found")
            time.sleep(0.5)  # stay well under SEC rate limit
        except Exception as e:
            print(f"[edgar] {form_type} error: {e}")

    print(f"[edgar] inserted {inserted} new signals total")
    return inserted


if __name__ == "__main__":
    collect(lookback_days=1)
