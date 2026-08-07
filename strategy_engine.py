"""strategy_engine.py — 由「事件 + 波幅定價」自動生成期權策略。

呢個模組係第 1 項（異動事件策略自動生成）嘅核心：

  1. 事件源
     · earnings_calendar.py  → 已公布嘅董事會／業績日（有確切日期）
     · announcement_indexer  → 財技公告（配售、供股、可換股債、盈警、盈喜、要約…）
     · ccass_options_cross   → CCASS 歸邊 × 期權資金流嘅方向偏好
  2. 波幅定價
     · iv_analyzer            → IV rank / IV vs HV，決定應該買波幅定賣波幅
  3. 策略選擇（方向 × 波幅貴平矩陣）
  4. 真實行使價
     · options_chain          → HKEX 每日報告逐個行使價嘅結算價 + 該價 IV
  5. 盈虧計算
     · bs.py                  → Greeks、觸價概率
     · 數值積分               → 用 HV20（真實世界波幅）計期望盈虧 + 勝率

輸出每個事件一套具體策略：買賣邊隻行使價、幾多錢、盈虧平衡點、
最大賺／蝕、勝率、期望值（每張合約港元）。

CLI:
    python3 strategy_engine.py                    # 全部事件策略
    python3 strategy_engine.py --stock 00700
    python3 strategy_engine.py --min-score 2 --limit 15
    python3 strategy_engine.py --json             # 寫 options_data/strategies.json
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import bs
import earnings_calendar as ec
import iv_analyzer
import options_chain as oc

BASE = Path(__file__).parent
SPECS = BASE / "options_data" / "contract_specs.json"
CROSS = BASE / "options_data" / "ccass_options_cross.json"
ANNS = BASE / "imported" / "announcements.json"
OUT_JSON = BASE / "options_data" / "strategies.json"

# ---------------------------------------------------------------- 事件字典
# view: up / down / neutral / pinned      vol: 事件本身會唧高定壓低波幅
EVENT_VIEW = {
    "results":        ("neutral", "業績公布", 3.0),
    "profit_warning": ("down", "盈利警告", 3.0),
    "profit_alert":   ("up", "正面盈喜", 2.5),
    "inside_info":    ("neutral", "內幕消息", 2.0),
    "placing":        ("down", "配售／發新股（攤薄）", 2.5),
    "rights_issue":   ("down", "供股／公開發售（攤薄）", 3.0),
    "cb":             ("down", "可換股債（潛在攤薄）", 2.0),
    "consolidation":  ("down", "合股／股本重組", 2.0),
    "buyback":        ("up", "回購（撐價）", 1.0),
    "general_offer":  ("pinned", "要約收購（價格釘死）", 2.5),
    "privatization":  ("pinned", "私有化（價格釘死）", 2.5),
    "acquisition":    ("neutral", "收購", 1.5),
    "merger":         ("neutral", "合併", 1.5),
    "restructuring":  ("neutral", "重組", 1.5),
    "dividend":       ("neutral", "派息", 0.5),
}
ANN_LOOKBACK = 7        # 財技公告只睇最近 N 日
EVENT_HORIZON = 60      # 業績日只睇未來 N 日

MIN_OI = 20             # 行使價最低未平倉，避免揀到零流通
GRID = 401              # 數值積分格數


# ---------------------------------------------------------------- 基礎資料

def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def contract_size(code: str) -> int:
    try:
        return int(_load(SPECS).get(code, {}).get("contract_size") or 0) or 1000
    except Exception:
        return 1000


_specs_cache: dict | None = None


def _spec(code: str) -> dict:
    global _specs_cache
    if _specs_cache is None:
        _specs_cache = _load(SPECS) if SPECS.exists() else {}
    return _specs_cache.get(code, {})


def vol_regime(iv_row: dict) -> tuple[str, str]:
    """由 iv_analyzer 嘅評分判斷應該買定賣波幅。"""
    score = iv_row.get("score") or 0
    ratio = iv_row.get("iv_hv")
    rank = iv_row.get("iv_rank")
    if score >= 3:
        return "rich", f"IV {iv_row['iv']:.0f}% 貴（IV/HV {ratio}、IVR {rank}）→ 賣波幅"
    if score <= -3:
        return "cheap", f"IV {iv_row['iv']:.0f}% 平（IV/HV {ratio}、IVR {rank}）→ 買波幅"
    return "fair", f"IV {iv_row['iv']:.0f}% 中性（IV/HV {ratio}、IVR {rank}）"


# ---------------------------------------------------------------- 事件收集

def _ann_events(codes: set[str], as_of: date) -> dict[str, list[dict]]:
    """最近 ANN_LOOKBACK 日嘅財技公告（只留期權標的）。"""
    import announcement_indexer as ai

    cutoff = (as_of - timedelta(days=ANN_LOOKBACK)).isoformat()
    out: dict[str, list[dict]] = {}
    for a in _load(ANNS):
        if a["date"] < cutoff or a["date"] > as_of.isoformat():
            continue
        code = a.get("stock_code")
        if code not in codes:
            continue
        cat = ai.categorize(a.get("title", ""), a.get("doc_type", "") or "")
        if cat["category"] not in EVENT_VIEW:
            continue
        view, label, weight = EVENT_VIEW[cat["category"]]
        out.setdefault(code, []).append(
            {
                "kind": cat["category"],
                "label": label,
                "view": view,
                "weight": weight,
                "date": a["date"],
                "title": a.get("title", "")[:60],
                "days_to_event": 0,
                "source": "公告",
            }
        )
    return out


def collect_events(codes: set[str], as_of: date) -> dict[str, list[dict]]:
    """所有事件（業績日 + 財技公告），按股票歸類。"""
    events = _ann_events(codes, as_of)

    for r in ec.upcoming(EVENT_HORIZON):
        code = r["stock_code"]
        if code not in codes:
            continue
        view, label, weight = EVENT_VIEW["results"]
        events.setdefault(code, []).append(
            {
                "kind": "results",
                "label": label + ("（有派息）" if r.get("has_dividend") else ""),
                "view": view,
                "weight": weight + (0.5 if r.get("confidence") == "high" else 0),
                "date": r["meeting_date"],
                "title": r.get("title", ""),
                "days_to_event": r["days_to_event"],
                "source": "業績日曆",
            }
        )
    return events


def _cross_map() -> dict[str, dict]:
    if not CROSS.exists():
        return {}
    return {r["stock_code"]: r for r in _load(CROSS)}


# ---------------------------------------------------------------- 方向綜合

def resolve_view(evs: list[dict], cross: dict | None) -> tuple[str, float, list[str]]:
    """夾埋事件方向 + CCASS／期權資金流，得出最終看法。"""
    notes = []
    up = sum(e["weight"] for e in evs if e["view"] == "up")
    down = sum(e["weight"] for e in evs if e["view"] == "down")
    pinned = sum(e["weight"] for e in evs if e["view"] == "pinned")
    neutral = sum(e["weight"] for e in evs if e["view"] == "neutral")

    if cross:
        bias, cscore = cross.get("bias"), cross.get("score") or 0
        if cscore >= 2 and bias == "看多":
            up += min(cscore / 2, 2.0)
            notes.append(f"資金流看多（分 {cscore}）")
        elif cscore >= 2 and bias == "看空":
            down += min(cscore / 2, 2.0)
            notes.append(f"資金流看空（分 {cscore}）")

    if pinned >= max(up, down, neutral):
        return "pinned", pinned, notes
    if up - down >= 1.0:
        return "up", up, notes
    if down - up >= 1.0:
        return "down", down, notes
    return "neutral", max(neutral, up, down), notes


# ---------------------------------------------------------------- 策略構建

def _leg(row: pd.Series, qty: int) -> dict | None:
    if row is None or pd.isna(row.get("settle")) or row["settle"] <= 0:
        return None
    return {
        "action": "買入" if qty > 0 else "賣出",
        "type": "Call" if row["type"] == "C" else "Put",
        "cp": row["type"],
        "strike": float(row["strike"]),
        "price": float(row["settle"]),
        "iv": None if pd.isna(row.get("iv")) else float(row["iv"]),
        "oi": int(row.get("oi") or 0),
        "volume": int(row.get("volume") or 0),
        "qty": qty,
    }


def _by_delta(df: pd.DataFrame, spot: float, t: float, target_delta: float,
              cp: str, atm_iv: float) -> pd.Series | None:
    """揀最接近目標 delta 嘅行使價（用該行使價自己嘅 IV，缺就用 ATM IV）。"""
    sub = df[(df.type == cp) & (df.oi >= MIN_OI) & (df.settle > 0)]
    if sub.empty:
        sub = df[(df.type == cp) & (df.settle > 0)]
    if sub.empty:
        return None
    best, gap = None, 9e9
    for _, r in sub.iterrows():
        vol = (r["iv"] if r.get("iv") and not pd.isna(r["iv"]) else atm_iv) / 100
        g = bs.greeks(spot, r["strike"], t, vol, r["type"])
        if not g:
            continue
        d = abs(g["delta"])
        if abs(d - target_delta) < gap:
            best, gap = r, abs(d - target_delta)
    return best


def _atm(df: pd.DataFrame, spot: float, cp: str) -> pd.Series | None:
    return oc.nearest(df, spot, cp, min_oi=MIN_OI)


def build_strategy(view: str, regime: str, df: pd.DataFrame, spot: float,
                   t: float, atm_iv: float) -> dict | None:
    """方向 × 波幅矩陣 → 具體腳（legs）。"""
    def leg_by_delta(d, cp, qty):
        return _leg(_by_delta(df, spot, t, d, cp, atm_iv), qty)

    def leg_atm(cp, qty):
        return _leg(_atm(df, spot, cp), qty)

    if view == "neutral" and regime == "cheap":
        legs = [leg_atm("C", 1), leg_atm("P", 1)]
        return {"name": "Long Straddle（買跨式）",
                "logic": "事件前波幅偏平，買 ATM Call + Put 賭大幅波動，唔賭方向",
                "legs": legs}

    if view == "neutral" and regime == "rich":
        sc, sp = leg_by_delta(0.20, "C", -1), leg_by_delta(0.20, "P", -1)
        lc, lp = leg_by_delta(0.08, "C", 1), leg_by_delta(0.08, "P", 1)
        if all([sc, sp, lc, lp]) and lc["strike"] > sc["strike"] > sp["strike"] > lp["strike"]:
            return {"name": "Iron Condor（鐵鷹）",
                    "logic": "波幅貴但要封頂風險：賣 0.2Δ 兩邊、買外面 0.08Δ 做保險",
                    "legs": [sp, sc, lp, lc]}
        return {"name": "Short Strangle（賣勒式）",
                "logic": "波幅貴，賣兩邊價外收時間值（無限風險，需保證金）",
                "legs": [sc, sp]}

    if view == "neutral" and regime == "fair":
        c, p = leg_by_delta(0.30, "C", 1), leg_by_delta(0.30, "P", 1)
        return {"name": "Long Strangle（買勒式）",
                "logic": "事件將至但波幅未見便宜，用價外雙邊降低成本",
                "legs": [c, p]}

    if view == "up" and regime in {"cheap", "fair"}:
        buy, sell = leg_atm("C", 1), leg_by_delta(0.22, "C", -1)
        if buy and sell and sell["strike"] > buy["strike"]:
            return {"name": "Bull Call Spread（牛市買權價差）",
                    "logic": "看升，買 ATM Call 賣價外 Call 攤成本、封最大蝕",
                    "legs": [buy, sell]}
        return {"name": "Long Call（買認購）", "logic": "看升，直接買 Call",
                "legs": [buy]}

    if view == "up" and regime == "rich":
        sell, buy = leg_by_delta(0.30, "P", -1), leg_by_delta(0.12, "P", 1)
        if sell and buy and sell["strike"] > buy["strike"]:
            return {"name": "Bull Put Spread（牛市賣權價差・收錢）",
                    "logic": "看升 + 波幅貴：收 Put 期權金，買下面 Put 封蝕",
                    "legs": [sell, buy]}
        return {"name": "Short Put（賣認沽）",
                "logic": "看升 + 波幅貴，賣價外 Put 收錢，跌穿就接貨",
                "legs": [sell]}

    if view == "down" and regime in {"cheap", "fair"}:
        buy, sell = leg_atm("P", 1), leg_by_delta(0.22, "P", -1)
        if buy and sell and sell["strike"] < buy["strike"]:
            return {"name": "Bear Put Spread（熊市賣權價差）",
                    "logic": "看跌，買 ATM Put 賣價外 Put 攤成本、封最大蝕",
                    "legs": [buy, sell]}
        return {"name": "Long Put（買認沽）", "logic": "看跌，直接買 Put",
                "legs": [buy]}

    if view == "down" and regime == "rich":
        sell, buy = leg_by_delta(0.30, "C", -1), leg_by_delta(0.12, "C", 1)
        if sell and buy and buy["strike"] > sell["strike"]:
            return {"name": "Bear Call Spread（熊市買權價差・收錢）",
                    "logic": "看跌 + 波幅貴：收 Call 期權金，買上面 Call 封蝕",
                    "legs": [sell, buy]}
        return {"name": "Short Call（賣認購）",
                "logic": "看跌 + 波幅貴，賣價外 Call 收錢（無限風險）",
                "legs": [sell]}

    if view == "pinned":
        sc, sp = leg_by_delta(0.15, "C", -1), leg_by_delta(0.15, "P", -1)
        lc, lp = leg_by_delta(0.06, "C", 1), leg_by_delta(0.06, "P", 1)
        if all([sc, sp, lc, lp]) and lc["strike"] > sc["strike"] > sp["strike"] > lp["strike"]:
            return {"name": "Iron Condor（要約釘價・收時間值）",
                    "logic": "要約／私有化令股價釘住，波幅必崩：窄鐵鷹收兩邊期權金",
                    "legs": [sp, sc, lp, lc]}
        return {"name": "Short Strangle（要約釘價）",
                "logic": "要約價封住上下空間，賣兩邊收期權金",
                "legs": [sc, sp]}
    return None


# ---------------------------------------------------------------- 盈虧評估

def _payoff(legs: list[dict], s: float) -> float:
    """到期每股盈虧（未扣成本）。"""
    v = 0.0
    for lg in legs:
        intr = max(s - lg["strike"], 0.0) if lg["cp"] == "C" else max(lg["strike"] - s, 0.0)
        v += lg["qty"] * intr
    return v


def evaluate(legs: list[dict], spot: float, t: float, real_vol: float,
             size: int) -> dict:
    """淨成本、盈虧平衡、最大賺蝕、勝率、期望值。

    勝率／期望值用 HV20（真實世界波幅、零漂移）做對數常態積分，
    唔用 IV — 咁樣 IV 貴嘅時候賣方策略自然贏面高，反映真實邊際。
    """
    net = sum(lg["qty"] * lg["price"] for lg in legs)      # >0 = 付錢
    vol = max(real_vol, 1e-4) / 100
    sd = vol * math.sqrt(max(t, 1e-6))

    lo = spot * math.exp(-5 * sd)
    hi = spot * math.exp(5 * sd)
    xs = [lo + (hi - lo) * i / (GRID - 1) for i in range(GRID)]

    tot = ev = win = 0.0
    pnl_min, pnl_max = 9e18, -9e18
    prev_pnl = None
    breakevens: list[float] = []
    for i, s in enumerate(xs):
        z = (math.log(s / spot) + 0.5 * vol * vol * t) / sd
        dens = math.exp(-0.5 * z * z) / (s * sd * math.sqrt(2 * math.pi))
        w = dens * ((hi - lo) / (GRID - 1))
        pnl = _payoff(legs, s) - net
        tot += w
        ev += pnl * w
        if pnl > 0:
            win += w
        pnl_min, pnl_max = min(pnl_min, pnl), max(pnl_max, pnl)
        if prev_pnl is not None and (prev_pnl < 0 <= pnl or prev_pnl > 0 >= pnl):
            breakevens.append(round((xs[i - 1] + s) / 2, 2))
        prev_pnl = pnl

    ev /= tot or 1
    win /= tot or 1

    # 淨 Greeks（用每腳自己嘅 IV）
    g_tot = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for lg in legs:
        iv = (lg.get("iv") or 0) / 100 or vol
        g = bs.greeks(spot, lg["strike"], t, iv, lg["cp"])
        for k in g_tot:
            g_tot[k] += lg["qty"] * g.get(k, 0.0)

    naked = any(lg["qty"] < 0 for lg in legs) and not _is_defined(legs)
    net_calls = sum(lg["qty"] for lg in legs if lg["cp"] == "C")
    open_upside = net_calls > 0
    return {
        "net_cost": round(net, 3),
        "net_cost_hkd": round(net * size, 0),
        "direction": "付出" if net > 0 else "收取",
        "breakevens": breakevens[:4],
        "max_profit": (None if open_upside or pnl_max > 8e17
                       else round(pnl_max * size, 0)),
        "max_loss": None if naked else round(pnl_min * size, 0),
        "unlimited_risk": naked,
        "win_rate": round(win * 100, 1),
        "ev_hkd": round(ev * size, 0),
        "ev_pct_risk": (round(ev * size / abs(pnl_min * size) * 100, 1)
                        if not naked and pnl_min < 0 else None),
        "greeks": {k: round(v, 4) for k, v in g_tot.items()},
        "contract_size": size,
    }


def _is_defined(legs: list[dict]) -> bool:
    """有冇同類別、更遠嘅 long 腳蓋住每個 short 腳。"""
    for lg in legs:
        if lg["qty"] >= 0:
            continue
        cover = [x for x in legs if x["qty"] > 0 and x["cp"] == lg["cp"] and
                 ((x["strike"] >= lg["strike"]) if lg["cp"] == "C"
                  else (x["strike"] <= lg["strike"]))]
        if not cover:
            return False
    return True


# ---------------------------------------------------------------- 主流程

def analyse(stock: str | None = None, as_of: str | None = None) -> list[dict]:
    iv_rows = {r["stock_code"]: r for r in iv_analyzer.analyse(as_of)}
    if not iv_rows:
        return []
    rpt = date.fromisoformat(next(iter(iv_rows.values()))["date"])
    codes = set(iv_rows)
    if stock:
        code = stock.zfill(5)
        if code not in codes:
            raise SystemExit(f"{code} 唔喺期權標的名單")
        codes = {code}

    events = collect_events(codes, rpt)
    cross = _cross_map()

    out: list[dict] = []
    for code, evs in events.items():
        iv_row = iv_rows.get(code)
        if not iv_row or not iv_row.get("iv") or not iv_row.get("close"):
            continue
        view, strength, notes = resolve_view(evs, cross.get(code))
        regime, regime_note = vol_regime(iv_row)

        ev_dtes = [e["days_to_event"] for e in evs]
        need = max(min(ev_dtes) if ev_dtes else 0, 0)
        exps = oc.expiries(code, rpt)
        cand = [e for e in exps if e["dte"] >= need + 3 and (e["oi"] or 0) > 200]
        if not cand:
            cand = [e for e in exps if e["dte"] >= need + 3]
        if not cand:
            continue
        exp = min(cand, key=lambda e: e["dte"])
        df = oc.chain(code, rpt, exp["expiry"])
        if df.empty:
            continue

        spot = float(iv_row["close"])
        t = bs.yearfrac(exp["dte"])
        atm_iv = float(iv_row["iv"])
        strat = build_strategy(view, regime, df, spot, t, atm_iv)
        if not strat or not strat["legs"] or any(l is None for l in strat["legs"]):
            continue

        size = contract_size(code)
        ev_res = evaluate(strat["legs"], spot, t, iv_row.get("hv20") or atm_iv, size)

        score = round(strength + (2 if regime != "fair" else 0)
                      + (ev_res["win_rate"] - 50) / 25
                      + (1.5 if (ev_res["ev_hkd"] or 0) > 0 else -1), 2)

        out.append(
            {
                "date": str(rpt),
                "stock_code": code,
                "name": iv_row["name"],
                "close": spot,
                "events": sorted(evs, key=lambda e: e["days_to_event"]),
                "view": {"up": "看升", "down": "看跌",
                         "neutral": "中性／賭波幅", "pinned": "釘價"}[view],
                "view_raw": view,
                "vol_regime": {"cheap": "波幅平", "rich": "波幅貴",
                               "fair": "波幅中性"}[regime],
                "vol_note": regime_note,
                "notes": notes,
                "iv": atm_iv,
                "iv_rank": iv_row.get("iv_rank"),
                "iv_hv": iv_row.get("iv_hv"),
                "hv20": iv_row.get("hv20"),
                "expiry": str(exp["expiry"]),
                "dte": int(exp["dte"]),
                "strategy": strat["name"],
                "logic": strat["logic"],
                "legs": strat["legs"],
                **ev_res,
                "score": score,
            }
        )

    out.sort(key=lambda r: -r["score"])
    return out


# ---------------------------------------------------------------- 輸出

def _s(v, f="{:.2f}"):
    return "—" if v is None else (f.format(v) if isinstance(v, (int, float)) else str(v))


def fmt_table(rows: list[dict], limit: int = 20) -> str:
    head = (f"{'代號':<7}{'名稱':<17}{'事件':<20}{'DTE':>5}{'看法':<12}"
            f"{'波幅':<10}{'策略':<30}{'勝率':>7}{'期望值':>9}{'分':>6}")
    lines = [head, "─" * 165]
    for r in rows[:limit]:
        ev0 = r["events"][0]
        lines.append(
            f"{r['stock_code']:<7}{(r['name'] or '')[:15]:<17}"
            f"{ev0['label'][:18]:<20}{r['dte']:>5}{r['view']:<12}"
            f"{r['vol_regime']:<10}{r['strategy'][:28]:<30}"
            f"{_s(r['win_rate'], '{:.0f}%'):>7}"
            f"{_s(r['ev_hkd'], '{:+,.0f}'):>9}{_s(r['score']):>6}"
        )
    return "\n".join(lines)


def detail(r: dict) -> str:
    L = [f"\n{'═'*78}",
         f"{r['stock_code']} {r['name']}   收市 {r['close']}   報告日 {r['date']}",
         f"{'═'*78}"]
    L.append("【事件】")
    for e in r["events"]:
        when = f"{e['date']}（{e['days_to_event']:+d} 日）" if e["days_to_event"] else e["date"]
        L.append(f"  · {e['label']}  {when}   來源：{e['source']}")
        if e.get("title"):
            L.append(f"      {e['title']}")
    L.append("")
    L.append(f"【判讀】看法 {r['view']}   {r['vol_note']}")
    for n in r["notes"]:
        L.append(f"          {n}")
    L.append("")
    L.append(f"【策略】{r['strategy']}   到期 {r['expiry']}（DTE {r['dte']}）")
    L.append(f"          {r['logic']}")
    L.append("")
    L.append(f"  {'動作':<6}{'類型':<7}{'行使價':>9}{'結算價':>9}{'IV':>6}{'未平倉':>9}")
    for lg in r["legs"]:
        L.append(f"  {lg['action']:<6}{lg['type']:<7}{lg['strike']:>9.2f}"
                 f"{lg['price']:>9.2f}{_s(lg['iv'], '{:.0f}'):>6}{lg['oi']:>9,}")
    L.append("")
    size = r["contract_size"]
    L.append(f"  合約股數      {size:,} 股／張")
    L.append(f"  淨{r['direction']}      HK$ {abs(r['net_cost_hkd']):,.0f}／張"
             f"（每股 {abs(r['net_cost']):.2f}）")
    if r["breakevens"]:
        L.append(f"  盈虧平衡      {' / '.join(f'{b:,.2f}' for b in r['breakevens'])}")
    mp = "無上限" if r["max_profit"] is None else f"HK$ {r['max_profit']:,.0f}"
    ml = "無限（需保證金）" if r["unlimited_risk"] else f"HK$ {abs(r['max_loss'] or 0):,.0f}"
    L.append(f"  最大賺／蝕    {mp}  ／  {ml}")
    L.append(f"  勝率          {r['win_rate']:.1f}%   （用 HV20 {r['hv20']}% 模擬）")
    L.append(f"  期望值        HK$ {r['ev_hkd']:+,.0f}／張"
             + (f"（佔風險 {r['ev_pct_risk']:+.1f}%）" if r.get("ev_pct_risk") else ""))
    g = r["greeks"]
    L.append(f"  淨 Greeks     Δ {g['delta']:+.3f}  Γ {g['gamma']:+.4f}"
             f"  Θ {g['theta']:+.3f}／日  V {g['vega']:+.3f}")
    L.append(f"  綜合分        {r['score']}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="事件驅動期權策略自動生成")
    ap.add_argument("--stock", help="只睇一隻，例如 00700")
    ap.add_argument("--date", help="報告日 YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--min-score", type=float, default=None)
    ap.add_argument("--detail", action="store_true", help="逐隻詳細列出")
    ap.add_argument("--json", action="store_true", help="寫 options_data/strategies.json")
    ap.add_argument("--stdout-json", action="store_true",
                    help="直接 print JSON（供 API 用）")
    a = ap.parse_args()

    rows = analyse(a.stock, a.date)
    if a.min_score is not None:
        rows = [r for r in rows if r["score"] >= a.min_score]

    if a.stdout_json:
        print(json.dumps(rows, ensure_ascii=False, default=str))
        return

    if a.json:
        OUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, default=str),
                            encoding="utf-8")
        print(f"已寫 {OUT_JSON}（{len(rows)} 條策略）")
        return

    if not rows:
        print("暫時冇符合條件嘅事件策略")
        return

    if a.stock or a.detail:
        for r in rows[: a.limit]:
            print(detail(r))
    else:
        print(f"\n=== 事件驅動期權策略   {rows[0]['date']}   共 {len(rows)} 條 ===\n")
        print(fmt_table(rows, a.limit))
        print(f"\n（睇詳情：python3 strategy_engine.py --stock <代號>）")


if __name__ == "__main__":
    main()
