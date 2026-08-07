"""HKEX Stock Options Daily Market Report scraper.

Source: https://www.hkex.com.hk/eng/stat/dmstat/dayrpt/dqeYYMMDD.htm
HKEX 只保留約近一年嘅每日報告，所以呢個 scraper 係 append-only：
每日抓一次，寫入 options_data/iv_history.parquet，歷史就永久屬於我哋自己。

Class summary 每行 = 一隻期權標的當日：
  HKATS code / 名稱 / 股票代號 / 總成交 / Call 成交 / Put 成交 /
  總未平倉 / Call OI / Put OI / ATM IV%

CLI:
    python3 options_scraper.py                    # 抓最近交易日
    python3 options_scraper.py --date 2026-08-06
    python3 options_scraper.py --backfill 250     # 由今日倒數 250 個日曆日補抓
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

BASE = "https://www.hkex.com.hk/eng/stat/dmstat/dayrpt/dqe{ymd}.htm"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate",
}

DATA_DIR = Path(__file__).parent / "options_data"
HISTORY = DATA_DIR / "iv_history.parquet"
RAW_DIR = DATA_DIR / "raw"

CLASS_RE = re.compile(
    r"^([A-Z0-9]{3})\s+(.+?)\s+\((\d{5})\)\s+"
    r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+"
    r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+"
    r"(\d+|N/A)\s*$"
)
CLOSE_RE = re.compile(
    r"^CLASS\s+([A-Z0-9]{3})\s+-\s+.*?CLOSING PRICE HK\$\s*([\d,.]+)", re.M
)
DATE_RE = re.compile(r"STOCK OPTIONS DAILY MARKET REPORT AS AT\s+(\d{2} \w{3} \d{4})")


def _num(s: str) -> float | None:
    s = s.replace(",", "").strip()
    if not s or s in {"N/A", "-"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def strip_html(html: str) -> str:
    html = re.sub(r"<[^>]+>", "", html)
    return html.replace("&nbsp;", " ").replace("&amp;", "&")


def fetch(d: date, use_cache: bool = True) -> str | None:
    """Download one day's report. Returns plain text, or None if 404."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache = RAW_DIR / f"dqe{d:%y%m%d}.txt.gz"
    if use_cache and cache.exists():
        with gzip.open(cache, "rt", encoding="utf-8") as fh:
            return fh.read()

    url = BASE.format(ymd=f"{d:%y%m%d}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
    except requests.RequestException:
        return None
    if r.status_code != 200 or "STOCK OPTIONS DAILY MARKET REPORT" not in r.text:
        return None

    txt = strip_html(r.text)
    with gzip.open(cache, "wt", encoding="utf-8") as fh:
        fh.write(txt)
    return txt


def parse(txt: str) -> pd.DataFrame:
    """Parse a report's class-summary table into a DataFrame."""
    m = DATE_RE.search(txt)
    if not m:
        return pd.DataFrame()
    rpt_date = datetime.strptime(m.group(1), "%d %b %Y").date()

    closes = {c: _num(p) for c, p in CLOSE_RE.findall(txt)}

    rows = []
    for line in txt.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("TOTAL"):
            continue
        cm = CLASS_RE.match(line.strip())
        if not cm:
            continue
        code, name, stock, vol, calls, puts, oi, coi, poi, iv = cm.groups()
        rows.append(
            {
                "date": rpt_date,
                "hkats": code,
                "stock_code": stock,
                "name": name.strip(),
                "close": closes.get(code),
                "iv": _num(iv),
                "volume": _num(vol),
                "call_vol": _num(calls),
                "put_vol": _num(puts),
                "oi": _num(oi),
                "call_oi": _num(coi),
                "put_oi": _num(poi),
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # 同一 HKATS code 可能因調整合約重複（例 WHD / WHG 同指 00288）→ 保留成交最多嘅
    df = df.sort_values("volume", ascending=False).drop_duplicates(
        subset=["date", "hkats"], keep="first"
    )
    df["pcr_vol"] = (df.put_vol / df.call_vol.replace(0, float("nan"))).astype(float).round(3)
    df["pcr_oi"] = (df.put_oi / df.call_oi.replace(0, float("nan"))).astype(float).round(3)
    return df.sort_values("hkats").reset_index(drop=True)


def load_history() -> pd.DataFrame:
    if HISTORY.exists():
        return pd.read_parquet(HISTORY)
    return pd.DataFrame()


def save_history(df: pd.DataFrame) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df = df.sort_values(["date", "hkats"]).reset_index(drop=True)
    df.to_parquet(HISTORY, index=False)


def ingest(days: list[date], verbose: bool = True) -> pd.DataFrame:
    """Fetch + parse the given dates and merge into iv_history.parquet."""
    hist = load_history()
    have = set()
    if not hist.empty:
        have = {pd.Timestamp(d).date() for d in hist["date"].unique()}

    new_frames = []
    for d in days:
        if d in have:
            continue
        txt = fetch(d)
        if txt is None:
            if verbose:
                print(f"  {d}  — 無報告（假期／未出）")
            continue
        df = parse(txt)
        if df.empty:
            if verbose:
                print(f"  {d}  — 解析唔到")
            continue
        new_frames.append(df)
        if verbose:
            ivs = df.iv.dropna()
            print(
                f"  {d}  {len(df):3d} 隻   IV 中位數 {ivs.median():.0f}%   "
                f"總成交 {df.volume.sum():,.0f}"
            )
        time.sleep(0.4)

    if not new_frames:
        if verbose:
            print("冇新數據。")
        return hist

    merged = pd.concat([hist] + new_frames, ignore_index=True) if not hist.empty else pd.concat(new_frames, ignore_index=True)
    merged["date"] = pd.to_datetime(merged["date"]).dt.date
    merged = merged.drop_duplicates(subset=["date", "hkats"], keep="last")
    save_history(merged)
    if verbose:
        print(
            f"\n已儲存 {HISTORY}  →  {merged.date.nunique()} 個交易日 / "
            f"{merged.stock_code.nunique()} 隻標的 / {len(merged):,} 行"
        )
    return merged


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD 單日抓取")
    ap.add_argument("--backfill", type=int, help="由今日倒數 N 個日曆日")
    args = ap.parse_args()

    today = date.today()
    if args.date:
        days = [datetime.strptime(args.date, "%Y-%m-%d").date()]
    elif args.backfill:
        days = [today - timedelta(days=i) for i in range(args.backfill, -1, -1)]
        days = [d for d in days if d.weekday() < 5]
    else:
        days = [today - timedelta(days=i) for i in range(4, -1, -1)]
        days = [d for d in days if d.weekday() < 5]

    print(f"抓取 {len(days)} 日 HKEX 股票期權報告…")
    ingest(days)


if __name__ == "__main__":
    main()
