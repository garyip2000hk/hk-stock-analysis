"""hsi_winrate_sizing.py — 勝率定手數 規則回測
用 hsi_monthly_vs_weekly 嘅引擎，驗證「勝率高做大多啲、勝率細做細啲」係咪真係改善風險調整回報。
"""
import json, math
import numpy as np
import pandas as pd
from hsi_monthly_vs_weekly import (
    load, detect_mode, calc_strikes, strangle_value, round_up, round_down,
    MULT, settle, ENGINE_FUNCS, RATE,
)
from bs import price as bs_price, prob_touch

def engine_monthly_close_5d(closes, dts, i, hsi, vhsi, mode, band_mult=1.0):
    K, L = calc_strikes(hsi, vhsi, mode, dte=5, band_mult=band_mult)
    prem_in = strangle_value(hsi, K, L, 25, vhsi)
    if prem_in <= 0: return None
    if i + 5 >= len(closes): return None
    prem_out = strangle_value(closes[i + 5], K, L, 20, dts[i + 5])
    return (prem_in - prem_out) * MULT

def engine_daily_recheck(closes, dts, i, hsi, vhsi, mode, band_mult=1.0):
    K, L = calc_strikes(hsi, vhsi, mode, dte=5, band_mult=band_mult)
    prem = strangle_value(hsi, K, L, 5, vhsi)
    if prem <= 0: return None
    return settle(closes, i, 5, K, L, mode, prem)

def engine_weekly_fixed(closes, dts, i, hsi, vhsi, mode, band_mult=1.2):
    if dts[i].weekday() != 0: return None
    K, L = calc_strikes(hsi, vhsi, mode, dte=5, band_mult=band_mult)
    prem = strangle_value(hsi, K, L, 5, vhsi)
    if prem <= 0: return None
    n = 0
    while i + n < len(closes) and dts[i + n].weekday() != 4:
        n += 1
    hold = min(max(n + 1, 2), 5)
    return settle(closes, i, hold, K, L, mode, prem)

def trades_with_winprob(closes, dts, engine, band_mult=1.0):
    out = []
    for i in range(len(closes)):
        hsi = float(closes[i]); vhsi = float(dts[i])
        mode = detect_mode(vhsi)
        if mode == "SKIP": continue
        pnl = engine(closes, dts, i, hsi, vhsi, mode, band_mult)
        if pnl is None: continue
        K, L = calc_strikes(hsi, vhsi, mode, dte=5, band_mult=band_mult)
        t = 5 / 252; sig = vhsi / 100
        wp = 1.0
        if K:
            pt = prob_touch(hsi, K, t, sig)
            if pt is not None: wp *= 1 - min(0.99, pt)
        if L:
            pt = prob_touch(hsi, L, t, sig)
            if pt is not None: wp *= 1 - min(0.99, pt)
        out.append({"i": i, "mode": mode, "pnl": pnl, "win_prob": wp})
    return out

def sizing_stats(trades, label, sizing_fn):
    flat = [t["pnl"] for t in trades]
    sized = [t["pnl"] * sizing_fn(t["win_prob"]) for t in trades]
    def s(pnls):
        if not pnls: return {}
        arr = np.array(pnls, dtype=float)
        tot = arr.sum(); mean = arr.mean(); sd = arr.std()
        return {"n": len(arr), "total": round(tot, 0), "avg": round(mean, 0),
                "win_rate": round((arr > 0).mean() * 100, 1),
                "max_loss": round(arr.min(), 0),
                "sharpe": round(mean / sd * math.sqrt(52), 2) if sd > 0 else 0,
                "ret_over_risk": round(tot / abs(arr.min()), 2) if arr.min() < 0 else None}
    return {"label": label, "flat_1x": s(flat), "sized": s(sized)}

def tier_sizing(wp):
    if wp >= 0.80: return 5
    if wp >= 0.70: return 3
    if wp >= 0.55: return 1
    return 0.5

def main():
    closes, dts = load()
    print(f"數據: {len(closes)} 交易日")
    results = {}
    for name, engine, bm in [
        ("daily_recheck_weekly_settle", engine_daily_recheck, 1.0),
        ("monthly_close_5d", engine_monthly_close_5d, 1.0),
        ("weekly_fixed_settle", engine_weekly_fixed, 1.2),
    ]:
        trades = trades_with_winprob(closes, dts, engine, bm)
        results[name] = {
            "trades_n": len(trades),
            "sizing": sizing_stats(trades, name, tier_sizing),
        }
        st = results[name]["sizing"]
        print(f"\n=== {name} ({len(trades)} 筆) ===")
        print(f"  固定1倍:  總{st['flat_1x']['total']:>12,.0f}  均{st['flat_1x']['avg']:>8,.0f}  勝率{st['flat_1x']['win_rate']}%  最差{st['flat_1x']['max_loss']:>10,.0f}  sharpe {st['flat_1x']['sharpe']}")
        print(f"  勝率分層: 總{st['sized']['total']:>12,.0f}  均{st['sized']['avg']:>8,.0f}  勝率{st['sized']['win_rate']}%  最差{st['sized']['max_loss']:>10,.0f}  sharpe {st['sized']['sharpe']}")
        buckets = {}
        for t in trades:
            sz = tier_sizing(t["win_prob"])
            buckets.setdefault(sz, []).append(t)
        print("  分層明細（MHI 張數 | 筆數 | 勝率 | 平均$ | 最差 | 平均勝率預測）:")
        for sz in sorted(buckets, reverse=True):
            b = buckets[sz]
            w = [x for x in b if x["pnl"] > 0]
            wp = np.mean([x["win_prob"] for x in b])
            print(f"    {sz:>4}x | {len(b):>4} | {len(w)/len(b)*100:>5.1f}% | {np.mean([x['pnl'] for x in b]):>9,.0f} | {min(x['pnl'] for x in b):>10,.0f} | {wp*100:.0f}%")
    out_path = "/home/workspace/stock-analysis/hsi_winrate_sizing.json"
    json.dump(results, open(out_path, "w"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已寫入 {out_path}")

if __name__ == "__main__":
    main()
