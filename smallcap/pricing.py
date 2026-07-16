"""
Price + FX helpers for the sleeve — a deliberate DUPLICATE of the main book's
logic, not an import. The isolation guarantee (SPEC §1) is worth a few repeated
lines: the sleeve must not couple to paper_trader.

- current_price_native: latest price in the stock's own currency (USD or AUD)
- fetch_fx_rate: USD per 1 AUD (Yahoo AUDUSD=X), fallback 0.65
- to_aud: convert a native price to AUD given the exchange + fx rate
"""

import requests

HEADERS = {"User-Agent": "OpportunityScout"}
FX_FALLBACK = 0.65  # USD per AUD


def current_price_native(ticker: str) -> float | None:
    """Latest regular-market price in the ticker's native currency, or None."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        return resp.json()["chart"]["result"][0]["meta"].get("regularMarketPrice")
    except Exception:  # noqa: BLE001
        return None


def fetch_fx_rate() -> float:
    """USD per 1 AUD. Falls back to FX_FALLBACK on failure."""
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/AUDUSD=X?interval=1d&range=5d"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        rate = resp.json()["chart"]["result"][0]["meta"].get("regularMarketPrice")
        return float(rate) if rate else FX_FALLBACK
    except Exception:  # noqa: BLE001
        print("[smallcap] FX rate fetch failed, using fallback 0.65")
        return FX_FALLBACK


def to_aud(price_native: float, exchange: str, fx_rate: float) -> float:
    """ASX prices are already AUD; US prices are USD -> divide by USD-per-AUD."""
    if exchange == "ASX":
        return round(price_native, 4)
    return round(price_native / fx_rate, 4)
