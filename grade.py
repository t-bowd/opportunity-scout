"""
CLI tool for grading opportunities.

Usage:
  python grade.py --list                              # show pending feedback rows
  python grade.py --id <opp_id> --grade 4 --acted    # grade an opportunity
  python grade.py --report                            # show accuracy by pattern
"""
import argparse
import os
from db.client import get_client


def list_pending() -> None:
    db = get_client()
    rows = (
        db.table("feedback")
        .select("id, grade, acted, opportunities(title, vehicle, total_score, week_of)")
        .is_("grade", "null")
        .order("created_at", desc=True)
        .limit(20)
        .execute()
        .data
    )
    if not rows:
        print("No pending feedback rows.")
        return
    print(f"\n{'ID':36}  {'Ticker':8}  {'Score':6}  {'Title'}")
    print("-" * 90)
    for r in rows:
        opp = r.get("opportunities", {}) or {}
        print(f"{r['id']}  {opp.get('vehicle','?'):8}  {opp.get('total_score','?'):6}  {opp.get('title','')[:50]}")
    print(f"\nGrade with: python grade.py --id <ID> --grade <1-5> [--acted]\n")


def grade_opportunity(opp_id: str, grade: int, acted: bool, notes: str = "") -> None:
    db = get_client()
    # Find feedback row by opportunity_id
    rows = db.table("feedback").select("id").eq("opportunity_id", opp_id).execute().data
    if not rows:
        print(f"No feedback row found for opportunity {opp_id}")
        return
    fid = rows[0]["id"]
    updates = {"grade": grade, "acted": acted, "updated_at": "now()"}
    if notes:
        updates["notes"] = notes
    db.table("feedback").update(updates).eq("id", fid).execute()
    print(f"Graded opportunity {opp_id}: grade={grade}, acted={acted}")


def show_report() -> None:
    db = get_client()
    rows = (
        db.table("feedback")
        .select("grade, acted, pnl_30d_pct, pnl_90d_pct, opportunities(pattern, total_score)")
        .not_.is_("grade", "null")
        .execute()
        .data
    )
    if not rows:
        print("No graded feedback yet.")
        return

    from collections import defaultdict
    by_pattern: dict[str, list] = defaultdict(list)
    for r in rows:
        pattern = (r.get("opportunities") or {}).get("pattern", "unknown")
        by_pattern[pattern].append(r)

    print(f"\n{'Pattern':20}  {'N':4}  {'Avg Grade':10}  {'Acted%':8}  {'Avg P&L 30d':12}")
    print("-" * 70)
    for pattern, items in sorted(by_pattern.items()):
        n = len(items)
        avg_grade = sum(i["grade"] for i in items if i["grade"]) / n
        acted_pct = sum(1 for i in items if i.get("acted")) / n * 100
        pnl_vals = [i["pnl_30d_pct"] for i in items if i.get("pnl_30d_pct") is not None]
        avg_pnl = f"{sum(pnl_vals)/len(pnl_vals):.1f}%" if pnl_vals else "n/a"
        print(f"{pattern:20}  {n:4}  {avg_grade:10.1f}  {acted_pct:7.0f}%  {avg_pnl:12}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Grade opportunity scout picks")
    parser.add_argument("--list", action="store_true", help="Show pending feedback")
    parser.add_argument("--id", help="Opportunity ID to grade")
    parser.add_argument("--grade", type=int, choices=[1, 2, 3, 4, 5])
    parser.add_argument("--acted", action="store_true", help="Mark as acted upon")
    parser.add_argument("--notes", default="", help="Optional notes")
    parser.add_argument("--report", action="store_true", help="Show accuracy report")
    args = parser.parse_args()

    if args.list:
        list_pending()
    elif args.report:
        show_report()
    elif args.id and args.grade is not None:
        grade_opportunity(args.id, args.grade, args.acted, args.notes)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
