#!/usr/bin/env python3
"""
sync_quotes_cache.py — 重建 imported/quotes.json，對齊 backend 最新交易日

兩個資料來源合併：
  1. Desktop/db/quotes/*.htm  — HKEX Daily Quotations（主板 d*e.htm + GEM e_G*.htm
     + 2026_web/ 子目錄），有股票名稱、開高低收、成交量／額
  2. Desktop/db/CCASS/quotes.parquet — backend 每日報價（issue_id → 用
     shortnames.parquet 映射成股票代號），覆蓋 HTM 未落地嘅最近日子

舊版 load_quotes() 嘅正則 `^\\s*(\\d{5})\\s+(.*?)\\s+([\\d.,]+)\\s*$` 只會匹配到
「市場摘要」段落嘅權證行，所以每日只得 10 個假代號。呢個版本先切出
<a name="quotations"> → <a name="sales_all"> 之間嘅報價段，再按 HKEX 兩行式
排版（第一行 prev_close/ask/high/vol、第二行 close/bid/low/turnover）解析。

輸出結構（compact，唔用 indent）：
{
  "meta":   {"generated","date_min","date_max","days","codes","sources"},
  "names":  {"00700": "TENCENT", ...},
  "quotes": {"2026-08-05": {"00700": {"close","high","low","vol","turnover"}}}
}
"""

from __future__ import annotations

import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
IMPORTED = BASE / "imported"
DATA_ROOT = Path("/home/workspace/Desktop/db")
QUOTES_DIR = DATA_ROOT / "quotes"
CCASS_DIR = DATA_ROOT / "CCASS"
OUT = IMPORTED / "quotes.json"

MAX_EQUITY_CODE = 9999

ROW1 = re.compile(
    r"^[\*#\s]{0,3}\s*(\d{1,5})\s+(\S.{0,22}?)\s{2,}([A-Z]{3})"
    r"\s+([\d,.]+|-)\s+([\d,.]+|-)\s+([\d,.]+|-)\s+([\d,]+|-)\s*$"
)
ROW2 = re.compile(r"^\s+([\d,.]+|-)\s+([\d,.]+|-)\s+([\d,.]+|-)\s+([\d,]+|-)\s*$")
TAG = re.compile(r"<[^>]+>")
DATE_HDR = re.compile(r"DATE:\s*(\d{1,2})\s+([A-Z]{3})\s+(\d{4})")

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def _num(s: str):
    s = s.replace(",", "").strip()
    if s in ("-", ""):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def file_date(raw: str) -> str | None:
    """由檔案內文 `DATE: 31 JUL 2026` 抽日期（比檔名可靠）。"""
    m = DATE_HDR.search(raw[:20000])
    if not m:
        return None
    mon = MONTHS.get(m.group(2))
    if not mon:
        return None
    return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"


def parse_htm(path: Path) -> tuple[str | None, dict]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    dt = file_date(raw)
    if not dt:
        return None, {}

    start = raw.find('name = "quotations"')
    if start < 0:
        start = raw.find('name="quotations"')
    if start < 0:
        return dt, {}
    end = raw.find('name = "sales_all"', start)
    if end < 0:
        end = raw.find('name="sales_all"', start)
    seg = raw[start: end if end > start else len(raw)]
    text = html.unescape(TAG.sub("", seg))

    lines = text.split("\n")
    out: dict = {}
    i = 0
    n = len(lines)
    while i < n - 1:
        m1 = ROW1.match(lines[i].rstrip())
        if m1:
            m2 = ROW2.match(lines[i + 1].rstrip())
            if m2:
                code_i = int(m1.group(1))
                if code_i <= MAX_EQUITY_CODE:
                    close = _num(m2.group(1))
                    if close is not None:
                        out[f"{code_i:05d}"] = {
                            "name": m1.group(2).strip(),
                            "close": close,
                            "high": _num(m1.group(6)),
                            "low": _num(m2.group(3)),
                            "vol": _num(m1.group(7)),
                            "turnover": _num(m2.group(4)),
                        }
                i += 2
                continue
        i += 1
    return dt, out


