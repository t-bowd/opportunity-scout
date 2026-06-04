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


def signal_exists(accession_no: str) -> bool:
    """True if a signal with this accession is already stored. Used to avoid
    re-fetching Form 4 / 13F filing bodies for filings we've already processed."""
    db = get_client()
    result = (
        db.table("signals").select("id").eq("accession_no", accession_no).limit(1).execute()
    )
    return len(result.data) > 0


def filing_has_signals(accession_prefix: str) -> bool:
    """True if any signal exists whose accession starts with this prefix. 13F
    holdings store accession as '<base>-<cusip>', so this lets us tell whether a
    13F filing has already been processed without re-fetching its holdings."""
    db = get_client()
    result = (
        db.table("signals")
        .select("id")
        .like("accession_no", f"{accession_prefix}-%")
        .limit(1)
        .execute()
    )
    return len(result.data) > 0


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


def get_unprocessed_signals(limit: int = 50, max_age_days: int = 7) -> list[dict]:
    """
    Fetch unprocessed signals from the last max_age_days days.
    Signals older than the cutoff are ignored — if Gemini repeatedly
    fails on a signal it will age out naturally rather than accumulating.
    """
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    db = get_client()
    return (
        db.table("signals")
        .select("*")
        .eq("processed", False)
        .gte("signal_date", cutoff)
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


def opportunity_exists(vehicle: str, week_of: str) -> bool:
    """True if this ticker has already been scored for this week."""
    db = get_client()
    result = (
        db.table("opportunities")
        .select("id")
        .eq("vehicle", vehicle)
        .eq("week_of", week_of)
        .execute()
    )
    return len(result.data) > 0


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


def get_recent_opportunities(days: int = 10, limit: int = 25) -> list[dict]:
    """
    Opportunities scored within the last `days`, highest score first.
    Used by the paper trader so entries aren't reset to an empty pool every
    Monday when the calendar week rolls over — per-pattern recency windows in
    entry.py still gate how fresh each pick must be.
    """
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    db = get_client()
    return (
        db.table("opportunities")
        .select("*, entities(ticker, name)")
        .gte("created_at", cutoff)
        .order("total_score", desc=True)
        .limit(limit)
        .execute()
        .data
    )


def insert_feedback_rows(opportunity_ids: list[str]) -> None:
    db = get_client()
    rows = [{"opportunity_id": oid} for oid in opportunity_ids]
    db.table("feedback").upsert(rows, on_conflict="opportunity_id").execute()


# ---------------------------------------------------------------------------
# Paper trading
# ---------------------------------------------------------------------------

def get_open_paper_positions() -> list[dict]:
    db = get_client()
    return (
        db.table("paper_positions")
        .select("*")
        .eq("status", "open")
        .order("entry_date", desc=False)
        .execute()
        .data
    )


def get_closed_paper_positions(limit: int = 200) -> list[dict]:
    db = get_client()
    return (
        db.table("paper_positions")
        .select("*")
        .neq("status", "open")
        .order("exit_date", desc=True)
        .limit(limit)
        .execute()
        .data
    )


def paper_position_exists_for_opportunity(opportunity_id: str) -> bool:
    db = get_client()
    result = (
        db.table("paper_positions")
        .select("id")
        .eq("opportunity_id", opportunity_id)
        .execute()
    )
    return len(result.data) > 0


def insert_paper_position(pos: dict) -> str:
    db = get_client()
    result = db.table("paper_positions").insert(pos).execute()
    return result.data[0]["id"]


def close_paper_position(position_id: str, updates: dict) -> None:
    db = get_client()
    db.table("paper_positions").update(updates).eq("id", position_id).execute()


def update_paper_position_peak(
    position_id: str, peak_price_aud: float, trailing_stop_active: bool
) -> None:
    db = get_client()
    db.table("paper_positions").update({
        "peak_price_aud": peak_price_aud,
        "trailing_stop_active": trailing_stop_active,
    }).eq("id", position_id).execute()


def auto_fill_feedback_entry(opportunity_id: str, entry_price_aud: float) -> None:
    """When paper trading enters a position, mark the feedback row as acted."""
    db = get_client()
    db.table("feedback").upsert(
        {"opportunity_id": opportunity_id, "acted": True, "entry_price": entry_price_aud},
        on_conflict="opportunity_id",
    ).execute()


def auto_fill_feedback_exit(opportunity_id: str, grade: int) -> None:
    """When paper trading closes a position, auto-grade the feedback row."""
    db = get_client()
    db.table("feedback").update({"grade": grade}).eq(
        "opportunity_id", opportunity_id
    ).execute()


def insert_paper_skipped(skip: dict) -> None:
    db = get_client()
    db.table("paper_skipped_entries").insert(skip).execute()


def upsert_paper_snapshot(snap: dict) -> None:
    db = get_client()
    db.table("paper_portfolio_snapshots").upsert(snap, on_conflict="snapshot_date").execute()


def get_latest_paper_snapshot() -> dict | None:
    db = get_client()
    result = (
        db.table("paper_portfolio_snapshots")
        .select("*")
        .order("snapshot_date", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


# ---------------------------------------------------------------------------

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
