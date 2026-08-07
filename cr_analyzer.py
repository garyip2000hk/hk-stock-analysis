"""
cr_analyzer.py — 公司註冊處數據分析
Reads from imported/cr_signals.json
Detects: new incorporations, name changes, shell company signals
"""

import json, re
from pathlib import Path
from datetime import date, timedelta
from collections import defaultdict

BASE = Path(__file__).parent
IMPORTED = BASE / "imported"


def _load(name):
    with open(IMPORTED / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


def new_incorporations(days=30, limit=50) -> list:
    """Recent new company registrations."""
    data = _load("cr_signals.json")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    results = [s for s in data if s["type"] == "new_incorporation" and s["date"] >= cutoff]
    results.sort(key=lambda x: x["date"], reverse=True)
    return results[:limit]


def name_changes(days=30, limit=50) -> list:
    """Recent name changes (potential shell company signal)."""
    data = _load("cr_signals.json")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    results = [s for s in data if s["type"] == "name_change" and s["date"] >= cutoff]
    results.sort(key=lambda x: x["date"], reverse=True)
    return results[:limit]


def shell_signals() -> list:
    """
    Detect shell company signals:
    - Name change within 2 years of incorporation
    - Multiple name changes
    - Company names containing shell keywords
    """
    data = _load("cr_signals.json")
    inc_dates = {}
    name_change_map = defaultdict(list)

    for s in data:
        if s["type"] == "new_incorporation":
            inc_dates[s["br"]] = s["date"]
        elif s["type"] == "name_change":
            name_change_map[s["br"]].append(s)

    signals = []
    shell_keywords = ["investment holding", "投資控股", "holdings limited", "控股有限公司"]

    for br, changes in name_change_map.items():
        inc = inc_dates.get(br)
        if not inc:
            continue
        latest = changes[-1]
        signals.append({
            "br": br,
            "name": latest["name"],
            "inc_date": inc,
            "change_date": latest["date"],
            "change_count": len(changes),
            "type": "multiple_changes" if len(changes) >= 3 else "recent_name_change",
        })

    signals.sort(key=lambda x: x["change_count"], reverse=True)
    return signals[:100]


def get_stats() -> dict:
    """CR data statistics."""
    data = _load("cr_signals.json")
    inc = [s for s in data if s["type"] == "new_incorporation"]
    nc = [s for s in data if s["type"] == "name_change"]
    return {
        "total_incorporations": len(inc),
        "total_name_changes": len(nc),
        "latest_incorporation": inc[0]["date"] if inc else None,
        "latest_name_change": nc[0]["date"] if nc else None,
    }


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if cmd == "stats":
        s = get_stats()
        for k, v in s.items():
            print(f"{k}: {v}")
    elif cmd == "new":
        for r in new_incorporations(int(sys.argv[2]) if len(sys.argv) > 2 else 30):
            print(f"{r['date']} {r['name'][:50]:<50} {r['br']}")
    elif cmd == "name":
        for r in name_changes(int(sys.argv[2]) if len(sys.argv) > 2 else 30):
            print(f"{r['date']} {r['name'][:50]:<50} {r['br']}")
    elif cmd == "shell":
        for r in shell_signals():
            print(f"{r['name'][:40]:<40} inc:{r['inc_date']} changed:{r['change_count']}x")
    else:
        print("Usage: python3 cr_analyzer.py [stats|new|name|shell] [days]")
