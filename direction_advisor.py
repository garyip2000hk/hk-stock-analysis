"""direction_advisor.py — 方向揀策略：輸入股票＋睇法（買升／窄幅／買跌），
逐個候選策略計勝率＋期望值，排出最優惠嘅期權買法。

同 strategy_engine 嘅分別：
  · strategy_engine 由「事件」推方向；呢度由「用戶揀嘅方向」出發
  · 每個方向生成多個候選策略 × 多個到期月，全部用 HV20 真實波幅
    做對數常態積分計勝率＋期望值，再綜合評分排序
  · 輸出每條腿嘅富途期權代碼（可以直接落盤）

CLI:
    python3 direction_advisor.py 00700 up
    python3 direction_advisor.py 00700 flat --json
    python3 direction_advisor.py 00700 down --top 3
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import bs
import iv_analyzer
import market_chains as mc
import options_chain as oc
import strategy_engine as se

sys.path.insert(0, str(se.BASE.parent / "auto-trading"))
import option_codes  # noqa: E402

DIR_LABEL = {"up": "買升", "flat": "窄幅波動", "down": "買跌"}
MIN_OI = 20

# 到期月分桶：短／中／長，每桶揀未平倉最多嗰個
EXP_BUCKETS = [(10, 35, "短線"), (35, 70, "中線"), (70, 130, "長線")]


# ---------------------------------------------------------------- 候選策略
def _leg(row, qty):
    return se._leg(row, qty)


def _candidates_up(df, spot, t, atm_iv, min_oi=MIN_OI):
    cands = []
    buy_atm = _leg(oc.nearest(df, spot, "C", min_oi=min_oi), 1)
    sell_hi = se._leg(se._by_delta(df, spot, t, 0.22, "C", atm_iv, min_oi), -1)
    sell_mid = se._leg(se._by_delta(df, spot, t, 0.32, "C", atm_iv, min_oi), -1)
    sell_put = se._leg(se._by_delta(df, spot, t, 0.28, "P", atm_iv, min_oi), -1)
    wing_put = se._leg(se._by_delta(df, spot, t, 0.12, "P", atm_iv, min_oi), 1)

    if buy_atm:
        cands.append(("Long Call（買認購）",
                      "直接買入貼價外／貼價內 Call，升得愈多賺愈多，蝕最多期權金",
                      [buy_atm]))
    if buy_atm and sell_hi and sell_hi["strike"] > buy_atm["strike"]:
        cands.append(("Bull Call Spread（牛市買權價差）",
                      "買 ATM Call＋賣更高行使價 Call 攤成本，最大賺蝕封頂",
                      [buy_atm, sell_hi]))
    if buy_atm and sell_mid and sell_mid["strike"] > buy_atm["strike"]:
        cands.append(("Bull Call Spread（較闊翼）",
                      "賣更高行使價嘅 Call，賺幅更闊但期權金抵銷較少",
                      [buy_atm, sell_mid]))
    if sell_put and wing_put and sell_put["strike"] > wing_put["strike"]:
        cands.append(("Bull Put Spread（牛市賣權價差・收錢）",
                      "賣價外 Put 收期權金＋買更低 Put 封蝕，唔升都有錢收",
                      [sell_put, wing_put]))
    if sell_put:
        cands.append(("Short Put（賣認沽・收錢）",
                      "賣價外 Put 收期權金，跌穿行權價要接貨（需保證金）",
                      [sell_put]))
    return cands


def _candidates_flat(df, spot, t, atm_iv, min_oi=MIN_OI):
    cands = []
    sc20 = se._leg(se._by_delta(df, spot, t, 0.20, "C", atm_iv, min_oi), -1)
    sp20 = se._leg(se._by_delta(df, spot, t, 0.20, "P", atm_iv, min_oi), -1)
    lc08 = se._leg(se._by_delta(df, spot, t, 0.08, "C", atm_iv, min_oi), 1)
    lp08 = se._leg(se._by_delta(df, spot, t, 0.08, "P", atm_iv, min_oi), 1)

    if all([sc20, sp20, lc08, lp08]) and \
            lc08["strike"] > sc20["strike"] > sp20["strike"] > lp08["strike"]:
        cands.append(("Iron Condor（鐵鷹）",
                      "賣兩邊 0.20Δ 價外＋買更遠翼封蝕，股價唔出帶就袋晒期權金",
                      [sp20, sc20, lp08, lc08]))
    if sc20 and sp20:
        cands.append(("Short Strangle（賣勒式・收錢）",
                      "賣兩邊價外收雙份期權金，唔升唔跌最賺（無限風險，需保證金）",
                      [sc20, sp20]))
    atm_c = _leg(oc.nearest(df, spot, "C", min_oi=min_oi), -1)
    atm_p = _leg(oc.nearest(df, spot, "P", min_oi=min_oi), -1)
    if all([atm_c, atm_p, lc08, lp08]) and \
            lc08["strike"] > atm_c["strike"] and lp08["strike"] < atm_p["strike"]:
        cands.append(("Iron Butterfly（鐵蝴蝶）",
                      "賣 ATM 兩邊收最厚期權金＋買翼封蝕，釘喺現價附近先最賺",
                      [atm_c, atm_p, lp08, lc08]))
    return cands


def _candidates_down(df, spot, t, atm_iv, min_oi=MIN_OI):
    cands = []
    buy_atm = _leg(oc.nearest(df, spot, "P", min_oi=min_oi), 1)
    sell_hi = se._leg(se._by_delta(df, spot, t, 0.22, "P", atm_iv, min_oi), -1)
    sell_mid = se._leg(se._by_delta(df, spot, t, 0.32, "P", atm_iv, min_oi), -1)
    sell_call = se._leg(se._by_delta(df, spot, t, 0.28, "C", atm_iv, min_oi), -1)
    wing_call = se._leg(se._by_delta(df, spot, t, 0.12, "C", atm_iv, min_oi), 1)

    if buy_atm:
        cands.append(("Long Put（買認沽）",
                      "直接買入貼價外／貼價內 Put，跌得愈多賺愈多，蝕最多期權金",
                      [buy_atm]))
    if buy_atm and sell_hi and sell_hi["strike"] < buy_atm["strike"]:
        cands.append(("Bear Put Spread（熊市賣權價差）",
                      "買 ATM Put＋賣更低行使價 Put 攤成本，最大賺蝕封頂",
                      [buy_atm, sell_hi]))
    if buy_atm and sell_mid and sell_mid["strike"] < buy_atm["strike"]:
        cands.append(("Bear Put Spread（較闊翼）",
                      "賣更低行使價嘅 Put，賺幅更闊但期權金抵銷較少",
                      [buy_atm, sell_mid]))
    if sell_call and wing_call and wing_call["strike"] > sell_call["strike"]:
        cands.append(("Bear Call Spread（熊市買權價差・收錢）",
                      "賣價外 Call 收期權金＋買更高 Call 封蝕，唔跌都有錢收",
                      [sell_call, wing_call]))
    if sell_call:
        cands.append(("Short Call（賣認購・收錢）",
                      "賣價外 Call 收期權金，升穿行權價要交貨（無限風險，需保證金）",
                      [sell_call]))
    return cands


BUILDERS = {"up": _candidates_up, "flat": _candidates_flat, "down": _candidates_down}


# ---------------------------------------------------------------- 評分
def score_strategy(ev_res: dict) -> float:
    """綜合分：勝率＋期望值／風險比，無限風險重罰。"""
    wr = (ev_res["win_rate"] - 50.0) / 10.0          # 每 10 個百分點 1 分
    if ev_res["unlimited_risk"]:
        risk_pts = -3.0
    else:
        risk_base = abs(ev_res["max_loss"] or 0) or abs(ev_res["net_cost_hkd"]) or 1.0
        risk_pts = max(-3.0, min(3.0, (ev_res["ev_hkd"] or 0) / risk_base * 10))
    ev_pts = 1.5 if (ev_res["ev_hkd"] or 0) > 0 else -1.5
    return round(wr + risk_pts + ev_pts, 2)


# ---------------------------------------------------------------- 主流程
def _vol_regime_by_ratio(iv: float, hv: float) -> tuple[str, str]:
    """冇 IVR 嘅市場（期指／美股）：用 IV/HV 比率判斷貴平。"""
    if not hv or hv <= 0:
        return "fair", f"IV {iv:.0f}%（冇 HV 對照）"
    ratio = iv / hv
    if ratio >= 1.15:
        return "rich", f"IV {iv:.0f}% 貴（IV/HV {ratio:.2f}）→ 賣波幅較有利"
    if ratio <= 0.85:
        return "cheap", f"IV {iv:.0f}% 平（IV/HV {ratio:.2f}）→ 買波幅較有利"
    return "fair", f"IV {iv:.0f}% 中性（IV/HV {ratio:.2f}）"


_iv_cache: dict[str, list[dict]] = {}


def _iv_rows(as_of: str | None = None) -> list[dict]:
    key = as_of or "latest"
    if key not in _iv_cache:
        _iv_cache[key] = iv_analyzer.analyse(as_of)
    return _iv_cache[key]


def _pick_expiries(code: str, rpt: date) -> list[dict]:
    exps = oc.expiries(code, rpt)
    picked = []
    for lo, hi, label in EXP_BUCKETS:
        cand = [e for e in exps if lo <= e["dte"] <= hi and (e["oi"] or 0) > 0]
        if cand:
            e = max(cand, key=lambda x: x["oi"] or 0)
            e = dict(e)
            e["bucket"] = label
            picked.append(e)
    if not picked and exps:
        e = dict(min(exps, key=lambda x: x["dte"]))
        e["bucket"] = "最近"
        picked.append(e)
    return picked


def advise(code: str, direction: str, as_of: str | None = None,
           top: int = 5, market: str = "hk_stock",
           instrument: str = "HSI") -> dict:
    market = (market or "hk_stock").strip().lower()
    if market not in mc.MARKET_LABEL:
        return {"ok": False, "error": f"market 必須係 {'/'.join(mc.MARKET_LABEL)}"}
    if direction not in BUILDERS:
        return {"ok": False, "error": f"direction 必須係 up/flat/down（收到 {direction}）"}

    code = code.strip().zfill(5) if market == "hk_stock" else code.strip().upper()

    if market == "hk_stock":
        ctx = _ctx_hk_stock(code, as_of)
    elif market == "hk_index":
        ctx = mc.hk_index_ctx(instrument)
    else:
        ctx = mc.us_ctx(code)
    if not ctx or not ctx.get("ok"):
        return {"ok": False,
                "error": (ctx or {}).get("error") or f"{code} 攞唔到期權數據"}
    if not ctx["exps"]:
        return {"ok": False, "error": f"{code} 搵唔到可用到期月"}
    if not ctx["hv20"]:
        ctx["hv20"] = ctx["atm_iv"]

    spot = ctx["spot"]
    atm_iv = ctx["atm_iv"]
    real_vol = ctx["hv20"]
    size = ctx["size"]
    if not ctx.get("regime"):
        regime, regime_note = _vol_regime_by_ratio(atm_iv, real_vol)
        ctx["regime"], ctx["regime_note"] = regime, regime_note
    if not ctx.get("note"):
        ctx["note"] = ""
    ctx.setdefault("codes_from_chain", market != "hk_stock")

    results: list[dict] = []
    for exp in ctx["exps"]:
        df = ctx["chain"](exp["expiry"])
        if df is None or df.empty:
            continue
        cmap = {}
        if "futu_code" in df.columns:
            cmap = {(r["type"], float(r["strike"])): r["futu_code"]
                    for _, r in df.iterrows()}
        t = bs.yearfrac(exp["dte"]) if hasattr(bs, "yearfrac") else exp["dte"] / 365.0
        for name, logic, legs in BUILDERS[direction](df, spot, t, atm_iv,
                                                         ctx.get("min_oi", MIN_OI)):
            if any(l is None for l in legs):
                continue
            ev_res = se.evaluate(legs, spot, t, real_vol, size)
            if not ctx["codes_from_chain"]:
                for lg in legs:
                    lg["futu_code"] = option_codes.option_code(
                        code, str(exp["expiry"]), lg["cp"], lg["strike"])
            elif cmap:
                for lg in legs:
                    lg["futu_code"] = cmap.get((lg["cp"], float(lg["strike"])))
            sc = score_strategy(ev_res)
            results.append({
                "strategy": name,
                "logic": logic,
                "expiry": str(exp["expiry"]),
                "dte": int(exp["dte"]),
                "bucket": exp["bucket"],
                "legs": legs,
                **ev_res,
                "score": sc,
            })

    results.sort(key=lambda r: (-r["score"], -(r["ev_hkd"] or 0)))
    best = results[0] if results else None
    out = {
        "ok": bool(results),
        "error": None if results else f"{code} 期權鏈太薄，砌唔到可靠策略",
        "date": str(ctx["date"]),
        "stock_code": code,
        "name": ctx["name"],
        "close": round(spot, 2),
        "market": market,
        "market_label": mc.MARKET_LABEL[market],
        "currency": ctx["currency"],
        "direction": DIR_LABEL[direction],
        "direction_raw": direction,
        "iv": round(atm_iv, 1),
        "hv20": round(real_vol, 1),
        "iv_rank": ctx.get("iv_rank"),
        "iv_hv": (round(atm_iv / real_vol * 100) if real_vol else None),
        "vol_regime": {"cheap": "波幅平", "rich": "波幅貴", "fair": "波幅中性"}[ctx["regime"]],
        "vol_note": ctx["regime_note"],
        "price_note": ctx["note"],
        "contract_size": size,
        "expiry": str(best["expiry"]) if best else None,
        "dte": int(best["dte"]) if best else None,
        "best": best,
        "alternatives": results[1:top],
        "n_candidates": len(results),
    }
    if market == "hk_index":
        out["instrument"] = ctx["instrument"]
        out["mult_note"] = ctx["mult_note"]
    return out


def _ctx_hk_stock(code: str, as_of: str | None = None) -> dict:
    rows = _iv_rows(as_of)
    iv_row = next((r for r in rows if r["stock_code"] == code), None)
    if not iv_row:
        return {"ok": False, "error": f"{code} 唔喺期權標的名單（{len(rows)} 隻），請檢查代號"}
    if not iv_row.get("iv") or not iv_row.get("close"):
        return {"ok": False, "error": f"{code} 最新報告冇 IV／收市數據"}
    rpt = date.fromisoformat(iv_row["date"])
    exps = _pick_expiries(code, rpt)
    regime, regime_note = se.vol_regime(iv_row)
    return {
        "ok": True, "name": iv_row["name"], "date": rpt,
        "spot": float(iv_row["close"]), "atm_iv": float(iv_row["iv"]),
        "hv20": iv_row.get("hv20"), "size": se.contract_size(code),
        "currency": "HKD", "iv_rank": iv_row.get("iv_rank"), "min_oi": MIN_OI,
        "regime": regime, "regime_note": regime_note,
        "note": "權金＝HKEX 每日結算價（非即市，落單時會換即市價）",
        "exps": exps,
        "chain": lambda exp: oc.chain(code, rpt, exp),
        "codes_from_chain": False,
    }


def stocks(market: str = "hk_stock") -> list[dict]:
    """各市場標的名單（代號＋名），俾前端做搜尋。"""
    market = (market or "hk_stock").strip().lower()
    if market == "hk_index":
        return [{"code": k, "name": f"{mc.INDEX_INSTRUMENTS[k]['name']}（{mc.INDEX_INSTRUMENTS[k]['mult_note']}）"}
                for k in mc.INDEX_INSTRUMENTS]
    if market == "us_stock":
        return [{"code": s2["code"], "name": f"{s2['code']}（{s2['name']}）"}
                for s2 in mc.US_UNIVERSE]
    return [{"code": r["stock_code"], "name": r["name"]} for r in _iv_rows()]


# ---------------------------------------------------------------- CLI
def _fmt_leg(lg: dict) -> str:
    return (f"  {lg['action']:<4} {lg['type']:<5} 行使價 {lg['strike']:>9.2f}  "
            f"權金 {lg['price']:>8.2f}  OI {lg['oi']:>8,}  {lg.get('futu_code') or ''}")


def _fmt(res: dict, full: bool = True) -> str:
    if not res["ok"]:
        return f"✗ {res['error']}"
    cur = "US$" if res.get("currency") == "USD" else "HK$"
    L = [f"{'═' * 72}",
         f"{res['stock_code']} {res['name']}   收市 {res['close']}   報告日 {res['date']}",
         f"市場：{res.get('market_label', '港股期權')}   {res.get('price_note', '')}",
         f"睇法：{res['direction']}   ATM IV {res['iv']:.0f}%（HV20 {res['hv20']}%"
         + (f"、IVR {res['iv_rank']:.0f}" if res.get('iv_rank') is not None else "")
         + f"）→ {res['vol_regime']}：{res['vol_note']}",
         f"{'═' * 72}"]
    rows = [res["best"]] + (res["alternatives"] if full else [])
    for i, r in enumerate(rows):
        tag = "★ 首選" if i == 0 else f"  候選 {i}"
        L.append(f"\n【{tag}】{r['strategy']}   到期 {r['expiry']}（DTE {r['dte']}・{r['bucket']}）")
        L.append(f"  {r['logic']}")
        for lg in r["legs"]:
            L.append(_fmt_leg(lg))
        mp = "無上限" if r["max_profit"] is None else f"{cur}{r['max_profit']:,.0f}"
        ml = ("無限（需保證金）" if r["unlimited_risk"]
              else f"{cur}{abs(r['max_loss'] or 0):,.0f}")
        L.append(f"  淨{r['direction'] if r['direction'] in ('付出','收取') else '成本'} "
                 f"{cur}{abs(r['net_cost_hkd']):,.0f}／張"
                 f"{'（收錢）' if r['net_cost'] < 0 else ''}")
        if r["breakevens"]:
            L.append(f"  盈虧平衡 {' / '.join(f'{b:,.2f}' for b in r['breakevens'])}")
        L.append(f"  最大賺 {mp} ／ 最大蝕 {ml}")
        L.append(f"  勝率 {r['win_rate']:.1f}%   期望值 {cur}{r['ev_hkd']:+,.0f}／張   "
                 f"綜合分 {r['score']}")
    L.append(f"\n共評估 {res['n_candidates']} 個組合（3 個到期月桶 × 多個策略）")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="方向揀期權策略")
    ap.add_argument("stock", help="股票代號，例如 00700／HSI／AAPL")
    ap.add_argument("direction", choices=["up", "flat", "down"])
    ap.add_argument("--market", default="hk_stock",
                    choices=["hk_stock", "hk_index", "us_stock"])
    ap.add_argument("--top", type=int, default=4)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stdout-json", action="store_true")
    a = ap.parse_args()

    res = advise(a.stock, a.direction, top=a.top, market=a.market,
                 instrument=a.stock.upper() if a.market == "hk_index" else "HSI")
    if a.json or a.stdout_json:
        print(json.dumps(res, ensure_ascii=False, default=str))
        return
    print(_fmt(res))


if __name__ == "__main__":
    main()
