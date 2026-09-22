#!/usr/bin/env python3
"""US SPX weekly short strangle / iron condor gate (VIX regime 過濾 + 注碼管理)。

方法論同 HSI 週版一致：band = 1.15 × EM（EM = SPX × VIX/100 × √(DTE/365)），
週五收市入場、下週五到期，用每日 high/low 做保守觸及結算。
期權金用 BS 估算（IV = VIX，無 OPRA 訂閱，tick 係估計值）。
"""
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import bs
from cross_market import load_ib, vix_regime

HERE = Path(__file__).parent
OUT = HERE / "options_data" / "us_condor_gate.json"
MULT = 1.15
WING = 2.2
RATE = 0.035
DTE = 5


def _week_fridays(dates: list[str]) -> list[str]:
    out = []
    for d in dates:
        dt = datetime.strptime(d, "%Y-%m-%d")
        if dt.weekday() == 4:
            out.append(d)
    return out


def backtest(spx: pd.DataFrame, vix: pd.DataFrame) -> dict:
    spx = spx.set_index("date")
    vix_close = vix.set_index("date")["close"]
    dates = sorted(spx.index)
    fridays = [d for d in _week_fridays(dates) if d in vix_close.index]
    variants = {"always": [], "gated15": [], "gated_stress": []}
    trades = []
    for i in range(len(fridays) - 1):
        entry, expiry = fridays[i], fridays[i + 1]
        if entry not in spx.index or expiry not in spx.index:
            continue
        c = float(spx.loc[entry, "close"])
        v = float(vix_close.loc[entry])
        if math.isnan(v) or v <= 0:
            continue
        em = c * v / 100 * math.sqrt(DTE / 365)
        k_put = round((c - MULT * em) / 25) * 25
        k_call = round((c + MULT * em) / 25) * 25
        w_put = round((c - WING * em) / 25) * 25
        w_call = round((c + WING * em) / 25) * 25
        t = DTE / 365
        credit = bs.price(c, k_call, t, v / 100, "C", r=RATE) + bs.price(c, k_put, t, v / 100, "P", r=RATE)
        win = True
        loss_pts = 0.0
        for d in dates:
            if entry < d <= expiry:
                hi, lo = float(spx.loc[d, "high"]), float(spx.loc[d, "low"])
                if hi >= k_call:
                    win = False
                    loss_pts = max(loss_pts, hi - k_call)
                if lo <= k_put:
                    win = False
                    loss_pts = max(loss_pts, k_put - lo)
        strangle_pnl = credit if win else credit - loss_pts
        wing = max(w_call - k_call, k_put - w_put)
        condor_pnl = credit if win else credit - min(loss_pts, wing)
        reg = vix_regime(v)["regime"]
        tr = {"entry": entry, "expiry": expiry, "vix": round(v, 1), "regime": reg,
              "k_put": k_put, "k_call": k_call, "credit": round(credit, 1),
              "win": win, "strangle_pnl": round(strangle_pnl, 1),
              "condor_pnl": round(condor_pnl, 1)}
        trades.append(tr)
        variants["always"].append(tr)
        if v >= 15:
            variants["gated15"].append(tr)
        if reg != "stress":
            variants["gated_stress"].append(tr)

    def summ(rows, key):
        if not rows:
            return {"n": 0}
        pnls = [r[key] for r in rows]
        wins = sum(1 for p in pnls if p > 0)
        return {"n": len(rows), "win_rate_pct": round(100 * wins / len(rows), 1),
                "avg_pnl_pts": round(sum(pnls) / len(pnls), 1),
                "worst_pts": round(min(pnls), 1)}

    return {"method": f"SPX weekly strangle DTE{DTE}, band {MULT}xEM, wings {WING}xEM",
            "variants": {
                "always": {**summ(variants["always"], "strangle_pnl"),
                           "condor": summ(variants["always"], "condor_pnl")},
                "gated_vix15": {**summ(variants["gated15"], "strangle_pnl"),
                                "condor": summ(variants["gated15"], "condor_pnl")},
                "gated_no_stress": {**summ(variants["gated_stress"], "strangle_pnl"),
                                    "condor": summ(variants["gated_stress"], "condor_pnl")},
            },
            "trades": trades[-12:]}


def ticket(spx: pd.DataFrame, vix: pd.DataFrame) -> dict:
    c = float(spx["close"].iloc[-1])
    v = float(vix["close"].iloc[-1])
    last_date = spx["date"].iloc[-1]
    reg = vix_regime(v)
    sizing = {"normal": 1.0, "caution": 0.5, "too_low": 0.5, "stress": 0.0,
              "unknown": 0.0}.get(reg["regime"], 0.0)
    dt = datetime.strptime(last_date, "%Y-%m-%d")
    days_to_fri = (4 - dt.weekday()) % 7 or 7
    expiry = dt + timedelta(days=days_to_fri)
    dte = days_to_fri
    em = c * v / 100 * math.sqrt(dte / 365)
    k_put = round((c - MULT * em) / 25) * 25
    k_call = round((c + MULT * em) / 25) * 25
    w_put = round((c - WING * em) / 25) * 25
    w_call = round((c + WING * em) / 25) * 25
    t = dte / 365
    credit = bs.price(c, k_call, t, v / 100, "C", r=RATE) + bs.price(c, k_put, t, v / 100, "P", r=RATE)
    return {
        "as_of": last_date, "spot": round(c, 1), "vix": round(v, 2),
        "regime": reg, "size_mult": sizing,
        "allow_new": sizing > 0,
        "expiry": expiry.strftime("%Y-%m-%d"), "dte": dte,
        "strangle": {"put": k_put, "call": k_call},
        "condor": {"put_wing": w_put, "put": k_put, "call": k_call, "call_wing": w_call},
        "est_credit_pts": round(credit, 1),
        "notes": [
            "期權金係 BS 估計（IV=VIX）——戶口未訂 OPRA，tick 數據唔可用",
            "美股賣方過去一年全 VIX 環境正溢價，閘門用途係注碼管理唔係避負期望",
            "stress（VIX≥30）唔開新倉；caution（22-30）／too_low（<15）半注",
        ],
    }


def main():
    data = load_ib()
    spx, vix = data["SPX"], data["VIX"]
    out = {"generated_at": datetime.now().isoformat(timespec="seconds"),
           "backtest": backtest(spx, vix), "ticket": ticket(spx, vix)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    bt = out["backtest"]["variants"]
    tk = out["ticket"]
    print(f"SPX weekly strangle 回測（n={bt['always']['n']}）：")
    for k, s in bt.items():
        print(f"  {k}: 勝率 {s['win_rate_pct']}% 平均 {s['avg_pnl_pts']}pts 最差 {s['worst_pts']}pts | condor 平均 {s['condor']['avg_pnl_pts']}pts 最差 {s['condor']['worst_pts']}pts")
    print(f"今日 ticket: VIX {tk['vix']} ({tk['regime']['regime']}) size×{tk['size_mult']} "
          f"strangle {tk['strangle']['put']}/{tk['strangle']['call']} est credit {tk['est_credit_pts']}pts")
    print(f"written {OUT}")


if __name__ == "__main__":
    main()
