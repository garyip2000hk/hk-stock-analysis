"""chain_history.py — 由 HKEX raw 日報建立逐個行使價嘅期權鏈歷史快取。

`options_chain.parse_chains()` 每次要解一份 8MB 報告（~2 秒）。
要做真期權回測就要反覆讀 212 個交易日 × 每日 7 萬行合約 —
逐次解析太慢，所以呢個模組一次過解全部，只留**有流動性**嘅合約
（成交 > 0 或未平倉 > 0，約 50%），存成 partition parquet。

有咗呢個快取，任何期權策略回測都可以直接由 parquet 讀真實結算價，
唔需要再碰 raw 報告。

用法：
    python3 chain_history.py --build          # 全量重建（首次，~10 分鐘）
    python3 chain_history.py --update         # 只補未入快取嘅新日子
    python3 chain_history.py --stats          # 睇覆蓋
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import options_chain as oc

BASE = Path(__file__).parent
RAW_DIR = BASE / "options_data" / "raw"
CACHE = BASE / "options_data" / "chain_history.parquet"

KEEP_COLS = [
    "date", "stock_code", "hkats", "close", "expiry", "strike", "type",
    "settle", "iv", "volume", "oi", "oi_chg", "dte",
]


def raw_dates() -> list[date]:
    return [
        datetime.strptime(f.name[3:9], "%y%m%d").date()
        for f in sorted(RAW_DIR.glob("dqe*.txt.gz"))
    ]


def cached_dates() -> set[date]:
    if not CACHE.exists():
        return set()
    d = pd.read_parquet(CACHE, columns=["date"])
    return set(pd.to_datetime(d["date"]).dt.date.unique())


def _parse_day(d: date) -> pd.DataFrame:
    df = oc.parse_chains(d)
    if df.empty:
        return df
    # 只留有流動性嘅合約：冇成交又冇未平倉嘅行使價對回測冇用
    live = (df["oi"].fillna(0) > 0) | (df["volume"].fillna(0) > 0)
    df = df[live]
    if df.empty:
        return df
    df = df[[c for c in KEEP_COLS if c in df.columns]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["expiry"] = pd.to_datetime(df["expiry"])
    for c in ("close", "strike", "settle", "iv", "volume", "oi", "oi_chg"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
    df["dte"] = pd.to_numeric(df["dte"], errors="coerce").astype("int16")
    df["stock_code"] = df["stock_code"].astype("string")
    df["hkats"] = df["hkats"].astype("string")
    df["type"] = df["type"].astype("category")
    return df


def build(update_only: bool = False, verbose: bool = True) -> pd.DataFrame:
    todo = raw_dates()
    if update_only:
        have = cached_dates()
        todo = [d for d in todo if d not in have]
    if not todo:
        if verbose:
            print("快取已經最新，冇新日子要補。")
        return pd.read_parquet(CACHE) if CACHE.exists() else pd.DataFrame()

    frames: list[pd.DataFrame] = []
    for i, d in enumerate(todo, 1):
        df = _parse_day(d)
        if not df.empty:
            frames.append(df)
        if verbose and (i % 20 == 0 or i == len(todo)):
            rows = sum(len(f) for f in frames)
            print(f"  [{i}/{len(todo)}] {d}  累計 {rows:,} 行")

    if not frames:
        print("冇解到任何合約。")
        return pd.DataFrame()

    new = pd.concat(frames, ignore_index=True)
    if update_only and CACHE.exists():
        old = pd.read_parquet(CACHE)
        new = pd.concat([old, new], ignore_index=True)
    new = (new.drop_duplicates(subset=["date", "stock_code", "expiry", "strike", "type"])
              .sort_values(["date", "stock_code", "expiry", "strike", "type"])
              .reset_index(drop=True))
    new.to_parquet(CACHE, index=False, compression="zstd")
    if verbose:
        print(f"\n✅ {CACHE}")
        print(f"   {len(new):,} 行 · {new.stock_code.nunique()} 隻標的 · "
              f"{new.date.nunique()} 個交易日")
    return new


def load(codes: list[str] | None = None,
         start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """讀快取。codes / 日期範圍可選，用 parquet filter 避免載入全部。"""
    if not CACHE.exists():
        raise FileNotFoundError(f"{CACHE} 未建立，先跑 python3 chain_history.py --build")
    filters = []
    if codes:
        filters.append(("stock_code", "in", [c.zfill(5) for c in codes]))
    if start:
        filters.append(("date", ">=", pd.Timestamp(start)))
    if end:
        filters.append(("date", "<=", pd.Timestamp(end)))
    return pd.read_parquet(CACHE, filters=filters or None)


def stats() -> None:
    if not CACHE.exists():
        print("快取未建立。")
        return
    df = pd.read_parquet(CACHE, columns=["date", "stock_code", "type", "oi"])
    print(f"檔案      {CACHE}  ({CACHE.stat().st_size / 1e6:.0f} MB)")
    print(f"行數      {len(df):,}")
    print(f"標的      {df.stock_code.nunique()}")
    print(f"交易日    {df.date.nunique()}  "
          f"({df.date.min().date()} → {df.date.max().date()})")
    per = df.groupby("stock_code").size().sort_values(ascending=False)
    print(f"\n合約行數最多嘅 10 隻：")
    for code, n in per.head(10).items():
        print(f"  {code}  {n:>9,}")


def main() -> None:
    ap = argparse.ArgumentParser(description="期權鏈歷史快取")
    ap.add_argument("--build", action="store_true", help="全量重建")
    ap.add_argument("--update", action="store_true", help="只補新日子")
    ap.add_argument("--stats", action="store_true", help="睇覆蓋")
    a = ap.parse_args()

    if a.stats:
        stats()
    elif a.build:
        build(update_only=False)
    elif a.update:
        build(update_only=True)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