def collect_htm() -> tuple[dict, dict, int]:
    files: list[Path] = []
    if QUOTES_DIR.exists():
        files.extend(sorted(QUOTES_DIR.glob("*.htm")))
        for sub in sorted(QUOTES_DIR.glob("*_web")):
            if sub.is_dir():
                files.extend(sorted(sub.glob("*.htm")))

    quotes: dict = {}
    names: dict = {}
    ok = 0
    for f in files:
        try:
            if f.stat().st_size < 20000:
                continue
            dt, rows = parse_htm(f)
        except Exception as exc:
            print(f"  [skip] {f.name}: {exc}", file=sys.stderr)
            continue
        if not dt or not rows:
            continue
        day = quotes.setdefault(dt, {})
        for code, rec in rows.items():
            names[code] = rec.pop("name")
            day[code] = rec
        ok += 1
        if ok % 50 == 0:
            print(f"  ... {ok} HTM 檔已解析（最新 {dt}）")
    return quotes, names, ok


def collect_parquet(existing_dates: set[str]) -> tuple[dict, dict, int]:
    qp = CCASS_DIR / "quotes.parquet"
    sp = CCASS_DIR / "shortnames.parquet"
    if not qp.exists() or not sp.exists():
        return {}, {}, 0
    try:
        import duckdb
    except ImportError:
        print("  [warn] duckdb 未安裝，跳過 parquet 來源", file=sys.stderr)
        return {}, {}, 0

    con = duckdb.connect()
    rows = con.execute(
        f"""
        WITH sn AS (
          SELECT TRY_CAST(issue_id AS BIGINT) AS iid,
                 LPAD(LTRIM(stock_code, '0'), 5, '0') AS code,
                 short_name,
                 ROW_NUMBER() OVER (
                   PARTITION BY TRY_CAST(issue_id AS BIGINT)
                   ORDER BY parallel ASC, use_date DESC NULLS LAST
                 ) AS rn
          FROM read_parquet('{sp}')
          WHERE stock_code IS NOT NULL
            AND TRY_CAST(issue_id AS BIGINT) IS NOT NULL
        )
        SELECT CAST(q.at_date AS VARCHAR), sn.code, sn.short_name,
               q.closing, q.high, q.low, q.vol, q.turn
        FROM read_parquet('{qp}') q
        JOIN sn ON sn.iid = q.issue_id AND sn.rn = 1
        WHERE q.susp = FALSE AND q.noclose = FALSE AND q.closing > 0
          AND TRY_CAST(LTRIM(sn.code, '0') AS BIGINT) <= {MAX_EQUITY_CODE}
        ORDER BY q.at_date
        """
    ).fetchall()
    con.close()

    quotes: dict = {}
    names: dict = {}
    for dt, code, short_name, close, high, low, vol, turn in rows:
        if dt in existing_dates:
            continue
        rec = {
            "close": round(float(close), 4),
            "high": round(float(high), 4) if high else None,
            "low": round(float(low), 4) if low else None,
            "vol": int(vol) if vol else None,
            "turnover": int(turn) if turn else None,
        }
        quotes.setdefault(dt, {})[code] = rec
        if short_name:
            names.setdefault(code, short_name)
    return quotes, names, len(quotes)


def main() -> int:
    IMPORTED.mkdir(parents=True, exist_ok=True)
    print("[1/3] 解析 HKEX Daily Quotations HTM ...")
    q_htm, n_htm, cnt_htm = collect_htm()
    print(f"      {cnt_htm} 個檔案 → {len(q_htm)} 個交易日")

    print("[2/3] 讀 backend quotes.parquet 補最近日子 ...")
    q_pq, n_pq, cnt_pq = collect_parquet(set(q_htm))
    print(f"      補上 {cnt_pq} 個交易日")

    quotes = {**q_htm, **q_pq}
    names = {**n_pq, **n_htm}
    if not quotes:
        print("冇任何報價資料，中止", file=sys.stderr)
        return 1

    dates = sorted(quotes)
    payload = {
        "meta": {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "date_min": dates[0],
            "date_max": dates[-1],
            "days": len(dates),
            "codes": len(names),
            "sources": {
                "hkex_daily_quotations_htm": cnt_htm,
                "ccass_quotes_parquet_days": cnt_pq,
            },
        },
        "names": dict(sorted(names.items())),
        "quotes": {d: quotes[d] for d in dates},
    }

    print("[3/3] 寫入 quotes.json ...")
    tmp = OUT.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(OUT)
    size_mb = OUT.stat().st_size / 1e6
    print(f"      {dates[0]} → {dates[-1]}（{len(dates)} 日, "
          f"{len(names)} 隻股票, {size_mb:.1f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
