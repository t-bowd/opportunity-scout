"""
Small-cap ticker universes (US + ASX), fetched from free/auth-free sources.

- US:  Nasdaq stock screener download endpoint (symbol, name, market cap, sector,
       industry, country, ipo year). Covers NYSE/Nasdaq/AMEX.
- ASX: the markitdigital company directory (code, name, GICS industry, market cap).

Both are normalised to a common Company schema so the rest of the sleeve is
exchange-agnostic. We also build a normalised name -> ticker index: the sleeve
matches catalyst-feed *names* (trial sponsors, contract recipients) against KNOWN
tickers (universe-first), which sidesteps the unreliable name->ticker search that
free lookup APIs give (see memory `smallcap-catalyst-sleeve`).
"""

import csv
import io
import re
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (OpportunityScout smallcap screener)"}

NASDAQ_SCREENER = (
    "https://api.nasdaq.com/api/screener/stocks"
    "?tableonly=false&limit=25000&download=true"
)
# The public directory-file export (code, name, GICS group, listing date, market
# cap). The access_token is the one the ASX website's own frontend uses; if it
# ever rotates and this 404s/401s, refresh it from asx.com.au's company directory.
ASX_DIRECTORY = ("https://asx.api.markitdigital.com/asx-research/1.0/companies/"
                 "directory/file?access_token=83ff96335c2d45a094df02a206a39ff4")

# Corporate-suffix / share-class noise stripped before name matching. Order matters
# (longest first) so e.g. "common stock" is removed before "stock".
_NAME_NOISE = [
    "american depositary shares", "american depositary share", "ordinary shares",
    "common shares", "common stock", "class a", "class b", "warrants", "rights",
    "depositary", "incorporated", "corporation", "limited", "holdings", "company",
    "group", "plc", "ltd", "inc", "corp", "llc", "co", "sa", "ag", "nv", "the",
]


def normalise_name(name: str) -> str:
    """Lowercase, strip punctuation + corporate/share-class noise -> match key."""
    s = (name or "").lower()
    s = re.sub(r"[.,&/()\-']", " ", s)
    for noise in _NAME_NOISE:
        s = re.sub(rf"\b{re.escape(noise)}\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _to_float(v) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (ValueError, AttributeError):
        return None


def fetch_us(max_cap: float = 2e9, min_cap: float = 1e7) -> list[dict]:
    """US small-caps within [min_cap, max_cap] USD market cap."""
    try:
        resp = requests.get(NASDAQ_SCREENER, headers=HEADERS, timeout=45)
        rows = resp.json()["data"]["rows"]
    except Exception as e:  # noqa: BLE001 — a dead feed shouldn't crash the screen
        print(f"[smallcap/universe] US fetch failed: {e}")
        return []
    out = []
    for r in rows:
        cap = _to_float(r.get("marketCap"))
        if cap is None or not (min_cap <= cap <= max_cap):
            continue
        out.append({
            "ticker": r.get("symbol", "").strip(),
            "name": r.get("name", "").strip(),
            "exchange": "US",
            "sector": (r.get("sector") or "").strip(),
            "industry": (r.get("industry") or "").strip(),
            "market_cap": cap,
            "country": (r.get("country") or "").strip(),
            "ipo_year": (r.get("ipoyear") or "").strip(),
        })
    return out


def fetch_asx(max_cap: float = 2e9, min_cap: float = 1e7) -> list[dict]:
    """ASX small-caps within [min_cap, max_cap] AUD market cap. Yahoo needs .AX."""
    try:
        resp = requests.get(ASX_DIRECTORY, headers=HEADERS, timeout=45)
        text = resp.text
    except Exception as e:  # noqa: BLE001
        print(f"[smallcap/universe] ASX fetch failed: {e}")
        return []
    out = []
    # The directory is CSV with a preamble line; find the header row and parse.
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.lower().startswith('"asx code"')), 0)
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    for r in reader:
        code = (r.get("ASX code") or "").strip().strip('"')
        cap = _to_float(r.get("Market Cap"))
        if not code or cap is None or not (min_cap <= cap <= max_cap):
            continue
        out.append({
            "ticker": f"{code}.AX",
            "name": (r.get("Company name") or "").strip().strip('"'),
            "exchange": "ASX",
            "sector": (r.get("GICs industry group") or r.get("GICS industry group") or "").strip().strip('"'),
            "industry": (r.get("GICs industry group") or r.get("GICS industry group") or "").strip().strip('"'),
            "market_cap": cap,
            "country": "Australia",
            "ipo_year": "",
        })
    return out


def load_universe(max_cap: float = 2e9, min_cap: float = 1e7,
                  include_us: bool = True, include_asx: bool = True) -> list[dict]:
    """Combined US + ASX small-cap universe."""
    uni: list[dict] = []
    if include_us:
        uni += fetch_us(max_cap, min_cap)
    if include_asx:
        uni += fetch_asx(max_cap, min_cap)
    print(f"[smallcap/universe] loaded {len(uni)} small-caps "
          f"(cap ${min_cap/1e6:.0f}M–${max_cap/1e9:.1f}B)")
    return uni


def name_index(universe: list[dict]) -> dict[str, dict]:
    """normalised-name -> company. Later duplicates lose (first listing wins)."""
    idx: dict[str, dict] = {}
    for c in universe:
        key = normalise_name(c["name"])
        if key and key not in idx:
            idx[key] = c
    return idx
