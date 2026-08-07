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
import ccass_snapshot


def iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def local_snapshot(stock_code, days=365, as_of=None):
    """True holder snapshot from the reconstructed change-log (see ccass_snapshot)."""
    snap = ccass_snapshot.snapshot(stock_code, as_of, top_n=10_000)
    if snap.get("error"):
        return None
    return {
        "date": snap["date"],
        "stock_code": snap["stock_code"],
        "participants": snap["participants"],
        "total_participants": snap["total_participants"],
        "total_shares": snap["ccass_total"],
        "concentration": snap["concentration"],
        "percentage_base": snap["percentage_base"],
        "basis": snap["basis"],
        "issued_shares": snap["issued_shares"],
        "ccass_share_of_issued": snap["ccass_share_of_issued"],
        "dailylog_participants": snap["dailylog_participants"],
        "source": "local",
    }


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
    conc = snapshot.get("concentration")
    if not conc:
        def cum(n):
            return round(sum(item.get("percentage", 0) for item in participants[:n]), 2)
        conc = {"top_5": cum(5), "top_10": cum(10), "top_20": cum(20)}
    return {
        "stock_code": snapshot["stock_code"],
        "date": snapshot.get("date", ""),
        "source": snapshot.get("source", "unknown"),
        "basis": snapshot.get("basis"),
        "issued_shares": snapshot.get("issued_shares"),
        "percentage_base": snapshot.get("percentage_base"),
        "ccass_share_of_issued": snapshot.get("ccass_share_of_issued"),
        "total_participants": snapshot.get("total_participants", len(participants)),
        "dailylog_participants": snapshot.get("dailylog_participants"),
        "total_shares": snapshot.get("total_shares", sum(item["shares"] for item in participants)),
        "concentration": conc,
        "top_holders": [{"rank": i + 1, **item} for i, item in enumerate(participants[:top_n])],
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
        "local_coverage_by_table": ccass_local.manifest().get("table_coverage", {}),
        "local": analyze(local) if local else {"error": "Local CCASS data unavailable"},
        "live": analyze(web) if web else {"error": web_error or "Live CCASS data unavailable"},
        "selected": analyze(selected),
        "fallback_used": web is None,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return result


def local_range_snapshot(stock_code, start_date=None, end_date=None, max_points=40):
    """Build true snapshots with exact start/end anchors.

    The holdings files are a change log. Using the first movement after the
    requested start date makes the baseline wrong and can reverse the apparent
    largest accumulator/distributor. Always forward-fill the requested start
    and end dates, then add movement dates inside the interval for tracking.
    """
    requested_start = start_date or ccass_snapshot.coverage_range()[0]
    requested_end = end_date or ccass_snapshot.market_latest_date()
    if not requested_start or not requested_end:
        return None

    days = ccass_snapshot.trading_dates(stock_code, requested_start, requested_end, max(2, max_points))
    anchor_days = [requested_start, *days, requested_end]
    snapshots_by_date = {}

    for day in anchor_days:
        snap = ccass_snapshot.snapshot(stock_code, day, top_n=10_000)
        if snap.get("error"):
            continue
        effective_day = snap["date"]
        snapshots_by_date[effective_day] = {
            "date": effective_day,
            "participants": snap["participants"],
            "total_shares": snap["ccass_total"],
            "concentration": snap["concentration"],
            "percentage_base": snap["percentage_base"],
            "total_participants": snap["total_participants"],
        }

    snapshots = [snapshots_by_date[day] for day in sorted(snapshots_by_date)]
    if not snapshots:
        return None
    return {
        "stock_code": ccass_snapshot.pad_code(stock_code),
        "source": "local",
        "snapshots": snapshots,
        "start_date": snapshots[0]["date"],
        "end_date": snapshots[-1]["date"],
        "requested_start": requested_start,
        "requested_end": requested_end,
        "data_points": len(snapshots),
    }


def analyze_range(stock_code, start_date=None, end_date=None):
    history = local_range_snapshot(stock_code, start_date, end_date)
    if not history:
        return {"error": "Local CCASS history unavailable"}
    first, last = history["snapshots"][0], history["snapshots"][-1]

    def index(snap):
        return {p["participant_id"]: p for p in snap["participants"]}

    old, new = index(first), index(last)
    changes = []
    for pid in set(old) | set(new):
        b = old.get(pid)
        a = new.get(pid)
        name = (a or b)["name"]
        sb = b["shares"] if b else 0
        sa = a["shares"] if a else 0
        pb = b["percentage"] if b else 0.0
        pa = a["percentage"] if a else 0.0
        if sa == sb:
            continue
        changes.append({
            "participant_id": pid,
            "name": name,
            "shares_before": sb,
            "shares_after": sa,
            "delta_shares": sa - sb,
            "percentage_before": pb,
            "percentage_after": pa,
            "delta_percentage": round(pa - pb, 4),
            "direction": "加倉" if sa > sb else "減倉",
            "is_new": b is None,
            "is_exit": sa == 0,
        })
    changes.sort(key=lambda item: abs(item["delta_percentage"]), reverse=True)

    def wrap(snap):
        return analyze({
            "stock_code": history["stock_code"],
            "date": snap["date"],
            "participants": snap["participants"],
            "total_shares": snap["total_shares"],
            "concentration": snap.get("concentration"),
            "percentage_base": snap.get("percentage_base"),
        })

    return {
        "stock_code": history["stock_code"],
        "date_before": first["date"],
        "date_after": last["date"],
        "data_points": history["data_points"],
        "concentration_before": first.get("concentration"),
        "concentration_after": last.get("concentration"),
        "delta_top_5": round((last.get("concentration") or {}).get("top_5", 0) - (first.get("concentration") or {}).get("top_5", 0), 2),
        "delta_top_10": round((last.get("concentration") or {}).get("top_10", 0) - (first.get("concentration") or {}).get("top_10", 0), 2),
        "accumulators": [c for c in changes if c["delta_shares"] > 0][:20],
        "distributors": [c for c in changes if c["delta_shares"] < 0][:20],
        "changes": changes[:100],
        "analysis_before": wrap(first),
        "analysis_after": wrap(last),
    }


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
