#!/usr/bin/env python3
import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

import ccass_local
import ccass_scraper


def iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def local_snapshot(stock_code, days=365):
    result = ccass_local.query_stock(stock_code, days=days, limit=10000)
    rows = result.get("rows", [])
    if not rows:
        return None
    latest = max(iso(row["at_date"]) for row in rows)
    selected = [row for row in rows if iso(row["at_date"]) == latest]
    participants = {}
    for row in selected:
        pid = str(row.get("part_id") or "N/A")
        item = participants.setdefault(pid, {"participant_id": pid, "name": row.get("short_name") or "N/A", "shares": 0})
        item["shares"] += int(row.get("holding") or 0)
    ordered = sorted(participants.values(), key=lambda item: item["shares"], reverse=True)
    total = sum(item["shares"] for item in ordered)
    for item in ordered:
        item["percentage"] = round(item["shares"] * 100 / total, 4) if total else 0
    return {"date": latest, "stock_code": str(stock_code).zfill(5), "participants": ordered, "total_participants": len(ordered), "total_shares": total, "source": "local", "rows": len(rows)}


def live_snapshot(stock_code, date_str=None):
    data = ccass_scraper.fetch_ccass(str(stock_code).zfill(5), date_str)
    if data.get("error"):
        return data
    participants = []
    for item in data.get("participants", []):
        participants.append({
            "participant_id": item.get("participant_id") or item.get("id") or "N/A",
            "name": item.get("name") or "N/A",
            "shares": int(item.get("shares") or 0),
            "percentage": float(item.get("percentage") or 0),
        })
    participants.sort(key=lambda item: item["shares"], reverse=True)
    total = sum(item["shares"] for item in participants)
    return {**data, "stock_code": str(stock_code).zfill(5), "participants": participants, "total_shares": total, "source": "web", "fetched_at": datetime.now().isoformat(timespec="seconds")}


def analyze(snapshot, top_n=30):
    if not snapshot or snapshot.get("error"):
        return snapshot or {"error": "No CCASS data"}
    participants = snapshot.get("participants", [])
    return {
        "stock_code": snapshot["stock_code"],
        "date": snapshot.get("date", ""),
        "source": snapshot.get("source", "unknown"),
        "total_participants": len(participants),
        "total_shares": snapshot.get("total_shares", sum(item["shares"] for item in participants)),
        "concentration": {
            "top_5": round(sum(item["percentage"] for item in participants[:5]), 2),
            "top_10": round(sum(item["percentage"] for item in participants[:10]), 2),
            "top_20": round(sum(item["percentage"] for item in participants[:20]), 2),
        },
        "top_holders": [{"rank": index + 1, **item} for index, item in enumerate(participants[:top_n])],
    }


def get_stock_data(stock_code, start_date=None, end_date=None, prefer_web=True):
    code = str(stock_code).zfill(5)
    local = local_snapshot(code)
    today = date.today().isoformat()
    target = end_date or today
    web = None
    web_error = None
    if prefer_web and target > "2026-05-22":
        try:
            web = live_snapshot(code, end_date)
            if web.get("error"):
                web_error = web.get("error")
                web = None
        except Exception as exc:
            web_error = str(exc)
    selected = web or local
    result = {
        "stock_code": code,
        "requested_range": [start_date, end_date],
        "local_coverage": ccass_local.manifest().get("combined_range"),
        "local": analyze(local) if local else {"error": "Local CCASS data unavailable"},
        "live": analyze(web) if web else {"error": web_error or "Live CCASS data unavailable"},
        "selected": analyze(selected),
        "fallback_used": web is None,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return result


def local_range_snapshot(stock_code, start_date=None, end_date=None):
    result = ccass_local.query_stock_range(stock_code, start_date, end_date)
    rows = result.get("rows", [])
    if not rows:
        return None
    by_date = {}
    for row in rows:
        day = iso(row["at_date"])
        by_date.setdefault(day, {})
        pid = str(row.get("part_id") or "N/A")
        item = by_date[day].setdefault(pid, {"participant_id": pid, "name": row.get("short_name") or "N/A", "shares": 0})
        item["shares"] += int(row.get("holding") or 0)
    snapshots = []
    for day, participants in sorted(by_date.items()):
        ordered = sorted(participants.values(), key=lambda item: item["shares"], reverse=True)
        total = sum(item["shares"] for item in ordered)
        for item in ordered:
            item["percentage"] = round(item["shares"] * 100 / total, 4) if total else 0
        snapshots.append({"date": day, "participants": ordered, "total_shares": total})
    return {"stock_code": str(stock_code).zfill(5), "source": "local", "snapshots": snapshots, "start_date": snapshots[0]["date"], "end_date": snapshots[-1]["date"], "data_points": len(snapshots)}


def analyze_range(stock_code, start_date=None, end_date=None):
    history = local_range_snapshot(stock_code, start_date, end_date)
    if not history:
        return {"error": "Local CCASS history unavailable"}
    first = history["snapshots"][0]
    last = history["snapshots"][-1]
    def index(snapshot):
        return {p["participant_id"]: p for p in snapshot["participants"]}
    old, new = index(first), index(last)
    changes = []
    for pid in set(old) | set(new):
        before, after = old.get(pid, {"name": "N/A", "shares": 0, "percentage": 0}), new.get(pid, {"name": old.get(pid, {}).get("name", "N/A"), "shares": 0, "percentage": 0})
        ds = after["shares"] - before["shares"]
        dp = round(after["percentage"] - before["percentage"], 4)
        if ds or dp:
            changes.append({"participant_id": pid, "name": after["name"], "shares_before": before["shares"], "shares_after": after["shares"], "delta_shares": ds, "percentage_before": before["percentage"], "percentage_after": after["percentage"], "delta_percentage": dp})
    changes.sort(key=lambda item: abs(item["delta_shares"]), reverse=True)
    return {"stock_code": history["stock_code"], "date_before": first["date"], "date_after": last["date"], "data_points": history["data_points"], "changes": changes, "analysis_before": analyze({"stock_code": history["stock_code"], "date": first["date"], "participants": first["participants"], "total_shares": first["total_shares"]}), "analysis_after": analyze({"stock_code": history["stock_code"], "date": last["date"], "participants": last["participants"], "total_shares": last["total_shares"]})}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_code")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--range", action="store_true")
    args = parser.parse_args()
    if args.range:
        result = analyze_range(args.stock_code, args.start, args.end)
    else:
        result = get_stock_data(args.stock_code, args.start, args.end, not args.local_only)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
