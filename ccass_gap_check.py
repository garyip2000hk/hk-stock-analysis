#!/usr/bin/env python3
"""Check CCASS local coverage against the real HK trading calendar.

Answers one question: 有冇交易日嘅 CCASS 數據冇上到 Zo。
Trading calendar comes from Futu OpenD (真日曆，自動處理公眾假期)；
OpenD 唔通時 fallback 去「星期一至五」近似。

Exit code 0 = no gap, 1 = gap found.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

CCASS = Path(__file__).resolve().parent.parent / "Desktop" / "db" / "CCASS"
INCOMING = Path("/home/workspace/incoming/ccass")


def local_dates(table: str = "dailylog") -> set[str]:
    import pandas as pd

    out: set[str] = set()
    for p in [CCASS / f"{table}.parquet", CCASS / "incremental" / f"{table}_20251225_20260522.parquet"]:
        if not p.exists():
            continue
        d = pd.read_parquet(p, columns=["at_date"])
        out |= set(pd.to_datetime(d["at_date"]).dt.date.astype(str))
    return out


def trading_days(start: str, end: str) -> tuple[list[str], str]:
    try:
        from futu import OpenQuoteContext, TradeDateMarket

        q = OpenQuoteContext(host="127.0.0.1", port=11111)
        try:
            ret, data = q.request_trading_days(market=TradeDateMarket.HK, start=start, end=end)
        finally:
            q.close()
        if ret == 0:
            return [x["time"] for x in data], "futu"
    except Exception:
        pass
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    days = []
    while d0 <= d1:
        if d0.weekday() < 5:
            days.append(d0.isoformat())
        d0 += timedelta(days=1)
    return days, "weekday-approx"


def pending_uploads() -> list[str]:
    if not INCOMING.exists():
        return []
    state = INCOMING / ".import_state.json"
    imported_at = None
    if state.exists():
        try:
            imported_at = json.loads(state.read_text()).get("imported_at")
        except Exception:
            pass
    files = sorted(p.name for p in INCOMING.glob("ccass_*.csv"))
    return files[-6:] if not imported_at else []


def check(days: int = 45) -> dict:
    end = date.today()
    start = end - timedelta(days=days)
    cal, cal_source = trading_days(start.isoformat(), end.isoformat())
    have = local_dates()

    # 今日嘅 CCASS 要收市後才公佈，明日才上載 —— 唔算缺口
    expected = [d for d in cal if d < end.isoformat()]
    missing = [d for d in expected if d not in have]
    latest_local = max(have) if have else None

    result = {
        "checked_range": [start.isoformat(), end.isoformat()],
        "calendar_source": cal_source,
        "trading_days_expected": len(expected),
        "trading_days_present": len([d for d in expected if d in have]),
        "latest_local_ccass": latest_local,
        "missing_trading_days": missing,
        "status": "GAP" if missing else "OK",
    }
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45, help="回望幾多個日曆日")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = check(args.days)
    start, end = result["checked_range"]
    cal_source = result["calendar_source"]
    latest_local = result["latest_local_ccass"]
    missing = result["missing_trading_days"]
    expected = [None] * result["trading_days_expected"]

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"CCASS 覆蓋檢查 ({start} → {end}, 日曆來源: {cal_source})")
        print(f"  最新本地 CCASS 日期: {latest_local}")
        print(f"  應有交易日: {len(expected)}   已有: {result['trading_days_present']}")
        if missing:
            print(f"  ⚠️  缺少 {len(missing)} 個交易日: {', '.join(missing)}")
        else:
            print("  ✅ 無缺口")

    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
