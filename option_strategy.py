"""option_strategy.py — 異動事件 → 期權策略自動生成。

輸入（全部係我哋自己已有嘅數據）：
  · earnings_calendar.py     未來業績日（由董事會公告 parse 出）
  · announcement_indexer.py  財技事件（配售／供股／盈警／盈喜／要約／私有化…）
  · iv_analyzer.py           IV 貴定平（IV rank / IV vs HV）
  · ccass_options_cross.py   CCASS 歸邊 × 期權資金流方向
  · options_chain.py         逐個行使價嘅真實結算價
  · bs.py                    Greeks / 概率

輸出：每隻股票一個具體可落單嘅策略——真實行使價、真實到期月、
用 HKEX 結算價計嘅成本、盈虧平衡、最大賺蝕、贏面（POP）。

判斷邏輯（兩個軸）：
  方向軸  bull / bear / neutral   ← 事件性質 + CCASS 歸邊 + 期權資金流
  波幅軸  long vol / short vol    ← IV rank + IV/HV + 事件前後

  方向 × 波幅 → 策略：
    看多 + IV 平   → Long Call / Bull Call Spread（借記）
    看多 + IV 貴   → Short Put / Bull Put Spread（貸記，收 premium）
    看空 + IV 平   → Bear Put Spread（借記）
    看空 + IV 貴   → Bear Call Spread（貸記）
    中性 + IV 平   → Long Straddle / Strangle（等大跳）
    中性 + IV 貴   → Iron Condor / Short Strangle（收時間值）

CLI:
    python3 option_strategy.py                  # 全市場掃描
    python3 option_strategy.py --stock 00700    # 單隻詳細
    python3 option_strategy.py --events         # 只睇有事件嘅
    python3 option_strategy.py --json
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import announcement_indexer as ai
import bs
import earnings_calendar as ec
import options_chain as oc

BASE = Path(__file__).parent
DATA = BASE / "options_data"
SPECS = DATA / "contract_specs.json"
IV_JSON = DATA / "iv_analysis.json"
CROSS_JSON = DATA / "ccass_options_cross.json"
OUT_JSON = DATA / "strategies.json"

# 事件性質 → 方向偏見 + 波幅偏見
EVENT_BIAS = {
    "profit_warning": (-2, +2, "盈警"),
    "profit_alert": (+2, +1, "盈喜"),
    "rights_issue": (-2, +2, "供股／公開發售"),
    "placing": (-2, +1, "配售攤薄"),
    "cb": (-1, +1, "可換股債券"),
    "consolidation": (-1, +2, "合股／股本重組"),
    "general_offer": (+2, -2, "要約收購"),
    "privatization": (+2, -2, "私有化"),
    "merger": (+1, +1, "合併"),
    "acquisition": (+1, +1, "收購"),
    "restructuring": (0, +2, "重組"),
    "suspension": (0, +2, "停牌"),
    "resumption": (0, +2, "復牌"),
    "buyback": (+1, -1, "回購"),
    "inside_info": (0, +2, "內幕消息"),
    "delisting": (-2, +2, "除牌"),
}
EVENT_LOOKBACK = 10          # 近 N 日嘅公告當「新鮮事件」
MIN_OI = 20                  # 行使價流通性門檻
MIN_DTE, MAX_DTE = 15, 80


def _load(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def _specs() -> dict:
    return _load(SPECS) or {}


# ---------------------------------------------------------------- 事件收集

def recent_events(codes: set[str], days: int = EVENT_LOOKBACK) -> dict[str, list[dict]]:
    """近 N 日嘅財技公告，只留有方向／波幅含意嘅類別。"""
    anns = ai._load("announcements.json")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    out: dict[str, list[dict]] = {}
    for a in anns:
        if a["date"] < cutoff or a["stock_code"] not in codes:
            continue
        cat = ai.categorize(a["title"], a.get("doc_type", ""))
        if cat["category"] not in EVENT_BIAS:
            continue
        out.setdefault(a["stock_code"], []).append(
            {"date": a["date"], "category": cat["category"],
             "label": cat["label"], "title": a["title"]}
        )
    for v in out.values():
        v.sort(key=lambda x: x["date"], reverse=True)
    return out


# ---------------------------------------------------------------- 觀點形成

def build_view(iv: dict, cross: dict | None, events: list[dict],
               earn: dict | None) -> dict:
    """夾埋所有訊號，出一個 direction / vol 觀點。"""
    dir_score = 0.0
    vol_score = 0.0
    why: list[str] = []

    # 1. IV 貴平（iv_analyzer 嘅 score：正＝貴）
    iv_score = iv.get("score") or 0
    vol_score -= iv_score                        # IV 貴 → 傾向賣波幅
    rank, ratio = iv.get("iv_rank"), iv.get("iv_hv")
    if iv_score >= 2:
        why.append(f"IV 偏貴（IVR {rank}・IV/HV {ratio}）→ 賣方有利")
    elif iv_score <= -2:
        why.append(f"IV 偏平（IVR {rank}・IV/HV {ratio}）→ 買方有利")

    # 2. CCASS × 期權資金流方向
    if cross:
        cs_score = cross.get("score") or 0
        if cross.get("bias") == "看多":
            dir_score += min(cs_score, 4) * 0.6
        elif cross.get("bias") == "看空":
            dir_score -= min(cs_score, 4) * 0.6
        if cs_score >= 2 and cross.get("signals"):
            why.append("・".join(cross["signals"][:2]))

    # 3. 財技事件
    for e in events[:3]:
        d, v, tag = EVENT_BIAS[e["category"]]
        age = (date.today() - date.fromisoformat(e["date"])).days
        decay = 1.0 if age <= 3 else 0.6 if age <= 7 else 0.35
        dir_score += d * decay
        vol_score += v * decay
        why.append(f"{e['date']} {tag}")

    # 4. 業績前 = 波幅事件
    if earn:
        dte = earn["days_to_event"]
        vol_score += 2.5 if dte <= 21 else 1.5
        why.append(f"{earn['meeting_date']} 業績（{dte} 日後）")

    direction = "看多" if dir_score >= 1.2 else "看空" if dir_score <= -1.2 else "中性"
    vol_view = "買波幅" if vol_score >= 1.5 else "賣波幅" if vol_score <= -1.5 else "無偏"
    return {
        "direction": direction,
        "vol_view": vol_view,
        "dir_score": round(dir_score, 1),
        "vol_score": round(vol_score, 1),
        "why": why,
    }


# ---------------------------------------------------------------- 行使價揀選

def _with_greeks(df: pd.DataFrame, spot: float) -> pd.DataFrame:
    df = df.copy()
    deltas, ivs = [], []
    for _, r in df.iterrows():
        vol = (r["iv"] or 0) / 100
        t = bs.yearfrac(int(r["dte"]))
        g = bs.greeks(spot, r["strike"], t, vol, r["type"]) if vol > 0 else {}
        deltas.append(g.get("delta"))
        ivs.append(vol)
    df["delta"] = deltas
    df["vol"] = ivs
    return df


def _pick(df: pd.DataFrame, cp: str, target_delta: float) -> pd.Series | None:
    """揀最接近目標 delta 嘅合約（要有結算價 + 基本流通）。"""
    sub = df[(df.type == cp) & df.delta.notna() & (df.settle > 0)]
    if sub.empty:
        return None
    liq = sub[sub.oi >= MIN_OI]
    sub = liq if not liq.empty else sub
    return sub.iloc[(sub.delta.abs() - abs(target_delta)).abs().argsort().iloc[0]]


def _atm(df: pd.DataFrame, cp: str, spot: float) -> pd.Series | None:
    sub = df[(df.type == cp) & (df.settle > 0)]
    if sub.empty:
        return None
    liq = sub[sub.oi >= MIN_OI]
    sub = liq if not liq.empty else sub
    return sub.iloc[(sub.strike - spot).abs().argsort().iloc[0]]


def _leg(row: pd.Series, qty: int) -> dict:
    return {
        "action": "買入" if qty > 0 else "賣出",
        "qty": qty,
        "type": "Call" if row["type"] == "C" else "Put",
        "strike": float(row["strike"]),
        "expiry": str(row["expiry"]),
        "price": float(row["settle"]),
        "iv": None if pd.isna(row["iv"]) else float(row["iv"]),
        "delta": None if row["delta"] is None else round(float(row["delta"]), 3),
        "oi": int(row["oi"] or 0),
    }


# ---------------------------------------------------------------- 策略構造

def _payoff(legs: list[dict], size: int, spot: float) -> dict:
    """用到期損益格計最大賺／蝕 + 盈虧平衡點。"""
    strikes = sorted({l["strike"] for l in legs})
    lo, hi = strikes[0] * 0.5, strikes[-1] * 1.6
    grid = [lo + (hi - lo) * i / 1200 for i in range(1201)]
    net = sum(-l["qty"] * l["price"] for l in legs)   # 正＝收錢（貸記）

    def pnl(s: float) -> float:
        v = net
        for l in legs:
            intr = max(0.0, s - l["strike"]) if l["type"] == "Call" else max(0.0, l["strike"] - s)
            v += l["qty"] * intr
        return v

    vals = [(s, pnl(s)) for s in grid]
    max_p = max(v for _, v in vals)
    max_l = min(v for _, v in vals)
    bes = []
    for i in range(1, len(vals)):
        a, b = vals[i - 1], vals[i]
        if (a[1] <= 0 < b[1]) or (a[1] >= 0 > b[1]):
            span = b[1] - a[1]
            x = a[0] if span == 0 else a[0] + (0 - a[1]) / span * (b[0] - a[0])
            bes.append(round(x, 2))
    return {
        "net": round(net * size, 0),
        "net_per_share": round(net, 3),
        "max_profit": round(max_p * size, 0) if max_p < 1e8 else None,
        "max_loss": round(max_l * size, 0) if max_l > -1e8 else None,
        "breakevens": bes[:4],
        "credit": net > 0,
    }


def _pop(legs: list[dict], payoff: dict, spot: float, vol: float, dte: int) -> float | None:
    """贏面：用 ATM IV 嘅對數常態分佈，計到期落喺賺錢區嘅概率。"""
    if not vol or dte <= 0 or not payoff["breakevens"]:
        return None
    t = bs.yearfrac(dte)
    bes = payoff["breakevens"]
    if len(bes) == 1:
        b = bes[0]
        profit_up = payoff["max_profit"] is None or _sign_above(legs, payoff, b)
        p = bs.prob_above(spot, b, t, vol) if profit_up else bs.prob_below(spot, b, t, vol)
        return None if p is None else round(p * 100, 1)
    lo, hi = bes[0], bes[-1]
    inside = (bs.prob_below(spot, hi, t, vol) or 0) - (bs.prob_below(spot, lo, t, vol) or 0)
    mid = (lo + hi) / 2
    win_inside = _pnl_at(legs, payoff, mid) > 0
    p = inside if win_inside else 1 - inside
    return round(max(0.0, min(1.0, p)) * 100, 1)


def _pnl_at(legs: list[dict], payoff: dict, s: float) -> float:
    v = payoff["net_per_share"]
    for l in legs:
        intr = max(0.0, s - l["strike"]) if l["type"] == "Call" else max(0.0, l["strike"] - s)
        v += l["qty"] * intr
    return v


def _sign_above(legs: list[dict], payoff: dict, be: float) -> bool:
    return _pnl_at(legs, payoff, be * 1.15) > 0


STRATEGY_BOOK = {
    ("看多", "買波幅"): "long_call",
    ("看多", "無偏"): "bull_call_spread",
    ("看多", "賣波幅"): "bull_put_spread",
    ("看空", "買波幅"): "long_put",
    ("看空", "無偏"): "bear_put_spread",
    ("看空", "賣波幅"): "bear_call_spread",
    ("中性", "買波幅"): "long_straddle",
    ("中性", "賣波幅"): "iron_condor",
    ("中性", "無偏"): None,
}


def construct(chain: pd.DataFrame, kind: str, spot: float) -> list[dict] | None:
    """按策略名喺真實期權鏈揀腳。"""
    if kind == "long_call":
        c = _atm(chain, "C", spot)
        return [_leg(c, 1)] if c is not None else None
    if kind == "long_put":
        p = _atm(chain, "P", spot)
        return [_leg(p, 1)] if p is not None else None
    if kind == "bull_call_spread":
        lo, hi = _atm(chain, "C", spot), _pick(chain, "C", 0.25)
        if lo is None or hi is None or hi.strike <= lo.strike:
            return None
        return [_leg(lo, 1), _leg(hi, -1)]
    if kind == "bear_put_spread":
        hi, lo = _atm(chain, "P", spot), _pick(chain, "P", 0.25)
        if lo is None or hi is None or lo.strike >= hi.strike:
            return None
        return [_leg(hi, 1), _leg(lo, -1)]
    if kind == "bull_put_spread":
        short, long = _pick(chain, "P", 0.30), _pick(chain, "P", 0.12)
        if short is None or long is None or long.strike >= short.strike:
            return None
        return [_leg(short, -1), _leg(long, 1)]
    if kind == "bear_call_spread":
        short, long = _pick(chain, "C", 0.30), _pick(chain, "C", 0.12)
        if short is None or long is None or long.strike <= short.strike:
            return None
        return [_leg(short, -1), _leg(long, 1)]
    if kind == "long_straddle":
        c, p = _atm(chain, "C", spot), _atm(chain, "P", spot)
        if c is None or p is None:
            return None
        if abs(c.strike - p.strike) > spot * 0.03:
            return [_leg(c, 1), _leg(p, 1)]
        return [_leg(c, 1), _leg(p, 1)]
    if kind == "iron_condor":
        sp, lp = _pick(chain, "P", 0.20), _pick(chain, "P", 0.09)
        sc, lc = _pick(chain, "C", 0.20), _pick(chain, "C", 0.09)
        if None in (sp, lp, sc, lc):
            return None
        if not (lp.strike < sp.strike < sc.strike < lc.strike):
            return None
        return [_leg(sp, -1), _leg(lp, 1), _leg(sc, -1), _leg(lc, 1)]
    return None


STRATEGY_LABEL = {
    "long_call": "Long Call（買升）",
    "long_put": "Long Put（買跌）",
    "bull_call_spread": "Bull Call Spread（牛市買權差價）",
    "bear_put_spread": "Bear Put Spread（熊市沽權差價）",
    "bull_put_spread": "Bull Put Spread（沽權收租）",
    "bear_call_spread": "Bear Call Spread（沽買權收租）",
    "long_straddle": "Long Straddle（買跨式・等大跳）",
    "iron_condor": "Iron Condor（鐵鷹・收時間值）",
}


# ---------------------------------------------------------------- 到期月揀選

def choose_expiry(chain_all: pd.DataFrame, event_date: str | None) -> date | None:
    """有事件就揀事件後第一個到期月，否則揀 DTE 15-80 之中最流通嘅。"""
    exps = (chain_all.groupby(["expiry", "dte"])
            .agg(oi=("oi", "sum")).reset_index().sort_values("expiry"))
    if exps.empty:
        return None
    if event_date:
        ev = date.fromisoformat(event_date)
        after = exps[exps.expiry.apply(lambda e: e > ev + timedelta(days=1))]
        if not after.empty:
            return after.iloc[0]["expiry"]
    cand = exps[(exps.dte >= MIN_DTE) & (exps.dte <= MAX_DTE)]
    if cand.empty:
        cand = exps[exps.dte >= 7]
    if cand.empty:
        return None
    return cand.loc[cand.oi.idxmax(), "expiry"]


# ---------------------------------------------------------------- 主流程

def analyse(stock: str | None = None, events_only: bool = False,
            as_of: date | None = None) -> list[dict]:
    ivs = {r["stock_code"]: r for r in (_load(IV_JSON) or [])}
    cross = {r["stock_code"]: r for r in (_load(CROSS_JSON) or [])}
    specs = _specs()
    if not ivs:
        raise SystemExit("未見 iv_analysis.json，先跑 iv_analyzer.py --json")

    codes = {stock.zfill(5)} if stock else set(ivs)
    ev_map = recent_events(codes)
    earn_map = ec.event_map(120)

    as_of = as_of or oc.latest_raw()
    all_chains = oc.parse_chains(as_of, stock.zfill(5) if stock else None)
    if all_chains.empty:
        raise SystemExit("未見期權鏈 raw 檔，先跑 options_scraper.py")
    by_code = {c: g for c, g in all_chains.groupby("stock_code")}

    out: list[dict] = []
    for code in sorted(codes):
        iv = ivs.get(code)
        chain_all = by_code.get(code)
        if iv is None or chain_all is None or chain_all.empty:
            continue
        events = ev_map.get(code, [])
        earn = earn_map.get(code)
        if events_only and not events and not earn:
            continue

        view = build_view(iv, cross.get(code), events, earn)
        kind = STRATEGY_BOOK.get((view["direction"], view["vol_view"]))
        spot = float(iv.get("close") or chain_all.iloc[0]["close"] or 0)
        if not spot:
            continue

        rec = {
            "stock_code": code,
            "name": iv.get("name"),
            "close": spot,
            "date": str(chain_all.iloc[0]["date"]),
            "iv": iv.get("iv"),
            "iv_rank": iv.get("iv_rank"),
            "iv_hv": iv.get("iv_hv"),
            "hv20": iv.get("hv20"),
            "iv_verdict": iv.get("verdict"),
            "events": events[:3],
            "earnings": ({"date": earn["meeting_date"], "dte": earn["days_to_event"],
                          "confidence": earn["confidence"]} if earn else None),
            **view,
        }

        if kind is None:
            rec.update({"strategy": None, "strategy_label": "觀望（訊號不足）"})
            out.append(rec)
            continue

        exp = choose_expiry(chain_all, earn["meeting_date"] if earn else None)
        if exp is None:
            continue
        chain = _with_greeks(chain_all[chain_all.expiry == exp], spot)
        legs = construct(chain, kind, spot)
        if not legs:
            rec.update({"strategy": None, "strategy_label": "無合適行使價（流通不足）"})
            out.append(rec)
            continue

        size = int(specs.get(code, {}).get("contract_size") or 1000)
        dte = int(chain.iloc[0]["dte"])
        pay = _payoff(legs, size, spot)
        atm = _atm(chain, "C", spot)
        atm_vol = float(atm["iv"] or 0) / 100 if atm is not None else 0
        pop = _pop(legs, pay, spot, atm_vol, dte)

        straddle_c, straddle_p = _atm(chain, "C", spot), _atm(chain, "P", spot)
        exp_move = None
        if straddle_c is not None and straddle_p is not None:
            exp_move = bs.expected_move(
                float(straddle_c["settle"]) + float(straddle_p["settle"]), spot)

        rec.update({
            "strategy": kind,
            "strategy_label": STRATEGY_LABEL[kind],
            "expiry": str(exp),
            "dte": dte,
            "contract_size": size,
            "legs": legs,
            **pay,
            "pop": pop,
            "expected_move_pct": round(exp_move, 1) if exp_move else None,
            "hv_move_pct": round(bs.vol_to_move(iv.get("hv20") or 0, dte) or 0, 1) or None,
            "risk_reward": (round(abs(pay["max_profit"] / pay["max_loss"]), 2)
                            if pay["max_profit"] and pay["max_loss"] else None),
        })
        out.append(rec)

    def rank(r: dict) -> tuple:
        has_ev = bool(r["events"]) or bool(r["earnings"])
        return (not has_ev, -(r.get("pop") or 0), -abs(r.get("dir_score") or 0))

    out.sort(key=rank)
    return out


# ---------------------------------------------------------------- 輸出

def _m(v) -> str:
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1_000_000:
        return f"{v/1_000_000:,.2f}M"
    if a >= 1000:
        return f"{v/1000:,.1f}K"
    return f"{v:,.0f}"


def fmt_table(rows: list[dict], limit: int = 25) -> str:
    head = (f"{'代號':<7}{'名稱':<18}{'觀點':<11}{'策略':<26}"
            f"{'到期':<12}{'成本／收':>10}{'贏面':>7}{'RR':>6}  觸發")
    lines = [head, "─" * 168]
    for r in rows[:limit]:
        if not r.get("strategy"):
            continue
        view = f"{r['direction']}/{r['vol_view']}"
        net = r["net"]
        money = f"收 {_m(net)}" if r["credit"] else f"付 {_m(abs(net))}"
        why = "・".join(r["why"][:2])[:48]
        lines.append(
            f"{r['stock_code']:<7}{(r['name'] or '')[:16]:<18}{view:<11}"
            f"{STRATEGY_LABEL[r['strategy']][:24]:<26}"
            f"{r['expiry']:<12}{money:>10}"
            f"{(str(r['pop']) + '%') if r['pop'] else '—':>7}"
            f"{r['risk_reward'] or '—':>6}  {why}"
        )
    return "\n".join(lines)


def detail(r: dict) -> str:
    L = [
        f"\n{r['stock_code']} {r['name']}   收市 {r['close']}   報告日 {r['date']}",
        f"IV {r['iv']}%  IVR {r['iv_rank']}  IV/HV {r['iv_hv']}  HV20 {r['hv20']}%  → {r['iv_verdict']}",
        f"觀點：{r['direction']} / {r['vol_view']}"
        f"（方向分 {r['dir_score']}・波幅分 {r['vol_score']}）",
    ]
    for w in r["why"]:
        L.append(f"  · {w}")
    if not r.get("strategy"):
        L.append(f"\n→ {r['strategy_label']}")
        return "\n".join(L)

    L.append(f"\n策略：{r['strategy_label']}")
    L.append(f"到期 {r['expiry']}（{r['dte']} 日）  每張 {r['contract_size']:,} 股")
    L.append(f"{'':<4}{'動作':<6}{'類型':<6}{'行使價':>9}{'結算價':>9}{'IV':>6}{'Delta':>8}{'未平倉':>9}")
    for l in r["legs"]:
        L.append(f"{'':<4}{l['action']:<6}{l['type']:<6}{l['strike']:>9.2f}"
                 f"{l['price']:>9.2f}{(l['iv'] or 0):>6.0f}{l['delta'] or 0:>8.3f}"
                 f"{l['oi']:>9,}")
    money = f"淨收 {_m(r['net'])}" if r["credit"] else f"淨付 {_m(abs(r['net']))}"
    L.append(f"\n{money}   最大賺 {_m(r['max_profit'])}   最大蝕 {_m(r['max_loss'])}"
             f"   風險回報 {r['risk_reward'] or '—'}")
    L.append(f"盈虧平衡：{'、'.join(f'{b:,.2f}' for b in r['breakevens']) or '—'}")
    L.append(f"贏面 (POP) {r['pop']}%"
             f"   市場隱含跳幅 ±{r['expected_move_pct']}%"
             f"   歷史波幅同期 ±{r['hv_move_pct']}%")
    if r["earnings"]:
        L.append(f"業績日 {r['earnings']['date']}（{r['earnings']['dte']} 日後，"
                 f"可信度 {r['earnings']['confidence']}）→ 已揀事件後到期月")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="異動事件 → 期權策略自動生成")
    ap.add_argument("--stock", help="單隻，例如 00700")
    ap.add_argument("--events", action="store_true", help="只出有事件／業績嘅")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--date", help="報告日 YYYY-MM-DD")
    a = ap.parse_args()

    as_of = date.fromisoformat(a.date) if a.date else None
    rows = analyse(a.stock, a.events, as_of)

    if a.json:
        OUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, default=str))
        print(f"已寫 {OUT_JSON}（{len(rows)} 隻）")
        return
    if a.stock:
        print(detail(rows[0]) if rows else "冇數據")
        return

    n_ev = sum(1 for r in rows if r["events"] or r["earnings"])
    print(f"\n=== 期權策略自動生成   報告日 {rows[0]['date'] if rows else '—'}   "
          f"{len(rows)} 隻掃描・{n_ev} 隻有事件 ===\n")
    print(fmt_table(rows, a.limit))
    print("\n（單隻詳細：python3 option_strategy.py --stock 00700）")


if __name__ == "__main__":
    main()
