"""event_strategy.py — 異動事件 → 期權策略自動生成。

把四樣嘢併埋一齊，自動揀策略、行使價、到期月：

  1. 事件      earnings_calendar（董事會業績日）+ announcement_indexer（配售／供股／
               盈警／盈喜／要約等財技動作）
  2. 波幅定價  iv_analyzer（IV rank／IV vs HV：市場對事件收貴定收平）
  3. 方向      ccass_options_cross（大戶歸邊 × 期權資金流）
  4. 合約      options_chain（逐個行使價結算價 + IV）+ bs.py（Greeks／概率）

核心判斷：市場隱含跳幅（ATM straddle ÷ 現價）vs 歷史同類事件實際跳幅中位數。
  · 隱含 < 歷史 → 期權收得平，買波幅（Long Straddle / Strangle）
  · 隱含 > 歷史 → 期權收得貴，賣波幅（Short Strangle / Iron Condor）
  · 有明確方向 → 換做方向性價差（Debit Spread / Short Put）

CLI:
    python3 event_strategy.py                 # 未來 45 日全部機會，按贏面排
    python3 event_strategy.py --stock 00700   # 單一隻嘅詳細建議
    python3 event_strategy.py --days 30 --json
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import announcement_indexer as ai
import bs
import earnings_calendar as ec
import iv_analyzer
import options_chain as oc

BASE = Path(__file__).parent
SPECS = BASE / "options_data" / "contract_specs.json"
CROSS = BASE / "options_data" / "ccass_options_cross.json"
QUOTES = BASE / "imported" / "quotes.json"
OUT = BASE / "options_data" / "event_strategies.json"

# 邊啲公告類別當「財技異動事件」，同佢隱含嘅方向偏好
EVENT_CATS = {
    "placing":        {"label": "配售／發新股", "dir": -1, "vol": +1, "window": 10},
    "rights_issue":   {"label": "供股／公開發售", "dir": -1, "vol": +1, "window": 15},
    "cb":             {"label": "可換股債券", "dir": -1, "vol": +1, "window": 10},
    "consolidation":  {"label": "合股／股本重組", "dir": -1, "vol": +1, "window": 15},
    "profit_warning": {"label": "盈利警告", "dir": -1, "vol": +1, "window": 7},
    "profit_alert":   {"label": "盈喜", "dir": +1, "vol": +1, "window": 7},
    "general_offer":  {"label": "要約收購", "dir": +1, "vol": -1, "window": 20},
    "privatization":  {"label": "私有化", "dir": +1, "vol": -1, "window": 20},
    "buyback":        {"label": "回購", "dir": +1, "vol": 0, "window": 5},
    "inside_info":    {"label": "內幕消息", "dir": 0, "vol": +1, "window": 7},
    "acquisition":    {"label": "收購", "dir": +1, "vol": +1, "window": 10},
    "merger":         {"label": "合併", "dir": 0, "vol": +1, "window": 10},
    "suspension":     {"label": "停牌", "dir": -1, "vol": +1, "window": 20},
    "resumption":     {"label": "復牌", "dir": 0, "vol": +1, "window": 10},
}

RESULT_DOCS = ("末期業績", "中期業績", "季度業績", "年度業績")
MIN_OI = 20          # 行使價最低未平倉，避免揀到冇流通嘅價
DELTA_SHORT = 0.22   # 賣方短腳目標 delta
DELTA_WING = 0.10    # Iron Condor 保護腳目標 delta


# ---------------------------------------------------------------- 載入

def _load(p: Path, default=None):
    if not p.exists():
        return default
    return json.loads(p.read_text())


def load_specs() -> dict:
    return _load(SPECS, {}) or {}


def load_cross() -> dict:
    rows = _load(CROSS, []) or []
    return {r["stock_code"]: r for r in rows}


def load_iv() -> dict:
    rows = iv_analyzer.analyse()
    return {r["stock_code"]: r for r in rows}


def load_closes() -> dict[str, pd.Series]:
    return iv_analyzer.load_closes()


# ---------------------------------------------------------------- 歷史事件跳幅

def past_result_dates(code: str, anns: list[dict], years: int = 3) -> list[str]:
    """由公告索引搵出過往業績公布日。"""
    cutoff = (date.today() - timedelta(days=365 * years)).isoformat()
    out = []
    for a in anns:
        if a["stock_code"] != code or a["date"] < cutoff:
            continue
        blob = f"{a.get('title', '')} {a.get('doc_type', '')}"
        if any(k in blob for k in RESULT_DOCS):
            out.append(a["date"])
    return sorted(set(out))


def historical_event_move(px: pd.Series, event_dates: list[str]) -> dict:
    """過往業績公布後第一個交易日嘅絕對變幅（%）中位數。"""
    moves = []
    if px is None or px.empty:
        return {}
    idx = px.index
    for d in event_dates:
        ts = pd.Timestamp(d)
        after = idx[idx > ts]
        before = idx[idx <= ts]
        if len(after) == 0 or len(before) == 0:
            continue
        p0, p1 = px.loc[before[-1]], px.loc[after[0]]
        if p0 and p1:
            moves.append(abs(p1 / p0 - 1) * 100)
    if not moves:
        return {}
    s = pd.Series(moves)
    return {
        "hist_move_median": round(float(s.median()), 2),
        "hist_move_max": round(float(s.max()), 2),
        "hist_move_n": len(s),
    }


# ---------------------------------------------------------------- 事件收集

def collect_events(codes: set[str], days: int, lookback: int) -> dict[str, list[dict]]:
    """每隻股票嘅事件清單（業績 + 財技公告）。"""
    events: dict[str, list[dict]] = {}
    today = date.today()

    for r in ec.upcoming(days):
        if r["stock_code"] not in codes:
            continue
        events.setdefault(r["stock_code"], []).append(
            {
                "kind": "earnings",
                "label": "業績公布",
                "event_date": r["meeting_date"],
                "dte": r["days_to_event"],
                "dir": 0,
                "vol": +1,
                "confidence": r["confidence"],
                "detail": r["title"],
                "link": r.get("link"),
            }
        )

    anns = ai._load("announcements.json")
    cutoff = (today - timedelta(days=lookback)).isoformat()
    for a in anns:
        code = a["stock_code"]
        if code not in codes or a["date"] < cutoff:
            continue
        cat = ai.categorize(a["title"], a.get("doc_type", ""))
        info = EVENT_CATS.get(cat["category"])
        if not info:
            continue
        age = (today - date.fromisoformat(a["date"])).days
        if age > info["window"]:
            continue
        events.setdefault(code, []).append(
            {
                "kind": cat["category"],
                "label": info["label"],
                "event_date": a["date"],
                "dte": -age,
                "dir": info["dir"],
                "vol": info["vol"],
                "confidence": "high",
                "detail": a["title"][:60],
                "link": a.get("file_link"),
            }
        )
    return events


# ---------------------------------------------------------------- 合約揀選

def _atm(df: pd.DataFrame, spot: float, cp: str):
    return oc.nearest(df, spot, cp, MIN_OI)


def _by_delta(df: pd.DataFrame, spot: float, cp: str, target: float,
              t: float, vol: float):
    """揀最接近目標 |delta| 嘅行使價。"""
    sub = df[df.type == cp]
    sub = sub[sub.oi >= MIN_OI] if (sub.oi >= MIN_OI).any() else sub
    if sub.empty:
        return None
    best, gap = None, 9e9
    for _, r in sub.iterrows():
        iv = (r.iv or vol * 100) / 100
        g = bs.greeks(spot, float(r.strike), t, iv, cp)
        if not g:
            continue
        d = abs(g["delta"])
        if abs(d - target) < gap:
            best, gap = r, abs(d - target)
    return best


def _leg(row, side: int, qty: int = 1) -> dict:
    return {
        "side": "買" if side > 0 else "賣",
        "type": "Call" if row.type == "C" else "Put",
        "strike": float(row.strike),
        "price": float(row.settle or 0),
        "iv": None if pd.isna(row.iv) else float(row.iv),
        "oi": int(row.oi or 0),
        "qty": qty,
        "_sign": side,
    }


def _net(legs: list[dict], size: int) -> float:
    """淨支出（正＝付錢／debit，負＝收錢／credit），每張合約 HKD。"""
    return sum(l["_sign"] * l["price"] * l["qty"] for l in legs) * size


# ---------------------------------------------------------------- 策略模板

def _straddle(df, spot, t, vol, size, long=True) -> dict | None:
    c, p = _atm(df, spot, "C"), _atm(df, spot, "P")
    if c is None or p is None:
        return None
    sign = 1 if long else -1
    legs = [_leg(c, sign), _leg(p, sign)]
    debit = _net(legs, size)
    width = abs(debit) / size
    lo, hi = float(p.strike) - width, float(c.strike) + width
    if long:
        pop = (bs.prob_below(spot, lo, t, vol) or 0) + (bs.prob_above(spot, hi, t, vol) or 0)
        maxl, maxp = abs(debit), None
    else:
        pop = 1 - ((bs.prob_below(spot, lo, t, vol) or 0) + (bs.prob_above(spot, hi, t, vol) or 0))
        maxl, maxp = None, abs(debit)
    return {
        "strategy": "Long Straddle" if long else "Short Straddle",
        "legs": legs, "net": round(debit, 0),
        "breakevens": [round(lo, 2), round(hi, 2)],
        "max_loss": None if maxl is None else round(maxl, 0),
        "max_profit": None if maxp is None else round(maxp, 0),
        "pop": round(pop * 100, 1),
    }


def _strangle(df, spot, t, vol, size, long=True, delta=None) -> dict | None:
    d = delta or (0.30 if long else DELTA_SHORT)
    c = _by_delta(df, spot, "C", d, t, vol)
    p = _by_delta(df, spot, "P", d, t, vol)
    if c is None or p is None or c.strike <= p.strike:
        return None
    sign = 1 if long else -1
    legs = [_leg(c, sign), _leg(p, sign)]
    net = _net(legs, size)
    w = abs(net) / size
    lo, hi = float(p.strike) - w, float(c.strike) + w
    if long:
        pop = (bs.prob_below(spot, lo, t, vol) or 0) + (bs.prob_above(spot, hi, t, vol) or 0)
        maxl, maxp = abs(net), None
    else:
        pop = 1 - ((bs.prob_below(spot, lo, t, vol) or 0) + (bs.prob_above(spot, hi, t, vol) or 0))
        maxl, maxp = None, abs(net)
    return {
        "strategy": "Long Strangle" if long else "Short Strangle",
        "legs": legs, "net": round(net, 0),
        "breakevens": [round(lo, 2), round(hi, 2)],
        "max_loss": None if maxl is None else round(maxl, 0),
        "max_profit": None if maxp is None else round(maxp, 0),
        "pop": round(pop * 100, 1),
    }


def _iron_condor(df, spot, t, vol, size) -> dict | None:
    sc = _by_delta(df, spot, "C", DELTA_SHORT, t, vol)
    sp = _by_delta(df, spot, "P", DELTA_SHORT, t, vol)
    lc = _by_delta(df, spot, "C", DELTA_WING, t, vol)
    lp = _by_delta(df, spot, "P", DELTA_WING, t, vol)
    if any(x is None for x in (sc, sp, lc, lp)):
        return None
    if not (lp.strike < sp.strike < sc.strike < lc.strike):
        return None
    legs = [_leg(sc, -1), _leg(lc, 1), _leg(sp, -1), _leg(lp, 1)]
    net = _net(legs, size)          # 負數＝收錢
    credit = -net
    if credit <= 0:
        return None
    cw = credit / size
    lo, hi = float(sp.strike) - cw, float(sc.strike) + cw
    width = max(float(lc.strike) - float(sc.strike), float(sp.strike) - float(lp.strike))
    pop = 1 - ((bs.prob_below(spot, lo, t, vol) or 0) + (bs.prob_above(spot, hi, t, vol) or 0))
    return {
        "strategy": "Iron Condor",
        "legs": legs, "net": round(net, 0),
        "breakevens": [round(lo, 2), round(hi, 2)],
        "max_profit": round(credit, 0),
        "max_loss": round(width * size - credit, 0),
        "pop": round(pop * 100, 1),
    }


def _vertical(df, spot, t, vol, size, bullish: bool, debit: bool) -> dict | None:
    """方向性價差。debit=True 買價差（Call/Put Debit Spread），False 賣價差。"""
    if bullish:
        long_cp, short_cp = "C", "C"
        ld, sd = (0.55, 0.28) if debit else (0.12, DELTA_SHORT)
    else:
        long_cp, short_cp = "P", "P"
        ld, sd = (0.55, 0.28) if debit else (0.12, DELTA_SHORT)

    lo_leg = _by_delta(df, spot, long_cp, ld, t, vol)
    sh_leg = _by_delta(df, spot, short_cp, sd, t, vol)
    if lo_leg is None or sh_leg is None or float(lo_leg.strike) == float(sh_leg.strike):
        return None
    legs = [_leg(lo_leg, 1), _leg(sh_leg, -1)]
    net = _net(legs, size)
    width = abs(float(sh_leg.strike) - float(lo_leg.strike)) * size
    if debit:
        if net <= 0:
            return None
        be = (float(lo_leg.strike) + net / size) if bullish else (float(lo_leg.strike) - net / size)
        pop = (bs.prob_above(spot, be, t, vol) if bullish else bs.prob_below(spot, be, t, vol)) or 0
        name = "Call Debit Spread" if bullish else "Put Debit Spread"
        maxp, maxl = width - net, net
    else:
        credit = -net
        if credit <= 0:
            return None
        be = (float(sh_leg.strike) - credit / size) if bullish else (float(sh_leg.strike) + credit / size)
        pop = (bs.prob_above(spot, be, t, vol) if bullish else bs.prob_below(spot, be, t, vol)) or 0
        name = "Put Credit Spread" if bullish else "Call Credit Spread"
        maxp, maxl = credit, width - credit
    return {
        "strategy": name,
        "legs": legs, "net": round(net, 0),
        "breakevens": [round(be, 2)],
        "max_profit": round(maxp, 0),
        "max_loss": round(maxl, 0),
        "pop": round(pop * 100, 1),
    }


def _short_put(df, spot, t, vol, size) -> dict | None:
    p = _by_delta(df, spot, "P", DELTA_SHORT, t, vol)
    if p is None:
        return None
    legs = [_leg(p, -1)]
    net = _net(legs, size)
    credit = -net
    if credit <= 0:
        return None
    be = float(p.strike) - credit / size
    pop = bs.prob_above(spot, be, t, vol) or 0
    return {
        "strategy": "Short Put（現金擔保）",
        "legs": legs, "net": round(net, 0),
        "breakevens": [round(be, 2)],
        "max_profit": round(credit, 0),
        "max_loss": round(be * size, 0),
        "pop": round(pop * 100, 1),
        "note": f"被行使即係 {be:.2f} 接貨",
    }


# ---------------------------------------------------------------- 策略決策

def choose(event: dict, ivrow: dict, cross: dict,
           implied_move: float | None, hist: dict) -> dict:
    """決定買波幅／賣波幅／方向性，同埋原因。"""
    reasons: list[str] = []
    ivr = ivrow.get("iv_rank")
    ratio = ivrow.get("iv_hv")
    score = ivrow.get("score", 0)

    hm = hist.get("hist_move_median")
    vol_edge = 0
    if implied_move is not None and hm:
        gap = implied_move - hm
        if gap <= -1.0:
            vol_edge = +1
            reasons.append(f"隱含跳幅 {implied_move:.1f}% < 歷史中位 {hm:.1f}% → 波幅收得平")
        elif gap >= 1.0:
            vol_edge = -1
            reasons.append(f"隱含跳幅 {implied_move:.1f}% > 歷史中位 {hm:.1f}% → 波幅收得貴")
        else:
            reasons.append(f"隱含跳幅 {implied_move:.1f}% ≈ 歷史中位 {hm:.1f}%")

    if score >= 3:
        vol_edge -= 1
        reasons.append(f"IV 極貴（IV/HV {ratio}，IV rank {ivr}）")
    elif score <= -3:
        vol_edge += 1
        reasons.append(f"IV 極平（IV/HV {ratio}，IV rank {ivr}）")
    elif ratio is not None:
        reasons.append(f"IV/HV {ratio}，IV rank {ivr}")

    bias = 0
    cb = (cross or {}).get("bias")
    cscore = (cross or {}).get("score") or 0
    if cb == "看多" and cscore >= 2:
        bias = +1
        reasons.append(f"CCASS×期權：{cb}（{'・'.join((cross.get('signals') or [])[:2])}）")
    elif cb == "看空" and cscore >= 2:
        bias = -1
        reasons.append(f"CCASS×期權：{cb}（{'・'.join((cross.get('signals') or [])[:2])}）")

    ev_dir = event.get("dir", 0)
    if ev_dir:
        bias = ev_dir if bias == 0 else (bias + ev_dir) // max(abs(bias + ev_dir), 1)
        reasons.append(f"事件方向偏{'多' if ev_dir > 0 else '空'}：{event['label']}")

    if event.get("vol", 0) < 0:
        vol_edge -= 1
        reasons.append("要約／私有化類事件 → 波幅會被壓死")

    return {"vol_edge": vol_edge, "bias": bias, "reasons": reasons}


def build(code: str, event: dict, ivrow: dict, cross: dict,
          px: pd.Series, anns: list[dict], specs: dict) -> dict | None:
    spot = ivrow.get("close")
    if not spot:
        return None
    size = int((specs.get(code) or {}).get("contract_size") or 0)
    if not size:
        return None

    ev_day = date.fromisoformat(event["event_date"])
    exps = oc.expiries(code)
    if not exps:
        return None
    after = [e for e in exps if e["expiry"] > ev_day and e["dte"] >= 3]
    pool = after or [e for e in exps if e["dte"] >= 3]
    if not pool:
        return None
    # 事件後第一個月，但要有起碼少少流通
    pool.sort(key=lambda e: (e["expiry"],))
    pick = next((e for e in pool if (e["oi"] or 0) >= 500), pool[0])
    exp, dte = pick["expiry"], int(pick["dte"])

    df = oc.chain(code, expiry=exp)
    if df.empty:
        return None
    t = bs.yearfrac(dte)
    atm_c, atm_p = _atm(df, spot, "C"), _atm(df, spot, "P")
    ivs = [x.iv for x in (atm_c, atm_p) if x is not None and not pd.isna(x.iv)]
    vol = (sum(ivs) / len(ivs) / 100) if ivs else ((ivrow.get("iv") or 30) / 100)

    straddle_px = None
    if atm_c is not None and atm_p is not None:
        straddle_px = float(atm_c.settle or 0) + float(atm_p.settle or 0)
    implied_move = bs.expected_move(straddle_px, spot) if straddle_px else None

    hist = historical_event_move(px, past_result_dates(code, anns)) \
        if event["kind"] == "earnings" else {}

    dec = choose(event, ivrow, cross, implied_move, hist)
    vol_edge, bias = dec["vol_edge"], dec["bias"]

    cands: list[dict] = []
    if vol_edge > 0:
        cands += [_straddle(df, spot, t, vol, size, True),
                  _strangle(df, spot, t, vol, size, True)]
        if bias:
            cands.append(_vertical(df, spot, t, vol, size, bias > 0, debit=True))
    elif vol_edge < 0:
        cands += [_iron_condor(df, spot, t, vol, size),
                  _strangle(df, spot, t, vol, size, False)]
        if bias > 0:
            cands += [_short_put(df, spot, t, vol, size),
                      _vertical(df, spot, t, vol, size, True, debit=False)]
        elif bias < 0:
            cands.append(_vertical(df, spot, t, vol, size, False, debit=False))
    else:
        if bias > 0:
            cands += [_vertical(df, spot, t, vol, size, True, debit=True),
                      _short_put(df, spot, t, vol, size)]
        elif bias < 0:
            cands.append(_vertical(df, spot, t, vol, size, False, debit=True))
        else:
            cands.append(_iron_condor(df, spot, t, vol, size))

    cands = [c for c in cands if c]
    if not cands:
        return None

    for c in cands:
        mp, ml = c.get("max_profit"), c.get("max_loss")
        pop = (c.get("pop") or 0) / 100
        if mp and ml:
            c["expectancy"] = round(pop * mp - (1 - pop) * ml, 0)
            c["rr"] = round(mp / ml, 2) if ml else None
        else:
            c["expectancy"] = None
            c["rr"] = None
        for l in c["legs"]:
            l.pop("_sign", None)
    cands.sort(key=lambda c: (-(c["expectancy"] if c["expectancy"] is not None else -1e9),
                              -(c.get("pop") or 0)))

    best = cands[0]
    edge_txt = {1: "買波幅", -1: "賣波幅", 0: "中性"}.get(
        max(-1, min(1, vol_edge)), "中性")
    return {
        "stock_code": code,
        "name": ivrow.get("name"),
        "close": spot,
        "contract_size": size,
        "event": {k: event[k] for k in
                  ("kind", "label", "event_date", "dte", "confidence", "detail")},
        "expiry": str(exp),
        "dte": dte,
        "atm_iv": round(vol * 100, 1),
        "iv_rank": ivrow.get("iv_rank"),
        "iv_hv": ivrow.get("iv_hv"),
        "hv20": ivrow.get("hv20"),
        "implied_move": None if implied_move is None else round(implied_move, 2),
        **hist,
        "stance": edge_txt,
        "bias": {1: "看多", -1: "看空", 0: "中性"}[max(-1, min(1, bias))],
        "reasons": dec["reasons"],
        "primary": best,
        "alternatives": cands[1:3],
    }


def scan(days: int = 45, lookback: int = 10,
         stock: str | None = None) -> list[dict]:
    ivmap = load_iv()
    specs = load_specs()
    cross = load_cross()
    closes = load_closes()
    anns = ai._load("announcements.json")

    codes = set(ivmap) & set(specs)
    if stock:
        codes &= {stock.zfill(5)}
        if not codes:
            raise SystemExit(f"{stock} 唔喺期權標的名單內（或今日冇 IV 數據）")

    events = collect_events(codes, days, lookback)
    out: list[dict] = []
    for code, evs in events.items():
        evs.sort(key=lambda e: abs(e["dte"]))
        ev = evs[0]
        try:
            rec = build(code, ev, ivmap[code], cross.get(code),
                        closes.get(code), anns, specs)
        except Exception as e:                       # 個別股票壞數據唔應該炸全個 scan
            rec = None
            if stock:
                raise
        if rec:
            rec["other_events"] = [
                {k: e[k] for k in ("label", "event_date", "dte")} for e in evs[1:4]
            ]
            out.append(rec)

    def key(r):
        p = r["primary"]
        return (-(p.get("expectancy") or -1e9), -(p.get("pop") or 0))
    out.sort(key=key)
    return out


# ---------------------------------------------------------------- 輸出

def _hk(v) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}"


def fmt_table(rows: list[dict], limit: int = 30) -> str:
    head = (f"{'代號':<7}{'名稱':<18}{'事件':<10}{'DTE':>5}{'到期':<12}"
            f"{'IV':>5}{'IVR':>6}{'隱含跳':>7}{'歷史跳':>7}  {'策略':<22}{'贏面':>6}{'期望值':>9}")
    lines = [head, "─" * 132]
    for r in rows[:limit]:
        p = r["primary"]
        lines.append(
            f"{r['stock_code']:<7}{(r['name'] or '')[:16]:<18}"
            f"{r['event']['label'][:8]:<10}{r['event']['dte']:>5}{r['expiry']:<12}"
            f"{r['atm_iv']:>5.0f}{(r['iv_rank'] or 0):>6.0f}"
            f"{(f'{r[chr(105)+chr(109)+chr(112)+chr(108)+chr(105)+chr(101)+chr(100)+chr(95)+chr(109)+chr(111)+chr(118)+chr(101)]:.1f}%' if r.get('implied_move') else '—'):>7}"
            f"{(f'{r[chr(104)+chr(105)+chr(115)+chr(116)+chr(95)+chr(109)+chr(111)+chr(118)+chr(101)+chr(95)+chr(109)+chr(101)+chr(100)+chr(105)+chr(97)+chr(110)]:.1f}%' if r.get('hist_move_median') else '—'):>7}"
            f"  {p['strategy']:<22}{p['pop']:>5.0f}%{_hk(p.get('expectancy')):>9}"
        )
    return "\n".join(lines)


def detail(r: dict) -> str:
    p = r["primary"]
    L = [
        f"{r['stock_code']} {r['name']}   現價 {r['close']}   每張 {r['contract_size']:,} 股",
        f"事件      {r['event']['label']}  {r['event']['event_date']}"
        f"（{'仲有' if r['event']['dte'] >= 0 else '已過'} {abs(r['event']['dte'])} 日"
        f"，可信度 {r['event']['confidence']}）",
        f"          {r['event']['detail']}",
        f"到期月    {r['expiry']}  DTE {r['dte']}   ATM IV {r['atm_iv']}%"
        f"   HV20 {r['hv20']}   IV/HV {r['iv_hv']}   IV rank {r['iv_rank']}",
    ]
    if r.get("implied_move"):
        h = (f"   歷史中位 {r['hist_move_median']}%（{r['hist_move_n']} 次，最大 "
             f"{r['hist_move_max']}%）" if r.get("hist_move_median") else "")
        L.append(f"跳幅      市場隱含 {r['implied_move']}%{h}")
    L.append(f"判斷      【{r['stance']}・{r['bias']}】")
    for x in r["reasons"]:
        L.append(f"          · {x}")
    L.append("")
    L.append(f"主策略    {p['strategy']}")
    for l in p["legs"]:
        L.append(f"          {l['side']} {l['type']:<4} {l['strike']:>9.2f}  "
                 f"價 {l['price']:>8.2f}  IV {l['iv'] or '—':>4}  未平倉 {l['oi']:>7,}")
    net = p["net"]
    L.append(f"          淨{'支出' if net > 0 else '收入'} HK${abs(net):,.0f}"
             f"   保本點 {' / '.join(str(b) for b in p['breakevens'])}")
    L.append(f"          最大賺 {_hk(p.get('max_profit'))}   最大蝕 {_hk(p.get('max_loss'))}"
             f"   贏面 {p['pop']}%   期望值 {_hk(p.get('expectancy'))}"
             + (f"   賺蝕比 {p['rr']}" if p.get("rr") else ""))
    if p.get("note"):
        L.append(f"          {p['note']}")
    if r["alternatives"]:
        L.append("")
        L.append("替代方案  " + " ｜ ".join(
            f"{a['strategy']}（贏面 {a['pop']}%，期望 {_hk(a.get('expectancy'))}）"
            for a in r["alternatives"]))
    if r.get("other_events"):
        L.append("其他事件  " + " ｜ ".join(
            f"{e['label']} {e['event_date']}" for e in r["other_events"]))
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="異動事件 → 期權策略自動生成")
    ap.add_argument("--days", type=int, default=45, help="向前睇幾多日事件")
    ap.add_argument("--lookback", type=int, default=10, help="已發生公告向後睇幾多日")
    ap.add_argument("--stock", help="只做一隻，出詳細建議")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--json", action="store_true", help="寫 options_data/event_strategies.json")
    a = ap.parse_args()

    rows = scan(a.days, a.lookback, a.stock)
    if a.json:
        OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str))
        print(f"已寫 {OUT}（{len(rows)} 個機會）")
        return

    if a.stock:
        if not rows:
            print(f"{a.stock} 未來 {a.days} 日內冇捕捉到事件")
            return
        print(detail(rows[0]))
        return

    print(f"\n=== 異動事件期權策略  未來 {a.days} 日 / 已發生 {a.lookback} 日內 ===\n")
    print(fmt_table(rows, a.limit))
    print(f"\n共 {len(rows)} 個機會。想睇單隻詳細： python3 event_strategy.py --stock 00700")


if __name__ == "__main__":
    main()
