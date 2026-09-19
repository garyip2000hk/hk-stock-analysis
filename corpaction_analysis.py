"""財技動作逐件深度分析：解釋／歸邊／影響／倉位變化。

CLI: python3 corpaction_analysis.py <stock> <event_date> [--type <type>]
輸出 JSON 到 stdout，供 zo.space /api/corpaction-analysis 呼叫。
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import ccass_snapshot as cs

HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / "corp_actions_cache.json"
QUOTES_FILE = HERE / "imported" / "quotes.json"

CATEGORY = {
    "供股": "rights", "placing": "placing", "配股": "placing", "配售": "placing",
    "全購": "offer", "全面收購/要約": "offer", "收購": "offer",
    "cb": "cb", "可換股債券": "cb", "CB轉換": "cb",
    "合股": "consolidation", "拆股": "split", "送紅股": "bonus",
    "私有化": "privatize",
}

TYPE_INFO = {
    "rights": (
        "供股",
        "公司向現有股東按持股比例、以認購價發行新股集資。股東唔跟足認購，股權就會被攤薄；"
        "財技股常用深折讓供股洗走散戶、令籌碼歸邊到大戶手上。",
        "股數增加＝攤薄；折讓愈深、供股比例愈大，洗倉效應愈強。",
    ),
    "placing": (
        "配售／發新股",
        "公司向指定投資者發行新股集資，即時增加股數、攤薄現有股東。"
        "若承接方集中係一兩個大戶，籌碼會一次過歸邊。",
        "配售價通常折讓；承接人身份決定係『引入戰略股東』定係『派貨通道』。",
    ),
    "offer": (
        "要約／全購",
        "收購方向全體股東提出按指定價格買入股份。完成後收購方持股大升，"
        "籌碼高度集中於要約人；若持股超過門檻可能觸發強制收購或私有化。",
        "股價通常貼近要約價；失敗／撤回則股價回跌。",
    ),
    "cb": (
        "可換股債券",
        "公司發行日後可按換股價轉換成股份嘅債券。未轉換時係債，轉換後股數增加、攤薄股東；"
        "亦係引入策略股東／令籌碼歸邊嘅常見工具。",
        "換股價與現價嘅關係決定轉換誘因；大額轉換會顯著改變股權結構。",
    ),
    "consolidation": (
        "合股",
        "將多股合併為一股（例如 10 合 1），股數減少、股價按比例上升，本身唔攤薄。"
        "但財技股常以合股抬高股價門檻洗走散戶，並為後續供股／配售鋪路。",
        "每手入場費上升；碎股流動性變差；常係下一輪財技嘅前奏。",
    ),
    "split": (
        "拆股",
        "將一股拆成多股，股數增加、股價按比例下降，唔攤薄股權，通常為提升流動性。",
        "入場費下降；對股權結構無實質影響。",
    ),
    "bonus": (
        "送紅股",
        "按持股比例免費送股，股數增加但唔攤薄（資本化發行）。",
        "股價按比例除權；訊號意義大過實質影響。",
    ),
    "privatize": (
        "私有化",
        "大股東買晒其餘股東手上嘅股份並撤銷上市，籌碼最終 100% 歸邊。",
        "要約價即係股東最後出場機會；通過後股票除牌。",
    ),
    "other": (
        "公司行動／公告",
        "公司層面嘅行動或公告事件，對股權結構嘅影響要睇具體內容。",
        "視內容而定。",
    ),
}

STATUS_LABEL = {
    "completed": "已完成", "proposed": "建議中", "pending": "進行中",
    "awaiting_approval": "待批准", "withdrawn": "已撤回", "lapsed": "已失效",
    "in_progress": "進行中",
}


def _d(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def _load_cache() -> dict:
    return json.loads(CACHE_FILE.read_text("utf-8"))


def _find_event(stock: str, event_date: str, event_type: str | None) -> dict | None:
    events = _load_cache().get(stock, [])
    for ev in events:
        if (ev.get("date") or "")[:10] == event_date and (not event_type or ev.get("type") == event_type):
            return ev
    if not event_type:
        for ev in events:
            if (ev.get("date") or "")[:10] == event_date:
                return ev
    return None


def _parse_ratio(ratio: str | None) -> dict:
    out = {"multiplier": None, "kind": None, "raw": ratio or ""}
    if not ratio:
        return out
    m = re.search(r"(\d+(?:\.\d+)?)\s*[供配]\s*(\d+(?:\.\d+)?)", ratio)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0:
            out.update(multiplier=1 + b / a, kind="issue")
        return out
    m = re.search(r"(\d+(?:\.\d+)?)\s*合\s*(\d+(?:\.\d+)?)", ratio)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0:
            out.update(multiplier=b / a, kind="consolidation")
        return out
    m = re.search(r"(\d+(?:\.\d+)?)\s*拆\s*(\d+(?:\.\d+)?)", ratio)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0:
            out.update(multiplier=b / a, kind="split")
        return out
    m = re.search(r"(\d+(?:\.\d+)?)\s*送\s*(\d+(?:\.\d+)?)", ratio)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0:
            out.update(multiplier=1 + b / a, kind="bonus")
        return out
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", ratio)
    if m:
        out.update(pct=float(m.group(1)), kind="pct")
    return out


def _quotes() -> dict:
    try:
        return json.loads(QUOTES_FILE.read_text("utf-8"))
    except Exception:
        return {"quotes": {}, "names": {}}


def _price_impact(code: str, event_date: str) -> dict:
    q = _quotes()
    series = q.get("quotes", {})
    dates = sorted(d for d in series if code in series[d] and series[d][code].get("close"))
    if not dates:
        return {"available": False, "note": "報價快取無呢隻股票嘅數據"}
    ev = _d(event_date)
    before = [d for d in dates if _d(d) < ev]
    after = [d for d in dates if _d(d) >= ev]
    if not before:
        return {"available": False, "note": "事件前無報價數據"}
    t0 = before[-1]

    def close_at(d):
        return series[d][code]["close"]

    def offset_from(idx: int, n: int) -> str | None:
        return dates[idx + n] if 0 <= idx + n < len(dates) else None

    idx = dates.index(t0)
    rows = {"t_minus1": {"date": t0, "close": close_at(t0)}}
    for label, n in (("t_plus5", 5), ("t_plus20", 20), ("latest", None)):
        d = dates[-1] if label == "latest" else offset_from(idx, n)
        if d and _d(d) > _d(t0):
            rows[label] = {"date": d, "close": close_at(d)}
    base = close_at(t0)
    for label in ("t_plus5", "t_plus20", "latest"):
        if label in rows and base:
            rows[label]["chg_pct"] = round((rows[label]["close"] - base) / base * 100, 2)
    return {"available": True, "rows": rows, "note": f"以事件前最後交易日 {t0} 收市價做基準"}


def _concentration_and_positions(code: str, event_date: str) -> dict:
    cov = cs.coverage_range()
    if not cov[0] or not cov[1]:
        return {"available": False, "note": "本地 CCASS 資料庫無覆蓋"}
    before_snap = cs.snapshot(code, event_date, top_n=60)
    if before_snap.get("error"):
        return {"available": False, "note": before_snap["error"]}
    before_date = before_snap["date"]
    after_target = min(_d(event_date) + timedelta(days=90), _d(cov[1]))
    if after_target <= _d(before_date):
        after_target = _d(cov[1])
    after_snap = cs.snapshot(code, str(after_target), top_n=60)
    if after_snap.get("error"):
        return {"available": False, "note": after_snap["error"]}
    after_date = after_snap["date"]
    if after_date == before_date:
        return {
            "available": False,
            "before": {"date": before_date, **before_snap.get("concentration", {})},
            "note": "事件太新（或之後無新持倉變動），暫未見到事後歸邊變化",
        }

    def conc(s):
        c = s.get("concentration", {})
        return {"top_5": c.get("top_5", 0), "top_10": c.get("top_10", 0), "top_20": c.get("top_20", 0),
                "participants": s.get("total_participants", 0)}

    b, a = conc(before_snap), conc(after_snap)
    d5 = round(a["top_5"] - b["top_5"], 2)
    d10 = round(a["top_10"] - b["top_10"], 2)
    if d10 >= 5 or d5 >= 5:
        verdict, level = "明顯歸邊", "high"
    elif d10 >= 2 or d5 >= 2:
        verdict, level = "輕度歸邊", "mid"
    elif d10 <= -2 or d5 <= -2:
        verdict, level = "籌碼分散／稀釋", "spread"
    else:
        verdict, level = "無明顯歸邊變化", "none"
    top1_after = (after_snap.get("top_holders") or [{}])[0]

    before_map = {h["participant_id"]: h for h in before_snap.get("participants", [])}
    after_map = {h["participant_id"]: h for h in after_snap.get("participants", [])}
    flows = []
    for pid in set(before_map) | set(after_map):
        bh, ah = before_map.get(pid), after_map.get(pid)
        bs = int(bh["shares"]) if bh else 0
        as_ = int(ah["shares"]) if ah else 0
        if as_ - bs == 0:
            continue
        flows.append({
            "participant_id": pid,
            "name": (ah or bh).get("name", ""),
            "shares_before": bs,
            "shares_after": as_,
            "delta_shares": as_ - bs,
            "percentage_before": round(float(bh["percentage"]) if bh else 0.0, 4),
            "percentage_after": round(float(ah["percentage"]) if ah else 0.0, 4),
            "delta_percentage": round((float(ah["percentage"]) if ah else 0.0) - (float(bh["percentage"]) if bh else 0.0), 4),
        })
    accumulators = sorted((f for f in flows if f["delta_shares"] > 0), key=lambda x: x["delta_shares"], reverse=True)[:8]
    distributors = sorted((f for f in flows if f["delta_shares"] < 0), key=lambda x: x["delta_shares"])[:8]
    return {
        "available": True,
        "before": {"date": before_date, **b},
        "after": {"date": after_date, **a},
        "delta_top5": d5, "delta_top10": d10,
        "verdict": verdict, "verdict_level": level,
        "top1_after": {"name": top1_after.get("name", ""), "percentage": top1_after.get("percentage", 0)},
        "accumulators": accumulators,
        "distributors": distributors,
        "flow_count": len(flows),
    }


WATCH_LEVELS = {
    "high": ("\u2b50 \u91cd\u9ede\u76e3\u5bdf", "\u5efa\u8b70\u52a0\u5165\u91cd\u9ede\u76e3\u5bdf\uff1a\u8ffd\u8e64\u4e8b\u4ef6\u5b8c\u6210\u9032\u5ea6\u3001\u4e8b\u5f8c CCASS \u6301\u5009\u8b8a\u5316\u3001\u5927\u984d\u6536\u8ca8\u5238\u5546\u53ca\u6709\u7121\u9023\u7e8c\u8ca1\u6280\u52d5\u4f5c\u3002"),
    "mid": ("\ud83d\udc41 \u503c\u5f97\u7559\u610f", "\u53ef\u52a0\u5165\u89c0\u5bdf\u540d\u55ae\uff0c\u6bcf\u9031\u8907\u67e5\u4e00\u6b21\u6301\u5009\u8207\u80a1\u50f9\u8b8a\u5316\u3002"),
    "low": ("\u66ab\u7121\u9700\u91cd\u9ede\u76e3\u5bdf", "\u76ee\u524d\u8a0a\u865f\u4e0d\u5f37\uff0c\u6b63\u5e38\u89c0\u5bdf\u5373\u53ef\uff1b\u82e5\u5f8c\u7e8c\u6709\u65b0\u516c\u544a\u6216\u6301\u5009\u7570\u52d5\u518d\u91cd\u65b0\u8a55\u4f30\u3002"),
}


def _watch_verdict(code: str, ev: dict, conc: dict, dilution: dict | None, price: dict, status: str) -> dict:
    score = 0
    reasons = []
    if status in ("proposed", "pending", "in_progress", "awaiting_approval"):
        score += 2
        reasons.append(f"\u4e8b\u4ef6\u72c0\u614b\u300c{STATUS_LABEL.get(status, status)}\u300d\uff0c\u6d41\u7a0b\u4ecd\u5728\u9032\u884c\uff0c\u5b8c\u6210\u5f8c\u80a1\u6b0a\u7d50\u69cb\u53ef\u80fd\u5927\u5e45\u8b8a\u5316")
    elif status in ("withdrawn", "lapsed"):
        score -= 3
        reasons.append("\u4e8b\u4ef6\u5df2\u6492\u56de\uff0f\u5931\u6548\uff0c\u77ed\u671f\u5167\u7121\u9700\u8ffd\u8e64")
    lvl = conc.get("verdict_level")
    if lvl == "high":
        score += 2
        reasons.append(f"\u4e8b\u5f8c\u7c4c\u78bc\u660e\u986f\u6b78\u908a\uff08\u982d10\u5927\u4f54\u6bd4 {conc.get('delta_top10', 0):+.1f}pp\uff09")
    elif lvl == "mid":
        score += 1
        reasons.append(f"\u4e8b\u5f8c\u8f15\u5ea6\u6b78\u908a\uff08\u982d10\u5927\u4f54\u6bd4 {conc.get('delta_top10', 0):+.1f}pp\uff09")
    elif lvl == "spread":
        reasons.append("\u4e8b\u5f8c\u7c4c\u78bc\u53cd\u800c\u5206\u6563\uff0c\u672a\u898b\u6b78\u908a\u8de1\u8c61")
    big = [a for a in conc.get("accumulators", []) if a.get("delta_percentage", 0) >= 1.0]
    mild = [a for a in conc.get("accumulators", []) if 0.5 <= a.get("delta_percentage", 0) < 1.0]
    if big:
        score += 2
        names = "\u3001".join(f"{a['name'][:24]}({a['delta_percentage']:+.2f}pp)" for a in big[:3])
        reasons.append(f"{len(big)} \u500b\u5238\u5546\u55ae\u4e00\u7a97\u53e3\u6536\u8ca8 \u2265 1% \u80a1\u6b0a\uff1a{names}")
    elif mild:
        score += 1
        reasons.append(f"\u6709\u5238\u5546\u660e\u986f\u52a0\u5009\uff08\u6700\u5927 {mild[0]['delta_percentage']:+.2f}pp\uff09")
    if dilution and dilution.get("new_share_pct") is not None:
        pct = dilution["new_share_pct"]
        if pct >= 30:
            score += 2
            reasons.append(f"\u6524\u8584\u5e45\u5ea6\u5927\uff1a\u65b0\u80a1\u4f54\u767c\u884c\u5f8c {pct}%\uff0c\u4e0d\u8ddf\u8db3\u8a8d\u8cfc\u5373\u88ab\u6d17\u5009")
        elif pct >= 10:
            score += 1
            reasons.append(f"\u65b0\u80a1\u4f54\u767c\u884c\u5f8c {pct}%\uff0c\u6709\u4e00\u5b9a\u6524\u8584")
    events = _load_cache().get(code, [])
    evd = _d(ev.get("date", "1970-01-01"))
    nearby = [e for e in events
              if e.get("date") and _d(e["date"]) != evd
              and abs((_d(e["date"]) - evd).days) <= 180
              and CATEGORY.get(e.get("type", ""), "other") in ("rights", "placing", "offer", "cb", "consolidation", "privatize")]
    if len(nearby) >= 2:
        score += 2
        chain = "\u3001".join(f"{e['date'][:7]} {e.get('type','')}" for e in nearby[:3])
        reasons.append(f"\u540c\u80a1 \u00b1180 \u65e5\u5167\u4e32\u806f\u51fa\u73fe\u591a\u5b97\u6524\u8584\uff0f\u6b78\u908a\u578b\u8ca1\u6280\u52d5\u4f5c\uff08{chain}\uff09\uff0c\u5c6c\u8ca1\u6280\u80a1\u5e38\u898b\u624b\u6cd5")
    elif len(nearby) == 1:
        score += 1
        reasons.append(f"\u524d\u5f8c 180 \u65e5\u5167\u53e6\u6709\u4e00\u5b97\u76f8\u95dc\u8ca1\u6280\u52d5\u4f5c\uff08{nearby[0]['date'][:7]} {nearby[0].get('type','')}\uff09")
    if price.get("available"):
        chg = price.get("rows", {}).get("latest", {}).get("chg_pct")
        if chg is not None and chg <= -30:
            score += 1
            reasons.append(f"\u4e8b\u4ef6\u5f8c\u80a1\u50f9\u7d2f\u8dcc {abs(chg):.0f}%\uff0c\u7559\u610f\u6d17\u5009\u6d3e\u8ca8\u98a8\u96aa")
    if not reasons:
        reasons.append("\u672a\u898b\u660e\u986f\u7570\u52d5\u8a0a\u865f")
    if status in ("withdrawn", "lapsed"):
        level = "low"
    elif score >= 4:
        level = "high"
    elif score >= 2:
        level = "mid"
    else:
        level = "low"
    label, action = WATCH_LEVELS[level]
    return {"level": level, "label": label, "score": score, "reasons": reasons, "action": action}


def analyze(stock: str, event_date: str, event_type: str | None = None) -> dict:
    code = cs.pad_code(stock)
    ev = _find_event(code, event_date, event_type)
    if not ev:
        return {"error": f"搇唔到 {code} 喺 {event_date} 嘅財技事件", "stock_code": code}
    etype = ev.get("type", "")
    cat = CATEGORY.get(etype, "other")
    label, what, impact_general = TYPE_INFO[cat]

    ratio = _parse_ratio(ev.get("ratio"))
    issued = cs.issued_shares(code)
    dilution = None
    dilution_note = impact_general
    if ratio.get("kind") == "issue" and ratio.get("multiplier"):
        m = ratio["multiplier"]
        new_pct = round((m - 1) / m * 100, 1)
        dilution = {"new_share_pct": new_pct, "share_multiplier": round(m, 3)}
        dilution_note = f"若全數認購／承接，股數變原來嘅 {round(m, 2)} 倍；新股佔發行後 {new_pct}%，唔跟足嘅股東股權同比例被攤薄。"
    elif ratio.get("kind") in ("split", "bonus") and ratio.get("multiplier"):
        m = ratio["multiplier"]
        dilution = {"share_multiplier": round(m, 3), "dilutive": False}
        dilution_note = f"股數變原來嘅 {round(m, 2)} 倍，但按比例派送，唔構成攤薄。"
    elif ratio.get("kind") == "consolidation" and ratio.get("multiplier"):
        m = ratio["multiplier"]
        dilution = {"share_multiplier": round(m, 3), "dilutive": False}
        dilution_note = f"股數縮為原來嘅 {round(m, 3)} 倍，股價按比例上調，本身唔攤薄；留意係咪後續供股／配售嘅前奏。"
    elif ratio.get("kind") == "pct":
        dilution = {"stake_pct": ratio["pct"]}
        dilution_note = f"涉及約 {ratio['pct']}% 股權。"

    case_bits = []
    if ev.get("ratio"):
        case_bits.append(f"比例：{ev['ratio']}")
    if ev.get("price"):
        case_bits.append(f"價格：HK${ev['price']}")
    if ev.get("ex_date"):
        case_bits.append(f"除權日：{ev['ex_date']}")
    if ev.get("deadline"):
        case_bits.append(f"截止日：{ev['deadline']}")
    status = ev.get("status", "")
    case_bits.append(f"狀態：{STATUS_LABEL.get(status, status or '未標明')}")

    conc = _concentration_and_positions(code, event_date)
    price = _price_impact(code, event_date)

    impact_points = []
    if dilution:
        impact_points.append(dilution_note)
    if price.get("available"):
        rows = price["rows"]
        if "latest" in rows:
            impact_points.append(f"股價由事件前（{rows['t_minus1']['date']} 收 {rows['t_minus1']['close']}）至 {rows['latest']['date']} 變咗 {rows['latest']['chg_pct']:+.1f}%。")
    else:
        impact_points.append(price.get("note", ""))
    if conc.get("available"):
        impact_points.append(f"CCASS 頭10大佔比由 {conc['before']['top_10']}% 變 {conc['after']['top_10']}%（{conc['delta_top10']:+.1f}pp）→ {conc['verdict']}。")
    elif conc.get("note"):
        impact_points.append(conc["note"])

    watch = _watch_verdict(code, ev, conc, dilution, price, status)

    return {
        "stock_code": code,
        "stock_name": _quotes().get("names", {}).get(code, ""),
        "event": ev,
        "explain": {
            "category": cat,
            "type_label": label,
            "raw_type": etype,
            "what": what,
            "this_case": "；".join(case_bits),
            "title": ev.get("title") or ev.get("detail") or "",
            "status": status,
            "status_label": STATUS_LABEL.get(status, status or "未標明"),
        },
        "concentration": conc,
        "impact": {
            "dilution": dilution,
            "price": price,
            "points": [p for p in impact_points if p],
            "issued_shares": issued,
        },
        "watch": watch,
        "positions": {
            "window": [conc.get("before", {}).get("date", ""), conc.get("after", {}).get("date", "")] if conc.get("available") else [],
            "accumulators": conc.get("accumulators", []),
            "distributors": conc.get("distributors", []),
            "note": "由 CCASS change-log forward-fill 重建；百分比以已發行股數計算。" if conc.get("available") else conc.get("note", ""),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock")
    parser.add_argument("event_date")
    parser.add_argument("--type", default=None)
    args = parser.parse_args()
    print(json.dumps(analyze(args.stock, args.event_date, args.type), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
