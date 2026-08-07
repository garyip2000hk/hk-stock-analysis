"""
short_analyzer.py — 沽空數據分析工具
Reads from imported/short_positions.json + imported/quotes.json
Detects: top shorted, short squeeze signals, WoW changes
"""

import json
from pathlib import Path
from datetime import date, timedelta

BASE = Path(__file__).parent
IMPORTED = BASE / "imported"


def _load(name):
    with open(IMPORTED / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_quotes() -> dict:
    """quotes.json 新格式 {"meta":…,"names":…,"quotes":{date:{code:{...}}}}，
    舊格式 {date:{code:{...}}}。兩者都支援。"""
    data = _load("quotes.json")
    if isinstance(data, dict) and "quotes" in data:
        return data["quotes"]
    return data


def top_shorted(limit=20) -> list:
    """Top N shorted stocks by HK$ value."""
    data = _load("short_positions.json")
    latest_date = max(r["date"] for r in data)
    latest = [r for r in data if r["date"] == latest_date]
    latest.sort(key=lambda x: x["short_hkd"], reverse=True)
    return latest[:limit]


def short_changes(days=7, limit=30) -> list:
    """Biggest short position changes in last N days."""
    data = _load("short_positions.json")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    recent = [r for r in data if r["date"] >= cutoff]

    by_code = {}
    for r in recent:
        by_code.setdefault(r["code"], []).append(r)

    changes = []
    for code, recs in by_code.items():
        recs.sort(key=lambda x: x["date"])
        if len(recs) < 2:
            continue
        latest, prev = recs[-1], recs[-2]
        if prev["short_shares"] == 0:
            continue
        change = (latest["short_shares"] - prev["short_shares"]) / prev["short_shares"] * 100
        changes.append({
            "code": code,
            "name": latest["name"],
            "prev_shares": prev["short_shares"],
            "current_shares": latest["short_shares"],
            "change_pct": round(change, 2),
            "short_hkd": latest["short_hkd"],
            "date": latest["date"],
        })
    changes.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    return changes[:limit]


def short_squeeze_candidates() -> list:
    """Stocks with high short interest + recent price increase (squeeze signal)."""
    sp = _load("short_positions.json")
    quotes = _load_quotes()

    latest_date = max(r["date"] for r in sp)
    latest_sp = [r for r in sp if r["date"] == latest_date]
    by_code = {r["code"]: r for r in latest_sp}

    recent_dates = sorted(quotes.keys())[-20:]
    price_change = {}
    for dt in recent_dates:
        for code, data in quotes[dt].items():
            if code not in price_change:
                price_change[code] = {"first": data["close"], "last": data["close"], "dates": []}
            price_change[code]["last"] = data["close"]
            price_change[code]["dates"].append(dt)

    candidates = []
    for code, sp_data in by_code.items():
        if code not in price_change:
            continue
        pc = price_change[code]
        if pc["first"] == 0:
            continue
        change = (pc["last"] - pc["first"]) / pc["first"] * 100
        if change > 3:
            candidates.append({
                "code": code,
                "name": sp_data["name"],
                "short_hkd": sp_data["short_hkd"],
                "price_change_20d": round(change, 2),
                "short_shares": sp_data["short_shares"],
            })
    candidates.sort(key=lambda x: x["short_hkd"], reverse=True)
    return candidates[:30]


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "top"

    if cmd == "top":
        for r in top_shorted(int(sys.argv[2]) if len(sys.argv) > 2 else 20):
            print(f"{r['code']} {r['name']:<30} {r['short_shares']:>15,} {r['short_hkd']:>18,}")
    elif cmd == "changes":
        for r in short_changes():
            print(f"{r['code']} {r['name']:<30} {r['change_pct']:>+8.2f}% {r['short_hkd']:>18,}")
    elif cmd == "squeeze":
        for r in short_squeeze_candidates():
            print(f"{r['code']} {r['name']:<30} short:{r['short_hkd']:>15,} price:{r['price_change_20d']:>+8.2f}%")
    else:
        print("Usage: python3 short_analyzer.py [top|changes|squeeze] [N]")
