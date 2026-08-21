"""
futu_index_data.py — 恒指 + VHSI 歷史K線（OpenD 版）

用途：替代 gsmart-box 由 hsi.com.hk（VHSI）同 Yahoo（恒指價）攞嘅即時/歷史數據。
      HSI Short Strangle 嘅 VHSI 分級回測要靠呢條 VHSI 歷史。

輸出（append-only，去重）：
  /home/workspace/Desktop/db/Futu/Kline/kline_index.parquet
      columns: code, time_key, open, high, low, close, volume, turnover

code: HK.800000 = 恒生指數, HK.800125 = VHSI 恒指波幅指數
"""
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import futu
import pandas as pd

OUT_DIR = Path("/home/workspace/Desktop/db/Futu/Kline")
OUT_PATH = OUT_DIR / "kline_index.parquet"
HOST = "127.0.0.1"
PORT = 11111

INDEX_CODES = ["HK.800000", "HK.800125"]  # HSI, VHSI


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def connect():
    return futu.OpenQuoteContext(host=HOST, port=PORT)


def pull(ctx, codes=INDEX_CODES, days=365):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    existing = pd.read_parquet(OUT_PATH) if OUT_PATH.exists() else pd.DataFrame()

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    frames = []
    for code in codes:
        ret, df, _ = ctx.request_history_kline(
            code, start=start, end=end, ktype=futu.KLType.K_DAY, max_count=1000
        )
        if ret != futu.RET_OK or df is None or len(df) == 0:
            log(f"✗ {code}: ret={ret} {df}")
            continue
        df = df[["code", "time_key", "open", "high", "low", "close", "volume", "turnover"]].copy()
        df["time_key"] = pd.to_datetime(df["time_key"])
        frames.append(df)
        log(f"✓ {code}: {len(df)} 行 → {df['time_key'].max().date()}")

    if not frames:
        log("冇拉到任何指數K線")
        return existing

    fresh = pd.concat(frames, ignore_index=True)
    merged = pd.concat([existing, fresh], ignore_index=True).drop_duplicates(
        subset=["code", "time_key"], keep="last"
    ).sort_values(["code", "time_key"]).reset_index(drop=True)
    merged.to_parquet(OUT_PATH, index=False)
    log(f"✅ 合併後 {len(merged)} 行 → {OUT_PATH.name}")
    return merged


def run_import(days=365):
    try:
        ctx = connect()
        try:
            df = pull(ctx, INDEX_CODES, days)
            return {
                "status": "ok",
                "rows": int(len(df)),
                "codes": sorted(df["code"].unique().tolist()),
                "latest": str(df["time_key"].max().date()) if len(df) else None,
            }
        finally:
            ctx.close()
    except Exception as e:
        return {"status": "error", "error": str(e)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()
    res = run_import(days=args.days)
    print(res)
