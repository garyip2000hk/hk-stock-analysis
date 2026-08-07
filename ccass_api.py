#!/usr/bin/env python3
"""Single entry point for the /api/ccass route.

Emits exactly the shape the frontend expects, using the corrected
snapshot reconstruction in `ccass_snapshot` (see that module's docstring
for why reading a raw `at_date` was wrong).

Modes:
  snapshot  --stock 01241 [--as-of YYYY-MM-DD]
  diff      --stock 01241 --start YYYY-MM-DD --end YYYY-MM-DD
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import ccass_snapshot


def _coverage() -> dict:
    try:
        import ccass_local

        meta = ccass_local.manifest()
        return {
            "coverage": meta.get("combined_range") or [],
            "coverage_by_table": meta.get("table_coverage") or {},
        }
    except Exception:
        return {"coverage": [], "coverage_by_table": {}}


def snapshot_payload(stock: str, as_of: str | None = None, top_n: int = 30) -> dict:
    snap = ccass_snapshot.snapshot(stock, as_of, top_n=top_n)
    if snap.get("error"):
        return {**snap, **_coverage()}

    dormant = None
    if snap.get("dailylog_participants"):
        dormant = max(0, int(snap["dailylog_participants"]) - snap["total_participants"])

    return {
        "stock_code": snap["stock_code"],
        "date": snap["date"],
        "source": snap["source"],
        # How percentages were computed — surfaced so the UI never implies
        # a 100% figure that is really "sum of what we can see".
        "basis": snap["basis"],
        "issued_shares": snap["issued_shares"],
        "percentage_base": snap["percentage_base"],
        "ccass_total": snap["ccass_total"],
        "ccass_share_of_issued": snap["ccass_share_of_issued"],
        "total_participants": snap["total_participants"],
        "dailylog_participants": snap["dailylog_participants"],
        "unmapped_participants": dormant,
        "total_shares": snap["ccass_total"],
        "concentration": snap["concentration"],
        "top_holders": snap["top_holders"],
        **_coverage(),
    }


def diff_payload(stock: str, start: str, end: str, min_pct: float = 0.1) -> dict:
    mv = ccass_snapshot.movements(stock, start, end, min_pct=min_pct)
    if mv.get("error"):
        return {**mv, **_coverage()}

    before = snapshot_payload(stock, start, top_n=50)
    after = snapshot_payload(stock, end, top_n=50)

    return {
        "stock_code": mv["stock_code"],
        "date_before": mv["date_before"],
        "date_after": mv["date_after"],
        "concentration_before": mv["concentration_before"],
        "concentration_after": mv["concentration_after"],
        "delta_top_5": mv["delta_top_5"],
        "delta_top_10": mv["delta_top_10"],
        "accumulators": mv["accumulators"],
        "distributors": mv["distributors"],
        "significant_count": mv["significant_count"],
        "changes": mv["changes"],
        "analysis_before": before,
        "analysis_after": after,
        **_coverage(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["snapshot", "diff"])
    ap.add_argument("--stock", required=True)
    ap.add_argument("--as-of")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--min-pct", type=float, default=0.1)
    args = ap.parse_args()

    if args.mode == "diff":
        if not (args.start and args.end):
            print(json.dumps({"error": "diff mode 需要 --start 同 --end"}, ensure_ascii=False))
            return 1
        out = diff_payload(args.stock, args.start, args.end, args.min_pct)
    else:
        out = snapshot_payload(args.stock, args.as_of, args.top)

    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
