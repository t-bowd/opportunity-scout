"""
Tracks newly launched ETFs via ETF.com RSS and EDGAR N-1A cross-reference.
Also checks AUM for recently launched ETFs to spot traction.
"""
import feedparser
import requests
from datetime import date, timedelta
from db.client import insert_signal

# ETF.com publishes a new-ETF feed; fall back to EDGAR N-1A (handled by edgar.py)
ETF_COM_RSS = "https://www.etf.com/sections/features/new-etfs?format=rss"

# Yahoo Finance for quick AUM/volume proxy on known tickers
YF_QUOTE = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1mo"


def _check_aum_traction(ticker: str) -> dict | None:
    """Return basic volume/price data for a ticker, or None on failure."""
    try:
        resp = requests.get(
            YF_QUOTE.format(ticker=ticker),
            headers={"User-Agent": "OpportunityScout"},
            timeout=10,
        )
        resp.raise_for_status()
        meta = resp.json()["chart"]["result"][0]["meta"]
        return {
            "avg_volume": meta.get("regularMarketVolume"),
            "price": meta.get("regularMarketPrice"),
            "currency": meta.get("currency"),
        }
    except Exception:
        return None


def collect() -> int:
    inserted = 0
    feed = feedparser.parse(ETF_COM_RSS)
    cutoff = date.today() - timedelta(days=2)

    for entry in feed.entries:
        pub = entry.get("published_parsed")
        if pub:
            from datetime import datetime
            entry_date = datetime(pub.tm_year, pub.tm_mon, pub.tm_mday).date()
            if entry_date < cutoff:
                continue

        raw = {
            "title": entry.get("title", ""),
            "summary": entry.get("summary", ""),
            "link": entry.get("link", ""),
            "published": entry.get("published", ""),
        }

        signal = {
            "source": "etf_launch",
            "accession_no": entry.get("id") or entry.get("link"),
            "pattern": "thematic_etf",
            "signal_date": date.today().isoformat(),
            "url": entry.get("link"),
            "raw_data": raw,
        }
        result = insert_signal(signal)
        if result:
            inserted += 1

    print(f"[etf_launches] inserted {inserted} new ETF launch signals")
    return inserted


if __name__ == "__main__":
    collect()
