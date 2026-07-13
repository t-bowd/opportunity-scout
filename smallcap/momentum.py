"""
"Climbing stock" momentum overlay — the second half of the sleeve's thesis
(a catalyst on a name that is ALREADY trending up, not a falling knife).

One Yahoo v8 daily fetch per ticker, same auth-free endpoint the main book uses.
Returns a compact momentum verdict; fails soft (None) so a data hiccup on one
name never sinks the screen.
"""

import requests

HEADERS = {"User-Agent": "OpportunityScout"}
YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1y"


def momentum(ticker: str) -> dict | None:
    """
    Climbing verdict for a ticker, or None if price data is unavailable.

    climbing = up over the last ~month AND trading above its 50-day average AND
    not pinned to the 52-week low. Deliberately simple and legible for Phase 1;
    the point is to separate "catalyst on a name the market already likes" from
    "catalyst on a name bleeding out."
    """
    try:
        resp = requests.get(YF_CHART.format(ticker=ticker), headers=HEADERS, timeout=10)
        data = resp.json()["chart"]["result"][0]
        closes = [c for c in data["indicators"]["quote"][0].get("close", []) if c]
        price = data["meta"].get("regularMarketPrice")
    except Exception:  # noqa: BLE001 — fail soft on any data issue
        return None
    if not closes or not price or len(closes) < 25:
        return None

    hi, lo = max(closes), min(closes)
    sma50 = sum(closes[-50:]) / len(closes[-50:])
    ref_20 = closes[-21] if len(closes) > 21 else closes[0]
    ref_5 = closes[-6] if len(closes) > 6 else closes[0]
    ret_20d = (price - ref_20) / ref_20 * 100 if ref_20 else 0.0
    ret_5d = (price - ref_5) / ref_5 * 100 if ref_5 else 0.0
    from_high = (price - hi) / hi * 100
    above_low = (price - lo) / lo * 100

    climbing = ret_20d > 0 and price >= sma50 and above_low > 15
    return {
        "price": round(price, 4),
        "ret_5d": round(ret_5d, 1),
        "ret_20d": round(ret_20d, 1),
        "from_high": round(from_high, 1),
        "above_low": round(above_low, 1),
        "above_sma50": price >= sma50,
        "climbing": climbing,
    }
