"""
Phase 1 orchestrator: universe x catalysts x momentum -> ranked watchlist.

Universe-first pipeline:
  1. Load the US + ASX small-cap universe (+ a normalised name index).
  2. Pull catalyst feeds; match each catalyst's name to a KNOWN small-cap ticker.
  3. Scan a bounded set of ASX materials/energy names for price-sensitive filings.
  4. Overlay the momentum ("climbing?") check on the matched candidates only.
  5. Rank and return.

Read-only. Nothing is traded or written to the DB in Phase 1.
"""

from datetime import date

from smallcap import universe as uni_mod
from smallcap import catalysts as cat_mod
from smallcap import momentum as mom_mod


def _attach(cand: dict, company: dict, cat: dict) -> dict:
    return {
        "ticker": company["ticker"],
        "name": company["name"],
        "exchange": company["exchange"],
        "sector": company["sector"],
        "market_cap": company["market_cap"],
        "vertical": cat["vertical"],
        "catalyst": cat["headline"],
        "phase": cat.get("phase", ""),
        "catalyst_date": cat.get("date", ""),
        "ref": cat.get("ref", ""),
        "sponsor_name": cat.get("match_name", ""),
    }


def _match_feed(catalysts: list[dict], name_idx: dict[str, dict]) -> dict[str, dict]:
    """Match catalyst names to known tickers. Returns ticker -> candidate."""
    hits: dict[str, dict] = {}
    for cat in catalysts:
        key = uni_mod.normalise_name(cat.get("match_name", ""))
        company = name_idx.get(key)
        if not company:
            continue
        tkr = company["ticker"]
        # Keep the earliest-dated catalyst per ticker (nearest readout).
        if tkr not in hits or (cat.get("date", "") or "9999") < hits[tkr]["catalyst_date"]:
            hits[tkr] = _attach(hits.get(tkr, {}), company, cat)
    return hits


def run_screen(max_cap: float = 2e9, min_cap: float = 1e7,
               include_us: bool = True, include_asx: bool = True,
               asx_scan_limit: int = 60, climbing_only: bool = False) -> list[dict]:
    universe = uni_mod.load_universe(max_cap, min_cap, include_us, include_asx)
    if not universe:
        print("[smallcap/screen] empty universe — aborting")
        return []
    name_idx = uni_mod.name_index(universe)

    candidates: dict[str, dict] = {}

    # --- Catalyst-first verticals (one API call yields the candidate names) ---
    print("[smallcap/screen] pulling biotech (clinicaltrials.gov)…")
    bio = _match_feed(cat_mod.biotech_catalysts(), name_idx)
    print(f"  biotech: {len(bio)} universe matches")
    candidates.update(bio)

    if include_us:
        print("[smallcap/screen] pulling defense/contract (USAspending)…")
        dfn = _match_feed(cat_mod.defense_catalysts(), name_idx)
        print(f"  defense: {len(dfn)} universe matches")
        for t, c in dfn.items():
            candidates.setdefault(t, c)

    # --- ASX price-sensitive announcement scan (bounded, O(N) per-ticker) ---
    if include_asx and asx_scan_limit > 0:
        asx_names = [c for c in universe if c["exchange"] == "ASX"
                     and any(s in c["sector"].lower() for s in ("material", "energy"))]
        asx_names.sort(key=lambda c: c["market_cap"])  # smallest first (moonshot end)
        scan = asx_names[:asx_scan_limit]
        print(f"[smallcap/screen] scanning {len(scan)} ASX materials/energy names for "
              f"price-sensitive filings…")
        for c in scan:
            code = c["ticker"].replace(".AX", "")
            anns = cat_mod.asx_announcements(code)
            if anns:
                cat = sorted(anns, key=lambda a: a["date"], reverse=True)[0]
                candidates.setdefault(c["ticker"], _attach({}, c, cat))

    # --- Momentum overlay on matched candidates only ---
    print(f"[smallcap/screen] momentum check on {len(candidates)} candidates…")
    out = []
    for tkr, cand in candidates.items():
        mom = mom_mod.momentum(tkr)
        cand["momentum"] = mom
        cand["climbing"] = bool(mom and mom["climbing"])
        cand["ret_20d"] = mom["ret_20d"] if mom else None
        if climbing_only and not cand["climbing"]:
            continue
        out.append(cand)

    out.sort(key=_rank_key, reverse=True)
    print(f"[smallcap/screen] done — {len(out)} names on the watchlist "
          f"({sum(c['climbing'] for c in out)} climbing)")
    return out


def _rank_key(c: dict):
    """Climbing first, then stronger 20d momentum, then smaller cap (more moonshot)."""
    return (
        1 if c["climbing"] else 0,
        c["ret_20d"] if c["ret_20d"] is not None else -999,
        -c["market_cap"],
    )
