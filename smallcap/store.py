"""
Persistence for the sleeve — its OWN tables, via the shared low-level Supabase
client only. Deliberately does NOT import any paper_trader/analyzer DB helpers,
and touches only smallcap_* tables (SPEC §1 isolation).

Tables (see smallcap/schema.sql): smallcap_positions, smallcap_skipped,
smallcap_snapshots.
"""

from db.client import get_client


def get_open_positions() -> list[dict]:
    db = get_client()
    return (
        db.table("smallcap_positions").select("*")
        .eq("status", "open").order("entry_date", desc=False).execute().data
    )


def get_closed_positions(limit: int = 500) -> list[dict]:
    db = get_client()
    return (
        db.table("smallcap_positions").select("*")
        .neq("status", "open").order("exit_date", desc=True).limit(limit).execute().data
    )


def insert_position(pos: dict) -> str:
    db = get_client()
    return db.table("smallcap_positions").insert(pos).execute().data[0]["id"]


def close_position(position_id: str, updates: dict) -> None:
    db = get_client()
    db.table("smallcap_positions").update(updates).eq("id", position_id).execute()


def update_position_peak(position_id: str, peak_price_aud: float,
                         trailing_stop_active: bool) -> None:
    db = get_client()
    db.table("smallcap_positions").update({
        "peak_price_aud": peak_price_aud,
        "trailing_stop_active": trailing_stop_active,
    }).eq("id", position_id).execute()


def insert_skipped(skip: dict) -> None:
    db = get_client()
    db.table("smallcap_skipped").insert(skip).execute()


def upsert_snapshot(snap: dict) -> None:
    db = get_client()
    db.table("smallcap_snapshots").upsert(snap, on_conflict="snapshot_date").execute()
