"""
Futu OpenD 數據匯入 — 兩路並行
  1. Market Snapshot：全港股即時快照（無配額限制）
  2. History K-Line：期權標的日K（受 300 隻/30日 滾動配額限制）

輸出：
  /home/workspace/Desktop/db/Futu/Snapshot/snapshot_YYYYMMDD.parquet
  /home/workspace/Desktop/db/Futu/Kline/kline_day.parquet   (append-only, 去重)
"""
import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import futu
import pandas as pd

OUT_DIR = Path("/home/workspace/Desktop/db/Futu")
SNAP_DIR = OUT_DIR / "Snapshot"
KLINE_DIR = OUT_DIR / "Kline"
SPEC_PATH = Path("/home/workspace/stock-analysis/options_data/contract_specs.json")
KLINE_PATH = KLINE_DIR / "kline_day.parquet"

HOST = "127.0.0.1"
PORT = 11111


HKT = timezone(timedelta(hours=8))


def now_hkt():
    return datetime.now(HKT)


def log(msg):
    print(f"[{now_hkt():%H:%M:%S}] {msg}", flush=True)


def connect():
    return futu.OpenQuoteContext(host=HOST, port=PORT)


def fetch_snapshot(ctx, codes=None):
    """默認只掃期權標的。傳 codes=[] 或設 all_market=True 才掃全市場。"""
    if codes is None:
        codes = options_underlyings()

    if not codes:
        ret, df_basic = ctx.get_stock_basicinfo(futu.Market.HK, futu.SecurityType.STOCK)
        if ret != futu.RET_OK:
            log(f"✗ basicinfo 失敗: {df_basic}")
            return None
        codes = df_basic.loc[df_basic["delisting"] == False, "code"].tolist()
        log(f"快照目標: {len(codes)} 隻港股（全市場）")
    else:
        log(f"快照目標: {len(codes)} 隻期權標的")

    frames, chunk = [], 400
    for i in range(0, len(codes), chunk):
        ret, df = ctx.get_market_snapshot(codes[i : i + chunk])
        if ret == futu.RET_OK:
            frames.append(df)
        else:
            log(f"  ✗ chunk {i}: {df}")
        time.sleep(0.5)

    if not frames:
        return None

    df_all = pd.concat(frames, ignore_index=True)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    out = SNAP_DIR / f"snapshot_{now_hkt():%Y%m%d}.parquet"
    df_all.to_parquet(out, index=False)
    log(f"✅ 快照: {len(df_all)} 行 → {out.name}")
    return df_all


def options_underlyings():
    if not SPEC_PATH.exists():
        return []
    specs = json.loads(SPEC_PATH.read_text())
    keys = specs if isinstance(specs, list) else list(specs)
    return [f"HK.{k}" if not str(k).startswith("HK.") else str(k) for k in keys]


def fetch_kline(ctx, codes, days=365, refresh_days=10):
    ret, quota = ctx.get_history_kl_quota(get_detail=False)
    if ret == futu.RET_OK:
        used, remain, _ = quota
        log(f"K線配額: 已用 {used} / 剩 {remain}")
    else:
        remain = 300

    existing = pd.read_parquet(KLINE_PATH) if KLINE_PATH.exists() else pd.DataFrame()
    done = set(existing["code"].unique()) if len(existing) else set()

    new_codes = [c for c in codes if c not in done][: max(0, int(remain))]
    refresh_codes = [c for c in codes if c in done]

    end = datetime.now().strftime("%Y-%m-%d")
    full_start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    inc_start = (datetime.now() - timedelta(days=refresh_days)).strftime("%Y-%m-%d")

    jobs = [(c, full_start, "新") for c in new_codes]
    jobs += [(c, inc_start, "增量") for c in refresh_codes]

    if not jobs:
        log("K線: 冇股票要拉")
        return existing

    log(f"K線: 新股 {len(new_codes)} 隻（{full_start}→{end}）, 增量 {len(refresh_codes)} 隻（{inc_start}→{end}）")

    frames = []
    for n, (code, start, kind) in enumerate(jobs, 1):
        ret, df, _ = ctx.request_history_kline(
            code, start=start, end=end, ktype=futu.KLType.K_DAY, max_count=1000
        )
        if ret == futu.RET_OK and len(df):
            frames.append(df)
            if n % 20 == 0:
                log(f"  {n}/{len(jobs)} …")
        else:
            log(f"  ✗ {code} ({kind}): {df}")
        time.sleep(0.6)

    if not frames:
        return existing

    df_new = pd.concat(frames, ignore_index=True)
    combined = pd.concat([existing, df_new], ignore_index=True) if len(existing) else df_new
    combined = combined.drop_duplicates(subset=["code", "time_key"], keep="last")
    combined = combined.sort_values(["code", "time_key"]).reset_index(drop=True)

    KLINE_DIR.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(KLINE_PATH, index=False)
    log(f"✅ K線: 新增 {len(df_new)} 行，合共 {len(combined)} 行 / {combined['code'].nunique()} 隻")
    return combined


def run_import(days=365, snapshot=True, kline=True, all_market=False):
    """供 daily_pipeline 呼叫。回傳 summary dict，OpenD 未開會回 status=skipped。
    默認只做期權標的；all_market=True 才掃全港股快照。"""
    summary = {"status": "ok", "snapshot": None, "kline": None}
    try:
        ctx = connect()
    except Exception as e:
        return {"status": "skipped", "error": f"OpenD 連唔到: {e}"}
    try:
        if snapshot:
            df = fetch_snapshot(ctx, codes=[] if all_market else None)
            summary["snapshot"] = {
                "rows": int(len(df)),
                "scope": "all_market" if all_market else "options_underlyings",
            } if df is not None else {"rows": 0}
        if kline:
            df = fetch_kline(ctx, options_underlyings(), days=days)
            if df is not None and len(df):
                summary["kline"] = {
                    "rows": int(len(df)),
                    "codes": int(df["code"].nunique()),
                    "latest": str(df["time_key"].max())[:10],
                }
    except Exception as e:
        summary["status"] = "error"
        summary["error"] = str(e)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot-only", action="store_true")
    ap.add_argument("--kline-only", action="store_true")
    ap.add_argument("--all-market", action="store_true", help="快照掃全港股（默認只掃期權標的）")
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()

    ctx = connect()
    try:
        if not args.kline_only:
            fetch_snapshot(ctx, codes=[] if args.all_market else None)
        if not args.snapshot_only:
            fetch_kline(ctx, options_underlyings(), days=args.days)
    finally:
        ctx.close()
    log("=== 完成 ===")


if __name__ == "__main__":
    main()
