"""
Catalyst feeds — free, structured, dated event sources, one function per vertical.

Each returns a list of raw catalyst records (with a sponsor/recipient/code name to
be matched against the ticker universe, plus a headline + date). Matching to
tickers happens in screen.py (universe-first).

  biotech_catalysts   -> clinicaltrials.gov v2 (US + AU, Phase 2/3, industry)
  defense_catalysts   -> USAspending.gov federal contract awards
  asx_announcements   -> markitdigital per-company ASX announcements (price-sensitive)

Proven reachable/auth-free 2026-07-13 (see memory `smallcap-catalyst-sleeve`).
"""

from datetime import date, timedelta
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (OpportunityScout smallcap screener)"}

CT_GOV = "https://clinicaltrials.gov/api/v2/studies"
USASPENDING = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
ASX_ANN = "https://asx.api.markitdigital.com/asx-research/1.0/companies/{code}/announcements"


def biotech_catalysts(window_days: int = 120, back_days: int = 30) -> list[dict]:
    """
    Industry-sponsored Phase 2/3 trials with a primary completion date in
    [today - back_days, today + window_days] — i.e. an imminent or just-passed
    readout. Pulls both US and Australia locations. Returns sponsor + date + phase.
    """
    start = (date.today() - timedelta(days=back_days)).isoformat()
    end = (date.today() + timedelta(days=window_days)).isoformat()
    out: list[dict] = []
    for country in ("United States", "Australia"):
        adv = (f"AREA[LeadSponsorClass]INDUSTRY AND (AREA[Phase]PHASE2 OR AREA[Phase]PHASE3) "
               f"AND AREA[LocationCountry]{country} "
               f"AND AREA[PrimaryCompletionDate]RANGE[{start},{end}]")
        params = {
            "filter.advanced": adv,
            "filter.overallStatus": "RECRUITING|ACTIVE_NOT_RECRUITING|COMPLETED",
            "fields": ",".join([
                "protocolSection.identificationModule.nctId",
                "protocolSection.sponsorCollaboratorsModule.leadSponsor.name",
                "protocolSection.designModule.phases",
                "protocolSection.statusModule.primaryCompletionDateStruct.date",
                "protocolSection.conditionsModule.conditions",
            ]),
            "pageSize": "200",
        }
        try:
            resp = requests.get(CT_GOV, headers=HEADERS, params=params, timeout=40)
            studies = resp.json().get("studies", [])
        except Exception as e:  # noqa: BLE001
            print(f"[smallcap/catalysts] biotech ({country}) fetch failed: {e}")
            continue
        for s in studies:
            p = s.get("protocolSection", {})
            out.append({
                "vertical": "biotech",
                "match_name": p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", ""),
                "headline": ", ".join(p.get("conditionsModule", {}).get("conditions", [])[:2]),
                "phase": "/".join(p.get("designModule", {}).get("phases", [])),
                "date": p.get("statusModule", {}).get("primaryCompletionDateStruct", {}).get("date", ""),
                "ref": p.get("identificationModule", {}).get("nctId", ""),
            })
    return out


def defense_catalysts(back_days: int = 30, min_amount: float = 5e6) -> list[dict]:
    """
    Recent federal contract awards >= min_amount. Recipient name is matched against
    the US universe (universe-first) — most recipients are mega-caps/labs and will
    not match a small-cap, which is the point of the filter.
    """
    start = (date.today() - timedelta(days=back_days)).isoformat()
    body = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],
            "time_period": [{"start_date": start, "end_date": date.today().isoformat()}],
            "award_amounts": [{"lower_bound": min_amount}],
        },
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency", "Start Date"],
        "sort": "Award Amount", "order": "desc", "limit": 100,
    }
    try:
        resp = requests.post(USASPENDING, json=body, timeout=40)
        results = resp.json().get("results", [])
    except Exception as e:  # noqa: BLE001
        print(f"[smallcap/catalysts] defense fetch failed: {e}")
        return []
    out = []
    for r in results:
        out.append({
            "vertical": "defense",
            "match_name": r.get("Recipient Name", ""),
            "headline": f"${_short(r.get('Award Amount'))} — {r.get('Awarding Agency', '')}",
            "phase": "",
            "date": r.get("Start Date", ""),
            "ref": r.get("Award ID", ""),
        })
    return out


def asx_announcements(code: str, only_price_sensitive: bool = True,
                      back_days: int = 21) -> list[dict]:
    """
    Recent announcements for one ASX code (pass the bare code, no .AX). Keeps
    price-sensitive ones (the ASX's own material-catalyst flag) within back_days.
    This is the O(N) feed — callers should scan a bounded set of codes.
    """
    cutoff = (date.today() - timedelta(days=back_days)).isoformat()
    url = ASX_ANN.format(code=code.lower())
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15,
                            params={"fields": "header,marketSensitive,documentDate", "pageSize": 12})
        items = resp.json().get("data", {}).get("items", [])
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in items:
        d = (it.get("date") or "")[:10]
        if d < cutoff:
            continue
        if only_price_sensitive and not it.get("isPriceSensitive"):
            continue
        out.append({
            "vertical": "asx_ann",
            "match_name": it.get("displayName", code),
            "headline": it.get("headline", ""),
            "phase": it.get("announcementType", ""),
            "date": d,
            "ref": it.get("documentKey", ""),
            "price_sensitive": bool(it.get("isPriceSensitive")),
        })
    return out


def _short(v) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n/div:.1f}{unit}"
    return f"{n:.0f}"
