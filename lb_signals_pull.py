#!/usr/bin/env python3
"""長橋 MCP 異動 + AI 訊號拉取 → stock-analysis/imported/lb_signals.json

用途：
1. 即市異動（anomaly）：港股全市場「急速拉升／加速下跌」串流，計牛熊情緒廣度，
   供牛熊雷達 kill signal（屠熊／屠牛）做日內確認。
2. AI 訊號（signals）：長橋事件驅動策略觀點，供 corp_scanner 做公告解讀補充。

用法：
  python3 lb_signals_pull.py              # 拉異動+訊號，寫入 imported/lb_signals.json
  python3 lb_signals_pull.py --print      # 同上，但只印摘要唔寫檔
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

SKILL_SCRIPTS = "/home/workspace/Skills/longbridge-mcp/scripts"
sys.path.insert(0, SKILL_SCRIPTS)
from lb import rpc, load_token  # noqa: E402

OUT_PATH = "/home/workspace/stock-analysis/imported/lb_signals.json"
RADAR_PATH = "/home/workspace/stock-analysis/options_data/predict_vs_actual.json"
HK_TZ = timezone(timedelta(hours=8))


def call(tool, args):
    res = rpc("tools/call", {"name": tool, "arguments": args}, load_token())
    out = res.get("result", {})
    if out.get("isError"):
        raise RuntimeError(f"{tool}: {out.get('content', [{}])[0].get('text', '')[:200]}")
    sc = out.get("structuredContent")
    if sc is not None:
        return sc
    return json.loads(out.get("content", [{}])[0].get("text", "null"))


def pull(count=50):
    now = datetime.now(HK_TZ)
    anomaly = call("anomaly", {"market": "HK", "count": count})
    signals = call("signals", {"market": "HK"})
    changes = anomaly.get("changes", []) if isinstance(anomaly, dict) else anomaly

    bull = [c for c in changes if c.get("emotion") == 1]
    bear = [c for c in changes if c.get("emotion") == 2]
    newest = max((float(c.get("alert_time", 0)) for c in changes), default=0)
    age_min = (now.timestamp() - newest) / 60
    breadth = {
        "window": "full feed (latest batch)",
        "bull_count": len(bull),
        "bear_count": len(bear),
        "ratio": round(len(bull) / len(bear), 2) if bear else None,
        "bias": ("bull" if len(bull) > len(bear) * 1.5 else
                 "bear" if len(bear) > len(bull) * 1.5 else "neutral"),
        "freshness_min": round(age_min, 1),
        "stale": age_min > 120,
    }

    radar_check = {"available": False}
    try:
        radar = json.load(open(RADAR_PATH)).get("radar", [])
        today = now.strftime("%Y-%m-%d")
        rec = next((r for r in reversed(radar) if r.get("d") == today), None) or (radar[-1] if radar else None)
        if rec:
            rdir = rec.get("dir", "range")
            agree = (rdir == "up" and breadth["bias"] == "bull") or (
                rdir == "down" and breadth["bias"] == "bear")
            radar_check = {
                "available": True,
                "date": rec.get("d"),
                "verdict": rec.get("verdict"),
                "dir": rdir,
                "breadth_bias": breadth["bias"],
                "confirm": agree if rdir in ("up", "down") else None,
                "note": ("雷達方向與異動廣度一致" if agree else
                         "窄幅預測，廣度只作參考" if rdir == "range" else
                         "⚠️ 雷達方向與異動廣度相反，訊號存疑"),
            }
    except Exception as e:
        radar_check = {"available": False, "error": str(e)[:120]}

    doc = {
        "pulled_at": now.isoformat(),
        "source": "longbridge-mcp (hosted, https://mcp.longbridge.com/mcp)",
        "anomaly_breadth": breadth,
        "radar_cross_check": radar_check,
        "anomaly": [
            {
                "symbol": c.get("symbol", "").replace(".HK", ""),
                "name": c.get("name"),
                "alert_name": c.get("alert_name"),
                "emotion": c.get("emotion"),
                "change": (c.get("change_values") or [None])[0],
                "alert_time": c.get("alert_time"),
            }
            for c in changes
        ],
        "signals": [
            {
                "symbol": s.get("symbol"),
                "name": s.get("company_name"),
                "title": s.get("title"),
                "summary": s.get("summary"),
                "signal_id": s.get("id"),
            }
            for s in (signals.get("signals", []) if isinstance(signals, dict) else signals)
        ],
    }
    return doc


def main():
    print_only = "--print" in sys.argv
    doc = pull()
    br = doc["anomaly_breadth"]
    fresh = "⚠️ 舊批次" if br["stale"] else "新鮮"
    print(f"即市異動廣度（最新批次，{br['freshness_min']} 分鐘前更新，{fresh}）："
          f"牛 {br['bull_count']} vs 熊 {br['bear_count']} → {br['bias'].upper()}（ratio {br['ratio']}）")
    rc = doc["radar_cross_check"]
    if rc.get("available"):
        print(f"雷達交叉確認（{rc['date']}，{rc['verdict']}）：{rc['note']}")
    print(f"AI 訊號：{len(doc['signals'])} 條（market HK）")
    for s in doc["signals"][:5]:
        print(f"  [{s['symbol']}] {s['title']}")
    if not print_only:
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "w") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        print(f"已寫入 {OUT_PATH}")


if __name__ == "__main__":
    main()
