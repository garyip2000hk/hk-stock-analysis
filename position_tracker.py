"""Local-only CCASS position tracking for the 财技分析 app."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime

import ccass_snapshot as cs


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _sample_dates(values: list[str], limit: int) -> list[str]:
    values = sorted(set(values))
    if len(values) <= limit:
        return values
    if limit <= 2:
        return [values[0], values[-1]][:limit]
    step = (len(values) - 1) / (limit - 1)
    return sorted({values[round(i * step)] for i in range(limit)})


def _flow(before: dict, after: dict) -> dict:
    before_shares = int(before.get("shares", 0)) if before else 0
    after_shares = int(after.get("shares", 0)) if after else 0
    before_pct = float(before.get("percentage", 0)) if before else 0.0
    after_pct = float(after.get("percentage", 0)) if after else 0.0
    return {
        "participant_id": (after or before).get("participant_id", ""),
        "name": (after or before).get("name", ""),
        "shares_before": before_shares,
        "shares_after": after_shares,
        "delta_shares": after_shares - before_shares,
        "percentage_before": round(before_pct, 4),
        "percentage_after": round(after_pct, 4),
        "delta_percentage": round(after_pct - before_pct, 4),
        "direction": "加倉" if after_shares > before_shares else "減倉" if after_shares < before_shares else "無變動",
    }


def track(stock: str, start: str, end: str, points: int = 24) -> dict:
    code = cs.pad_code(stock)
    requested_start = _date(start)
    requested_end = _date(end)
    if requested_start > requested_end:
        return {"error": "開始日期不可晚於結束日期", "stock_code": code}

    coverage_start, coverage_end = cs.coverage_range()
    if not coverage_start or not coverage_end:
        return {"error": "本地 CCASS 資料庫沒有覆蓋日期", "stock_code": code}
    effective_start = max(requested_start, _date(coverage_start))
    effective_end = min(requested_end, _date(coverage_end))
    if effective_start > effective_end:
        return {"error": "所選日期超出本地 CCASS 覆蓋範圍", "stock_code": code, "coverage": [coverage_start, coverage_end]}

    movement_dates = cs.trading_dates(code, str(effective_start), str(effective_end), max_points=500)
    dates = _sample_dates([str(effective_start), str(effective_end), *movement_dates], max(2, min(int(points), 60)))
    snapshots = []
    for d in dates:
        snap = cs.snapshot(code, d, top_n=60)
        if snap.get("error"):
            return {"error": snap["error"], "stock_code": code}
        snapshots.append(snap)

    point_rows = []
    for snap in snapshots:
        point_rows.append({
            "date": snap["date"],
            "last_movement": snap.get("last_movement"),
            "participants": snap.get("total_participants", 0),
            "total_shares": snap.get("ccass_total", 0),
            "ccass_share_of_issued": snap.get("ccass_share_of_issued"),
            "top_5_pct": snap.get("concentration", {}).get("top_5", 0),
            "top_10_pct": snap.get("concentration", {}).get("top_10", 0),
            "top_holders": snap.get("top_holders", [])[:10],
        })

    first = snapshots[0]
    last = snapshots[-1]
    before = {h["participant_id"]: h for h in first.get("participants", [])}
    after = {h["participant_id"]: h for h in last.get("participants", [])}
    all_ids = set(before) | set(after)
    flows = [_flow(before.get(pid), after.get(pid)) for pid in all_ids]
    flows = [f for f in flows if f["delta_shares"]]
    accumulators = sorted((f for f in flows if f["delta_shares"] > 0), key=lambda x: x["delta_shares"], reverse=True)
    distributors = sorted((f for f in flows if f["delta_shares"] < 0), key=lambda x: x["delta_shares"])

    tracked_ids = set()
    for snap in snapshots:
        tracked_ids.update(h["participant_id"] for h in snap.get("top_holders", [])[:20])
    tracked = []
    for pid in tracked_ids:
        flow = _flow(before.get(pid), after.get(pid))
        series = []
        for snap in snapshots:
            holder = next((h for h in snap.get("participants", []) if h["participant_id"] == pid), None)
            series.append({"date": snap["date"], "shares": holder["shares"] if holder else 0, "percentage": holder["percentage"] if holder else 0})
        flow["series"] = series
        tracked.append(flow)
    tracked.sort(key=lambda x: abs(x["delta_shares"]), reverse=True)

    return {
        "stock_code": code,
        "requested_from": start,
        "requested_to": end,
        "effective_from": point_rows[0]["date"],
        "effective_to": point_rows[-1]["date"],
        "point_count": len(point_rows),
        "coverage": [coverage_start, coverage_end],
        "source": "local-reconstructed",
        "points": point_rows,
        "tracked": tracked[:40],
        "accumulators": accumulators[:30],
        "distributors": distributors[:30],
        "data_quality": {
            "baseline_date": point_rows[0]["date"],
            "end_date": point_rows[-1]["date"],
            "note": "由 CCASS change-log forward-fill 重建；百分比以已發行股數計算。",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock")
    parser.add_argument("start")
    parser.add_argument("end")
    parser.add_argument("--points", type=int, default=24)
    args = parser.parse_args()
    print(json.dumps(track(args.stock, args.start, args.end, args.points), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
