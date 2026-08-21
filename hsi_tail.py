import math
import pandas as pd
import hsi_trade_log as tl

def worst(df, n=6):
    rows = []
    for i in range(len(df) - tl.HOLD):
        v = float(df.loc[i, "vhsi"]); hsi = float(df.loc[i, "hsi"])
        if int(df.loc[i, "weekday"]) == 4: continue
        if v < 20: continue
        em = hsi * (v/100.0) * math.sqrt(5/252)
        if v < 22: K, L, mode = None, tl.round_down(hsi - em*1.3), "PutOnly"
        else: K, L, mode = tl.round_up(hsi+em), tl.round_down(hsi-em), "Full"
        exit_hsi = float(df.loc[i+tl.HOLD, "hsi"])
        pnl, prem = tl.settle(hsi, v, K, L, tl.HOLD, exit_hsi)
        lo = min(float(df.loc[i+j, "hsi"]) for j in range(tl.HOLD+1))
        hi = max(float(df.loc[i+j, "hsi"]) for j in range(tl.HOLD+1))
        rows.append({"date": df.loc[i,"date"], "mode": mode, "vhsi": round(v,1),
                     "K": K, "L": L, "hsi0": round(hsi,0), "exit": round(exit_hsi,0),
                     "prem": round(prem,1), "pnl": round(pnl*tl.PTS_PER_LOT,0),
                     "lo": round(lo,0), "hi": round(hi,0)})
    rows.sort(key=lambda r: r["pnl"])
    return rows

def mark(s, k, t_yr, vol):
    v = 0.0
    if k:
        v += tl.bs_price(s, k, t_yr, vol, "C") or 0.0
    return v

def stop_loss(df, stop_mult, width=1.0, hold=tl.HOLD):
    trades = []
    for i in range(len(df) - hold):
        v = float(df.loc[i, "vhsi"]); hsi = float(df.loc[i, "hsi"])
        if int(df.loc[i, "weekday"]) == 4: continue
        if v < 20: continue
        em = hsi * (v/100.0) * math.sqrt(5/252) * width
        if v < 22: K, L = None, tl.round_down(hsi - em*1.3)
        else: K, L = tl.round_up(hsi+em), tl.round_down(hsi-em)
        t0 = (hold*7.0/5.0)/365.0
        prem = mark(hsi, K, t0, v/100.0) + mark(hsi, L, t0, v/100.0)
        pnl = None; stopped = False
        for j in range(1, hold):
            sj = float(df.loc[i+j, "hsi"]); vj = float(df.loc[i+j, "vhsi"])
            tr = ((hold-j)*7.0/5.0)/365.0
            val = mark(sj, K, tr, vj/100.0) + mark(sj, L, tr, vj/100.0)
            loss = val - prem
            if loss >= stop_mult * prem:
                pnl = -loss; stopped = True; break
        if not stopped:
            exit_hsi = float(df.loc[i+hold, "hsi"])
            pnl, _ = tl.settle(hsi, v, K, L, hold, exit_hsi)
        trades.append(pnl * tl.PTS_PER_LOT)
    return trades

def iron_condor(df, wing_mult, width=1.0, hold=tl.HOLD):
    trades = []
    for i in range(len(df) - hold):
        v = float(df.loc[i, "vhsi"]); hsi = float(df.loc[i, "hsi"])
        if int(df.loc[i, "weekday"]) == 4: continue
        if v < 20: continue
        em = hsi * (v/100.0) * math.sqrt(5/252) * width
        if v < 22: K, L = None, tl.round_down(hsi - em*1.3)
        else: K, L = tl.round_up(hsi+em), tl.round_down(hsi-em)
        t0 = (hold*7.0/5.0)/365.0
        if K and L:
            wing = round(em * wing_mult / 200.0) * 200.0
            if wing < 200: wing = 200
            Kc, Lp = K + wing, L - wing
            net = (tl.bs_price(hsi, K, t0, v/100.0, "C") + tl.bs_price(hsi, L, t0, v/100.0, "P")
                   - tl.bs_price(hsi, Kc, t0, v/100.0, "C") - tl.bs_price(hsi, Lp, t0, v/100.0, "P"))
            exit_hsi = float(df.loc[i+hold, "hsi"])
            pay = -max(exit_hsi-K, 0) + max(exit_hsi-Kc, 0) - max(L-exit_hsi, 0) + max(Lp-exit_hsi, 0)
            pnl = net + pay
        else:
            exit_hsi = float(df.loc[i+hold, "hsi"])
            pnl, _ = tl.settle(hsi, v, K, L, hold, exit_hsi)
        trades.append(pnl * tl.PTS_PER_LOT)
    return trades

def summ(name, trades):
    n = len(trades); wins = [t for t in trades if t > 0]
    tot = sum(trades); worst = min(trades)
    print(f"{name:26s} n={n:3d} 勝率={len(wins)/n*100:5.1f}% 期望=${tot/n:8.0f} 總=${tot:10.0f} 最差=${worst:9.0f}")

if __name__ == "__main__":
    df = tl.load_data()
    print("=== 最差 6 筆（日版，無避險）===")
    w = worst(df, 6)
    for r in w:
        print(f"  {r['date']}  {r['mode']:8s} VHSI={r['vhsi']:5.1f}  HSI {r['hsi0']:.0f}→{r['exit']:.0f}  "
              f"K={r['K']} L={r['L']}  權金={r['prem']:5.1f}  結果=${r['pnl']:8.0f}  期內 {r['lo']:.0f}~{r['hi']:.0f}")
    print()
    print("=== 避險方法實測 ===")
    summ("基線（無避險）", [r["pnl"] for r in worst(df, 10**9)])
    for m in (1.5, 2.0, 3.0):
        summ(f"止蝕 {m}×權金", stop_loss(df, m))
    for wm in (1.1, 1.15, 1.25, 1.3):
        summ(f"加闊行使價 {wm}×EM", [r["pnl"] for r in worst(df, 10**9)] if wm == 1.0 else stop_loss(df, 99, width=wm))
    for wm in (0.5, 1.0, 1.5):
        summ(f"鐵鷹（買翼 {wm}×EM）", iron_condor(df, wm))
