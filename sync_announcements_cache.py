#!/usr/bin/env python3
"""
sync_announcements_cache.py — 補齊 imported/announcements.json 到最新交易日

本地 Desktop/db/DoD/ 只落地到 2026-05-22（Windows scraper 停過），所以
2026-05-23 之後嘅公告要直接問 HKEXnews titleSearchServlet 拎。

寫入時保持原有 schema（stock_code / date / company / title / htm_file /
pdf_dir / pdf_files），另外加：
  source     — "dod_local" 或 "hkexnews"
  news_id    — HKEXnews NEWS_ID（去重用）
  file_link  — 公告 PDF 絕對網址
  doc_type   — HKEXnews LONG_TEXT（公告分類）

用法：
  python3 sync_announcements_cache.py              # 由 cache 最後日期補到今日
  python3 sync_announcements_cache.py --from 2026-05-23 --to 2026-08-06
"""

from __future__ import annotations

import argparse
import html as htmlmod
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).parent
IMPORTED = BASE / "imported"
OUT = IMPORTED / "announcements.json"

ENDPOINT = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
BASE_URL = "https://www1.hkexnews.hk"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml",
    "X-Requested-With": "XMLHttpRequest",
}


def clean(s: str) -> str:
    s = htmlmod.unescape(s or "")
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_day(session: requests.Session, day: date, market: str = "SEHK") -> list[dict]:
    ymd = day.strftime("%Y%m%d")
    params = {
        "sortDir": "0", "sortByOptions": "DateTime",
        "category": "0", "market": market,
        "stockId": "-1", "documentType": "-1",
        "fromDate": ymd, "toDate": ymd, "title": "",
        "searchType": "1", "t1code": "-2",
        "t2Gcode": "-2", "t2code": "-2",
        "rowRange": "5000", "lang": "ZH",
    }
    for attempt in range(4):
        try:
            resp = session.get(ENDPOINT, params=params, timeout=60)
            resp.raise_for_status()
            payload = resp.json()
            raw = payload.get("result", "[]")
            rows = json.loads(raw) if isinstance(raw, str) else raw
            return rows or []
        except Exception as exc:
            if attempt == 3:
                print(f"  [warn] {ymd} 抓唔到：{exc}", file=sys.stderr)
                return []
            time.sleep(2 * (attempt + 1))
    return []


def to_record(row: dict) -> dict | None:
    code = (row.get("STOCK_CODE") or "").strip()
    dt_raw = (row.get("DATE_TIME") or "").strip()
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", dt_raw)
    if not code or not m:
        return None
    day = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    link = (row.get("FILE_LINK") or "").strip()
    return {
        "stock_code": code.zfill(5),
        "date": day,
        "company": clean(row.get("STOCK_NAME")),
        "title": clean(row.get("TITLE")),
        "htm_file": None,
        "pdf_dir": None,
        "pdf_files": [],
        "source": "hkexnews",
        "news_id": (row.get("NEWS_ID") or "").strip() or None,
        "file_link": (BASE_URL + link) if link.startswith("/") else (link or None),
        "doc_type": clean(row.get("LONG_TEXT")),
        "datetime": dt_raw,
    }


def load_existing() -> list[dict]:
    if not OUT.exists():
        return []
    with open(OUT, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("announcements", [])
    for rec in data:
        rec.setdefault("source", "dod_local")
        rec.setdefault("news_id", None)
        rec.setdefault("file_link", None)
        rec.setdefault("doc_type", "")
    return data


def dedupe_key(rec: dict):
    if rec.get("news_id"):
        return ("nid", rec["news_id"])
    if rec.get("htm_file"):
        return ("htm", rec["htm_file"])
    return ("kv", rec["stock_code"], rec["date"], rec["title"][:120])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default=None)
    ap.add_argument("--to", dest="end", default=None)
    args = ap.parse_args()

    existing = load_existing()
    have = {dedupe_key(r) for r in existing}
    local_max = max((r["date"] for r in existing), default="2023-01-01")

    start = (datetime.strptime(args.start, "%Y-%m-%d").date() if args.start
             else datetime.strptime(local_max, "%Y-%m-%d").date() + timedelta(days=1))
    end = (datetime.strptime(args.end, "%Y-%m-%d").date() if args.end
           else datetime.now(timezone(timedelta(hours=8))).date())

    print(f"[announcements] cache 現有 {len(existing)} 條，最新 {local_max}")
    if start > end:
        print("      已經係最新，冇需要補")
        return
    print(f"[announcements] 由 HKEXnews 補 {start} → {end}")

    session = requests.Session()
    session.headers.update(HEADERS)

    added = 0
    day = start
    while day <= end:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        rows = fetch_day(session, day)
        new_today = 0
        for row in rows:
            rec = to_record(row)
            if not rec:
                continue
            key = dedupe_key(rec)
            if key in have:
                continue
            have.add(key)
            existing.append(rec)
            new_today += 1
        added += new_today
        print(f"  {day}  抓 {len(rows):>5} 條，新增 {new_today:>5}")
        time.sleep(0.6)
        day += timedelta(days=1)

    existing.sort(key=lambda x: (x["date"], x["stock_code"]))
    tmp = OUT.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(OUT)

    dates = [r["date"] for r in existing]
    size_mb = OUT.stat().st_size / 1024 / 1024
    print(f"[announcements] 新增 {added} 條 → 共 {len(existing)} 條 "
          f"({min(dates)} → {max(dates)}, {size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
