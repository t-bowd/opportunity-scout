"""
Polls SEC EDGAR for new filings across 5 form types.
Respects the SEC's rate limit: max 10 requests/second, user-agent required.
"""
import os
import time
import requests
from datetime import date, timedelta
from db.client import insert_signal

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

    return {
        "source": f"edgar_{form_type.lower().replace(' ', '_').replace('-', '_')}",
        "accession_no": accession,
        "pattern": pattern,
        "signal_date": src.get("file_date") or date.today().isoformat(),
        "url": url,
        "raw_data": raw_data,
    }


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
                signal = _build_signal(hit, form_type, pattern)
                result = insert_signal(signal)
                if result:
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
