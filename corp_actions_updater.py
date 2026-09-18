#!/usr/bin/env python3
"""
財技動作 cache 自動更新器（2026-09-18）

背景：`corp_actions_cache.json`（全市場最新財技動作 feed）以前係人手／一次性
curate，2026-07-29 之後再冇人更新，前端「財技動作」tab 長期停在 570 條舊事件。
本模組改以本地 `imported/announcements.json`（daily_pipeline 每日同步、帶 HKEXnews
官方 doc_type 分類）做來源，每日增量 append 入 cache，冪等（dedup by stock+date+type）。

規則：
- 只收「公告及通告／上市文件／《收購守則》」類別；排除翌日披露報表／月報表／
  通函（除非通函本身係供股／收購／可轉換／私有化）／債券結構性產品等噪音。
- 合併守則「交易披露」（經紀每日披露）太噪，不收；《收購守則》公告先算要約事件。
- 增量窗口 = cache 現有最遲事件日 − 3 日 → 今日（重掃 3 日防同日遲到公告，dedup 擋重複）。

CLI: python3 corp_actions_updater.py [--dry-run]
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "corp_actions_cache.json")
ANN_FILE = os.path.join(HERE, "imported", "announcements.json")

EXCLUDE_PREFIX = (
    "翌日披露報表", "月報表", "委任代表表格", "憲章文件",
    "債券及結構性產品", "交易所買賣基金", "槓桿及反向",
)

RULES = [
    ("供股", ["供股", "公開發售", "OPEN OFFER", "發售以供認購"]),
    ("配售", ["配售", "先舊後新", "TOP-TOPLING", "TOP-TOPLACING", "根據一般性授權發行股份"]),
    ("全購", ["收購守則", "要約", "全購"]),
    ("可換股債券", ["可轉換", "可換股"]),
    ("私有化", ["私有化"]),
    ("合股", ["股份合併", "合股", "CONSOLIDATION"]),
    ("拆股", ["拆細", "拆股", "SPLIT"]),
    ("送紅股", ["紅股", "BONUS"]),
]


def norm_date(d):
    d = (d or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return d
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", d)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"^(\d{4})/(\d{2})/(\d{2})$", d)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return d


def classify(doc_type, title):
    dt = doc_type or ""
    if dt.startswith(EXCLUDE_PREFIX):
        return None
    if dt.startswith("通函") and not any(k in dt for k in ("供股", "收購守則", "可轉換", "私有化")):
        return None
    if dt == "合併守則 - 交易披露":
        return None
    blob = dt + " " + (title or "")
    for typ, kws in RULES:
        if any(k in blob for k in kws):
            return typ
    return None


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def cache_max_date(cache):
    latest = ""
    for lst in cache.values():
        if not isinstance(lst, list):
            continue
        for e in lst:
            d = norm_date(e.get("date"))
            if d > latest:
                latest = d
    return latest


def update(dry_run=False):
    cache = load_cache()
    before = sum(len(v) for v in cache.values() if isinstance(v, list))
    max_d = cache_max_date(cache)
    start = ""
    if max_d:
        start = (datetime.strptime(max_d, "%Y-%m-%d") - timedelta(days=3)).date().isoformat()

    with open(ANN_FILE) as f:
        anns = json.load(f)

    existing = set()
    for code, lst in cache.items():
        if not isinstance(lst, list):
            continue
        for e in lst:
            existing.add((code, norm_date(e.get("date")), e.get("type")))

    now = datetime.now().isoformat()
    added = 0
    for a in anns:
        d = norm_date(a.get("date"))
        if not d or (start and d < start):
            continue
        code = str(a.get("stock_code") or "").zfill(5)
        if not code or code == "00000":
            continue
        typ = classify(a.get("doc_type"), a.get("title"))
        if not typ:
            continue
        key = (code, d, typ)
        if key in existing:
            continue
        existing.add(key)
        title = (a.get("title") or "").strip() or (a.get("doc_type") or typ)
        cache.setdefault(code, []).append({
            "date": d,
            "type": typ,
            "title": title,
            "detail": title,
            "source": "hkex_announcements",
            "news_id": a.get("news_id"),
            "collected_at": now,
        })
        added += 1

    after = before + added
    if added and not dry_run:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CACHE_FILE)
    print(f"corp_actions_updater: window>={start or '(all)'} added={added} total={after} (was {before})")
    return added


if __name__ == "__main__":
    update(dry_run="--dry-run" in sys.argv)
