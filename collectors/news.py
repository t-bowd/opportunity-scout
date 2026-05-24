"""
Ingests financial news from free RSS feeds.
Stores as signals; Gemini classifier picks out actionable patterns.
"""
import hashlib
import feedparser
from datetime import date, timedelta, datetime
from db.client import insert_signal

FEEDS = [
    # US feeds
    ("reuters_business",  "https://feeds.reuters.com/reuters/businessNews"),
    ("marketwatch_top",   "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("seeking_alpha",     "https://seekingalpha.com/market_currents.xml"),
    ("yahoo_finance",     "https://finance.yahoo.com/news/rssindex"),
    ("sec_press",         "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&dateb=&owner=include&count=40&output=atom"),
    ("investing_com",     "https://www.investing.com/rss/news_25.rss"),
    # Australian feeds
    ("abc_business",      "https://www.abc.net.au/news/feed/51120/rss.xml"),
    ("smh_business",      "https://www.smh.com.au/rss/business.xml"),
]

KEYWORDS = [
    # US signals
    "IPO", "goes public", "S-1", "direct listing", "SPAC", "merger",
    "ETF launch", "new fund", "pre-IPO", "SPV", "secondary", "tender offer",
    "insider buying", "13D", "activist", "spin-off", "spinoff",
    "short squeeze", "short interest", "index inclusion",
    # Australian signals
    "ASX", "prospectus", "substantial shareholder", "director buying",
    "takeover", "scheme of arrangement", "capital raising", "placement",
    "demerger", "IPO Australia", "listing", "ASX 200",
]


def _is_relevant(text: str) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in KEYWORDS)


def _stable_id(feed_name: str, entry_id: str) -> str:
    return hashlib.sha256(f"{feed_name}:{entry_id}".encode()).hexdigest()[:32]


def collect() -> int:
    cutoff = date.today() - timedelta(days=1)
    inserted = 0

    for feed_name, url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[news] {feed_name} parse error: {e}")
            continue

        for entry in feed.entries:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            combined = f"{title} {summary}"

            if not _is_relevant(combined):
                continue

            pub = entry.get("published_parsed")
            if pub:
                entry_date = datetime(pub.tm_year, pub.tm_mon, pub.tm_mday).date()
                if entry_date < cutoff:
                    continue

            signal = {
                "source": "news",
                "accession_no": _stable_id(feed_name, entry.get("id", title)),
                "pattern": None,  # Gemini will classify
                "signal_date": date.today().isoformat(),
                "url": entry.get("link"),
                "raw_data": {
                    "feed": feed_name,
                    "title": title,
                    "summary": summary,
                    "link": entry.get("link"),
                    "published": entry.get("published", ""),
                },
            }
            result = insert_signal(signal)
            if result:
                inserted += 1

    print(f"[news] inserted {inserted} relevant news signals")
    return inserted


if __name__ == "__main__":
    collect()
