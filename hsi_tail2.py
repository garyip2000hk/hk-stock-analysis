#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""修正版：正確 mark-to-market + 多種避險方法對決。"""
import math
import hsi_trade_log as tl


def bs(s, k, t, vol, cp):
    if not k:
        return 0.0
    return tl.bs_price(s, k, t, vol, cp) or 0.0


def prem_at(hsi, K, L, t, v):
    return bs(hsi, K, t, v / 100.0, "C") + bs(hsi, L, t, v / 100.0, "P")


def setup(df, i, width=1.0, allow_putonly=True):
    v = float(df.loc[i, "vhsi"])
    hsi = float(df.loc[i, "hsi"])
    if int(df.loc[i, "weekday"]) == 4:
        return None
    if v < 20:
        return None
    if v < 22 and not allow_putonly:
        return None
    em = hsi * (v / 100.0) * math.sqrt(5 / 252) * width
    if v < 22:
        K, L, mode = None, tl.round_down(hsi - em * 1.3), "PutOnly"
    else:
        K, L, mode = tl.round_up(hsi + em), tl.round_down(hsi - em), "Full"
    return dict(v=v, hsi=hsi, K=K, L=L, mode=mode, em=em)


def baseline(df, width=1.0, allow_putonly=True):
    out = []
    for i in range(len(df) - tl.HOLD):
        s = setup(df, i, width, allow_putonly)
        if not s:
            continue
        t0 = (tl.HOLD * 7.0 / 5.0) / 365.0
        exit_hsi = float(df.loc[i + tl.HOLD, "hsi"])
        pnl, _ = tl.settle(s["hsi"], s["v"], s["K"], s["L"], tl.HOLD, exit_hsi)
        out.append(pnl * tl.PTS_PER_LOT)
    return out


def stop_loss(df, stop_mult, width=1.0, allow_putonly=True):
    """每日 mark-to-market，浮虧 >= stop_mult x 權金即平（用當日 VHSI 剩餘 DTE）。"""
    out = []
    for i in range(len(df) - tl.HOLD):
        s = setup(df, i, width, allow_putonly)
        if not s:
            continue
        t0 = (tl.HOLD * 7.0 / 5.0) / 365.0
        prem = prem_at(s["hsi"], s["K"], s["L"], t0, s["v"])
        pnl = None
        for j in range(1, tl.HOLD):
            sj = float(df.loc[i + j, "hsi"])
            vj = float(df.loc[i + j, "vhsi"])
            tr = ((tl.HOLD - j) * 7.0 / 5.0) / 365.0
            val = prem_at(sj, s["K"], s["L"], tr, vj)
            loss_pts = val - prem
            if loss_pts >= stop_mult * prem:
                pnl = -loss_pts * tl.PTS_PER_LOT
                break
        if pnl is None:
            exit_hsi = float(df.loc[i + tl.HOLD, "hsi"])
            pnl, _ = tl.settle(s["hsi"], s["v"], s["K"], s["L"], tl.HOLD, exit_hsi)
            pnl *= tl.PTS_PER_LOT
        out.append(pnl)
    return out


def breach_exit(df, width=1.0, allow_putonly=True):
    """收市跌穿 L 或升穿 K 即刻平（touch-based 止蝕，用當日 VHSI mark）。"""
    out = []
    for i in range(len(df) - tl.HOLD):
        s = setup(df, i, width, allow_putonly)
        if not s:
            continue
        t0 = (tl.HOLD * 7.0 / 5.0) / 365.0
        prem = prem_at(s["hsi"], s["K"], s["L"], t0, s["v"])
        pnl = None
        for j in range(1, tl.HOLD):
            sj = float(df.loc[i + j, "hsi"])
            vj = float(df.loc[i + j, "vhsi"])
            breached = (s["K"] and sj > s["K"]) or (s["L"] and sj < s["L"])
            if breached:
                tr = ((tl.HOLD - j) * 7.0 / 5.0) / 365.0
                val = prem_at(sj, s["K"], s["L"], tr, vj)
                pnl = (prem - val) * tl.PTS_PER_LOT
                break
        if pnl is None:
            exit_hsi = float(df.loc[i + tl.HOLD, "hsi"])
            pnl, _ = tl.settle(s["hsi"], s["v"], s["K"], s["L"], tl.HOLD, exit_hsi)
            pnl *= tl.PTS_PER_LOT
        out.append(pnl)
    return out


def summ(label, pnls):
    n = len(pnls)
    if n == 0:
        print(f"{label:30s} n=0")
        return
    wins = sum(1 for p in pnls if p > 0)
    avg = sum(pnls) / n
    worst = min(pnls)
    print(f"{label:30s} n={n:4d} 勝率={wins/n*100:5.1f}% 期望=${avg:8.0f} 總=${sum(pnls):9.0f} 最差=${worst:9.0f}")


if __name__ == "__main__":
    df = tl.load_data()
    print("=== 避險方法對決（日版，DTE5）===\n")
    summ("基線（無避險）", baseline(df))
    summ("止蝕 1.5x權金（mark）", stop_loss(df, 1.5))
    summ("止蝕 2.0x權金（mark）", stop_loss(df, 2.0))
    summ("止蝕 3.0x權金（mark）", stop_loss(df, 3.0))
    summ("觸價止蝕（穿 K/L 即平）", breach_exit(df))
    print()
    summ("加闊 1.15xEM", baseline(df, width=1.15))
    summ("加闊 1.25xEM", baseline(df, width=1.25))
    summ("加闊 1.3xEM", baseline(df, width=1.3))
    print()
    summ("只做 Full（跳過 PutOnly）", baseline(df, allow_putonly=False))
    summ("只做 Full + 加闊 1.15x", baseline(df, width=1.15, allow_putonly=False))
    summ("只做 Full + 加闊 1.25x", baseline(df, width=1.25, allow_putonly=False))
    summ("只做 Full + 觸價止蝕", breach_exit(df, allow_putonly=False))
