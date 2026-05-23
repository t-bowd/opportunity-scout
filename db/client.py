import os
from supabase import create_client, Client

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        _client = create_client(url, key)
    return _client


def upsert_entity(ticker: str | None, name: str, entity_type: str) -> str:
    """Return entity id, creating it if it doesn't exist."""
    db = get_client()
    if ticker:
        existing = db.table("entities").select("id").eq("ticker", ticker).execute()
        if existing.data:
            return existing.data[0]["id"]
    result = (
        db.table("entities")
        .insert({"ticker": ticker, "name": name, "type": entity_type})
        .execute()
    )
    return result.data[0]["id"]


def insert_signal(signal: dict) -> str | None:
    """Insert a signal. Returns id, or None if duplicate."""
    db = get_client()
    if signal.get("accession_no"):
        existing = (
            db.table("signals")
            .select("id")
            .eq("accession_no", signal["accession_no"])
            .execute()
        )
        if existing.data:
            return None
    result = db.table("signals").insert(signal).execute()
    return result.data[0]["id"]


def get_unprocessed_signals(limit: int = 50) -> list[dict]:
    db = get_client()
    return (
        db.table("signals")
        .select("*")
        .eq("processed", False)
        .order("signal_date", desc=True)
        .limit(limit)
        .execute()
        .data
    )


def mark_signal_processed(signal_id: str, summary: str, pattern: str) -> None:
    db = get_client()
    db.table("signals").update(
        {"processed": True, "summary": summary, "pattern": pattern}
    ).eq("id", signal_id).execute()


def insert_opportunity(opp: dict) -> str:
    db = get_client()
    result = db.table("opportunities").insert(opp).execute()
    return result.data[0]["id"]


def get_top_opportunities(week_of: str, limit: int = 5) -> list[dict]:
    db = get_client()
    return (
        db.table("opportunities")
        .select("*, entities(ticker, name)")
        .eq("week_of", week_of)
        .order("total_score", desc=True)
        .limit(limit)
        .execute()
        .data
    )


def insert_feedback_rows(opportunity_ids: list[str]) -> None:
    db = get_client()
    rows = [{"opportunity_id": oid} for oid in opportunity_ids]
    db.table("feedback").upsert(rows, on_conflict="opportunity_id").execute()


def get_feedback_pending_pnl() -> list[dict]:
    """Opportunities needing 30/90d price checks."""
    from datetime import date, timedelta
    db = get_client()
    cutoff_30 = (date.today() - timedelta(days=30)).isoformat()
    cutoff_90 = (date.today() - timedelta(days=90)).isoformat()
    return (
        db.table("feedback")
        .select("id, opportunity_id, opportunities(vehicle, price_at_score, created_at), price_30d, price_90d")
        .execute()
        .data
    )


def update_feedback_pnl(feedback_id: str, updates: dict) -> None:
    db = get_client()
    db.table("feedback").update(updates).eq("id", feedback_id).execute()
