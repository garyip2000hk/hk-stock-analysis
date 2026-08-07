#!/usr/bin/env python3
"""Validate reconstructed CCASS snapshots against CCASS's own published aggregates.

`dailylog` carries CCASS's authoritative `c5` / `c10` (shares held by the top 5
and top 10 intermediaries) and `intermed_cnt`. Those columns are independent of
our change-log replay, so they are a genuine ground truth to check against.

Run:  python3 validate_ccass.py            # random sample
      python3 validate_ccass.py 01241 ...  # specific codes
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import duckdb

import ccass_snapshot as cs

TOLERANCE = 0.5  # percentage points


def sample_codes(n: int) -> list[str]:
    latest = cs.market_latest_date()
    with duckdb.connect() as con:
        rows = con.execute(
            """
            SELECT DISTINCT s.stock_code
            FROM read_parquet(?) d
            JOIN read_parquet(?) s
              ON TRY_CAST(s.issue_id AS BIGINT) = d.issue_id
            WHERE d.at_date = CAST(? AS DATE)
              AND d.c5 > 0
              AND (s.to_date IS NULL OR s.to_date = '')
            """,
            [cs._existing(cs.DAILYLOG_SOURCES), str(cs.SHORTNAMES), latest],
        ).fetchall()
    codes = sorted({r[0] for r in rows if r[0]})
    random.seed(42)
    return random.sample(codes, min(n, len(codes)))


def check(code: str) -> dict:
    snap = cs.snapshot(code)
    if snap.get("error"):
        return {"code": code, "status": "error", "detail": snap["error"]}

    issue_id = snap["issue_id"]
    log = cs._dailylog(issue_id, snap["date"])
    base = snap["percentage_base"]
    if not log or not base or log["at_date"] != snap["date"]:
        return {"code": code, "status": "skip", "detail": "no same-day dailylog"}

    exp5 = round(log["c5"] * 100 / base, 2)
    exp10 = round(log["c10"] * 100 / base, 2)
    got5 = snap["concentration"]["top_5"]
    got10 = snap["concentration"]["top_10"]
    d5, d10 = round(got5 - exp5, 2), round(got10 - exp10, 2)
    ok = abs(d5) <= TOLERANCE and abs(d10) <= TOLERANCE
    return {
        "code": code, "status": "ok" if ok else "MISMATCH",
        "date": snap["date"], "src": snap["concentration_source"],
        "exp5": exp5, "got5": got5, "d5": d5,
        "exp10": exp10, "got10": got10, "d10": d10,
        "holders": snap["total_participants"],
        "ccass_holders": log["intermed_cnt"],
    }


def main() -> int:
    codes = sys.argv[1:] or sample_codes(40)
    results = [check(c) for c in codes]
    bad = [r for r in results if r["status"] == "MISMATCH"]
    skipped = [r for r in results if r["status"] in ("skip", "error")]

    for r in results:
        if r["status"] in ("skip", "error"):
            print(f"  {r['code']}  {r['status']}: {r['detail']}")
        else:
            flag = "OK " if r["status"] == "ok" else "!! "
            print(f"  {flag}{r['code']}  {r['date']}  "
                  f"top5 {r['got5']:>6}% (CCASS {r['exp5']:>6}%, Δ{r['d5']:+.2f})  "
                  f"top10 {r['got10']:>6}% (CCASS {r['exp10']:>6}%, Δ{r['d10']:+.2f})  "
                  f"[{r['src']}]")

    checked = len(results) - len(skipped)
    print(f"\n{checked - len(bad)}/{checked} match CCASS published aggregates "
          f"(±{TOLERANCE}pp); {len(skipped)} skipped")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
