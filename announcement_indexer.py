"""
announcement_indexer.py — HKEX 公告索引 + 關鍵詞搜尋
Reads from imported/announcements.json
Cross-references with corp_scanner results
"""

import json, re
from pathlib import Path
from datetime import date, timedelta
from collections import defaultdict

BASE = Path(__file__).parent
IMPORTED = BASE / "imported"

CATEGORY_MAP = {
    "rights_issue": {"keywords": ["供股", "rights issue", "公開發售", "open offer",
                                  "發售以供認購"],
                     "label": "供股／公開發售", "signal": "🔴 財技動作"},
    "cb": {"keywords": ["可換股債券", "可轉換債券", "可轉債", "convertible bond",
                        "convertible note", "發行可轉換證券", "換股債"],
           "label": "可換股債券", "signal": "🔴 財技動作"},
    "placing": {"keywords": ["配售", "placing", "先舊後新", "top-up placing",
                             "根據一般性授權發行股份", "認購新股份"],
                "label": "配售／發新股", "signal": "🔴 財技動作"},
    "consolidation": {"keywords": ["股份合併", "share consolidation", "併股", "合股",
                                   "更改每股面值", "股本重組"],
                      "label": "合股／股本重組", "signal": "🔴 財技動作"},
    "general_offer": {"keywords": ["收購守則", "全面要約", "強制性現金要約",
                                   "mandatory offer", "general offer", "全購"],
                      "label": "要約收購", "signal": "🟠 要約"},
    "profit_warning": {"keywords": ["盈利警告", "盈警", "profit warning", "盈利預警",
                                     "預期虧損", "由盈轉虧"],
                        "label": "盈利警告", "signal": "🔴 盈警"},
    "profit_alert": {"keywords": ["正面盈利預告", "盈喜", "positive profit alert",
                                  "盈利大幅增長", "由虧轉盈"],
                     "label": "盈喜", "signal": "🟢 盈喜"},
    "inside_info": {"keywords": ["內幕消息", "inside information"],
                    "label": "內幕消息", "signal": "🟠 內幕消息"},
    "results": {"keywords": ["末期業績", "中期業績", "季度業績", "annual results",
                             "interim results", "quarterly results"],
                "label": "業績", "signal": "🟡 業績"},
    "buyback": {"keywords": ["回購", "buy-back", "buyback", "repurchase"], "label": "回購", "signal": "🟢 正面"},
    "merger": {"keywords": ["合併", "merger", "amalgamation"], "label": "合併", "signal": "🟡 重組"},
    "acquisition": {"keywords": ["收購", "acquisition", "takeover"], "label": "收購", "signal": "🟡 重組"},
    "restructuring": {"keywords": ["重組", "restructur", "reorganiz"], "label": "重組", "signal": "🟡 重組"},
    "suspension": {"keywords": ["停牌", "suspension", "halt"], "label": "停牌", "signal": "🔴 停牌"},
    "resumption": {"keywords": ["復牌", "resumption"], "label": "復牌", "signal": "🟢 復牌"},
    "delisting": {"keywords": ["除牌", "delisting", "withdrawal"], "label": "除牌", "signal": "🔴 除牌"},
    "name_change": {"keywords": ["更名", "change of name", "renamed"], "label": "更名", "signal": "🟡 改名"},
    "dividend": {"keywords": ["派息", "dividend", "final dividend", "interim dividend"], "label": "派息", "signal": "🟢 派息"},
    "privatization": {"keywords": ["私有化", "privatiz"], "label": "私有化", "signal": "🟡 私有化"},
    "connected_transaction": {"keywords": ["connected transaction", "關連交易"], "label": "關連交易", "signal": "🟡 關連交易"},
    "discloseable_transaction": {"keywords": ["discloseable transaction", "須予披露交易"], "label": "須予披露交易", "signal": "🟡 須予披露"},
}


def _load(name):
    with open(IMPORTED / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


def categorize(title: str, doc_type: str = "") -> dict:
    """按公告標題 + HKEXnews 公告類別分類（doc_type 準確度較高，優先）。"""
    t = f"{title} {doc_type}".lower()
    for cat_id, info in CATEGORY_MAP.items():
        for kw in info["keywords"]:
            if kw in t:
                return {"category": cat_id, "label": info["label"], "signal": info["signal"]}
    return {"category": "other", "label": "其他", "signal": "⚪ 其他"}


def search(keyword: str, stock_code: str = None, days: int = 365) -> list:
    """Full-text search announcements."""
    data = _load("announcements.json")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    kw = keyword.lower()
    results = []
    for a in data:
        if a["date"] < cutoff:
            continue
        if stock_code and a["stock_code"] != stock_code:
            continue
        text = f"{a['title']} {a['company']}".lower()
        if kw in text:
            cat = categorize(a["title"], a.get("doc_type", ""))
            results.append({**a, **cat})
    results.sort(key=lambda x: x["date"], reverse=True)
    return results


def get_by_stock(stock_code: str) -> list:
    """All announcements for a specific stock."""
    data = _load("announcements.json")
    code = stock_code.zfill(5)
    results = [{**a, **categorize(a["title"], a.get("doc_type", ""))} for a in data if a["stock_code"] == code]
    results.sort(key=lambda x: x["date"], reverse=True)
    return results


def get_by_category(cat_id: str, days: int = 90) -> list:
    """All announcements in a category."""
    data = _load("announcements.json")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    results = []
    for a in data:
        if a["date"] < cutoff:
            continue
        cat = categorize(a["title"], a.get("doc_type", ""))
        if cat["category"] == cat_id:
            results.append({**a, **cat})
    results.sort(key=lambda x: x["date"], reverse=True)
    return results


def get_summary(days: int = 30) -> dict:
    """Summary of announcements by category in last N days."""
    data = _load("announcements.json")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    recent = [a for a in data if a["date"] >= cutoff]

    by_cat = defaultdict(list)
    for a in recent:
        cat = categorize(a["title"], a.get("doc_type", ""))
        by_cat[cat["category"]].append(a)

    summary = {}
    for cat_id, info in CATEGORY_MAP.items():
        anns = by_cat.get(cat_id, [])
        if anns:
            summary[cat_id] = {
                "label": info["label"],
                "signal": info["signal"],
                "count": len(anns),
                "stocks": sorted(set(a["stock_code"] for a in anns)),
            }
    return summary


def get_recent_signals(limit=50) -> list:
    """Latest announcements with trading signals."""
    data = _load("announcements.json")
    data.sort(key=lambda x: x["date"], reverse=True)
    results = []
    for a in data[:limit * 3]:
        cat = categorize(a["title"], a.get("doc_type", ""))
        if cat["category"] != "other":
            results.append({**a, **cat})
            if len(results) >= limit:
                break
    return results


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"

    if cmd == "summary":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        s = get_summary(days)
        for cat_id, info in s.items():
            print(f"{info['signal']} {info['label']} ({info['count']}): {', '.join(info['stocks'][:10])}")
    elif cmd == "search":
        kw = sys.argv[2] if len(sys.argv) > 2 else ""
        for r in search(kw, sys.argv[3] if len(sys.argv) > 3 else None):
            print(f"{r['date']} {r['stock_code']} {r['signal']} {r['title'][:60]}")
    elif cmd == "stock":
        code = sys.argv[2] if len(sys.argv) > 2 else "00001"
        for r in get_by_stock(code):
            print(f"{r['date']} {r['signal']} {r['title'][:60]}")
    elif cmd == "recent":
        for r in get_recent_signals(int(sys.argv[2]) if len(sys.argv) > 2 else 50):
            print(f"{r['date']} {r['stock_code']} {r['signal']} {r['title'][:60]}")
    else:
        print("Usage: python3 announcement_indexer.py [summary|search|stock|recent] [args]")
