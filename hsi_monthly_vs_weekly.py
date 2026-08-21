"""hsi_monthly_vs_weekly.py — 日版策略：月期權 vs 週期權 + 勝率調節手數模擬

問題：日版 short strangle（每日重計行使價、VHSI≥20 先入場）
  A) 用週期權（~5 DTE），持到期結算（內在值）— 現行做法
  B) 用月期權（前月 DTE≥20），持 5 個交易日後 BS 平倉（出場用當日 VHSI 重估）

另外：
  - 按入場 BS 勝率分桶，睇預測勝率有冇分辨力（決定「勝率高做多d」可唔可行）
  - 模擬 MHI 五級手數（1-5 張 MHI，上限 = 1 張 HSI 等值）vs 固定 1 張 HSI

數據：Desktop/db/Futu/Kline/kline_index.parquet（HK.800000 恒指 + HK.800125 VHSI 日K）
"""
import json
import math
from datetime import date, timedelta

import pandas as pd

from bs import price as bs_price, prob_touch

KLINE = "/home/workspace/Desktop/db/Futu/Kline/kline_index.parquet"
OUT = "/home/workspace/stock-analysis/hsi_monthly_vs_weekly.json"
RATE = 0.035
MULT_HSI = 50.0     # $/點（HSI）
WEEK_EM_SQRT = math.sqrt(5.0 / 252.0)
HOLD_TD = 5         # 持 5 個交易日
MIN_MONTHLY_DTE = 20  # 前月期權最少 DTE


def load():
    df = pd.read_parquet(KLINE)
    df["date"] = pd.to_datetime(df["time_key"]).dt.date
    hsi = df[df["code"] == "HK.800000"].set_index("date")["close"]
    vhsi = df[df["code"] == "HK.800125"].set_index("date")["close"]
    px = pd.DataFrame({"hsi": hsi, "vhsi": vhsi}).dropna().sort_index()
    return px


def mode_of(v):
    if v < 20:
        return "SKIP"
    if v <= 22:
        return "PutOnly"
    if v <= 40:
        return "Full"
    return "HighVol"


def round_up200(x):
    return int(math.ceil(x / 200.0) * 200)


def round_dn200(x):
    return int(math.floor(x / 200.0) * 200)


def strikes_daily(hsi, vhsi, mode):
    """日版：1 週預期波幅行使價（同現行一致）"""
    em = hsi * (vhsi / 100.0) * WEEK_EM_SQRT
    K = L = None
    if mode in ("Full", "HighVol"):
        K = round_up200(hsi + em)
    if mode in ("PutOnly", "Full", "HighVol"):
        mult = 1.3 if mode == "PutOnly" else 1.0
        L = round_dn200(hsi - em * mult)
    return K, L


def month_last_second_bizday(y, m):
    if m == 12:
        d = date(y, 12, 31)
    else:
        d = date(y, m + 1, 1) - timedelta(days=1)
    # 月尾倒數第二個工作日
    days = []
    cur = d
    while len(days) < 2:
        if cur.weekday() < 5:
            days.append(cur)
        cur -= timedelta(days=1)
    return days[1]


def monthly_expiries(start, end):
    out = []
    y, m = start.year, start.month
    limit_y, limit_m = end.year, end.month + 2
    while limit_m > 12:
        limit_m -= 12
        limit_y += 1
    while (y, m) <= (limit_y, limit_m):
        out.append(month_last_second_bizday(y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return sorted(set(out))


def pick_monthly(entry, expiries):
    for e in expiries:
        if (e - entry).days >= MIN_MONTHLY_DTE:
            return e
    return None


def bs_strangle_value(S, K, L, sigma, T):
    v = 0.0
    if K:
        c = bs_price(S, K, T, sigma, "C", r=RATE, q=0.0)
        v += c or 0.0
    if L:
        p = bs_price(S, L, T, sigma, "P", r=RATE, q=0.0)
        v += p or 0.0
    return v


def run(px):
    dates = list(px.index)
    expiries = monthly_expiries(dates[0], dates[-1])
    trades = []
    for i in range(len(dates) - HOLD_TD):
        d0 = dates[i]
        d1 = dates[i + HOLD_TD]
        hsi0, v0 = float(px["hsi"].iloc[i]), float(px["vhsi"].iloc[i])
        hsi1, v1 = float(px["hsi"].iloc[i + HOLD_TD]), float(px["vhsi"].iloc[i + HOLD_TD])
        mode = mode_of(v0)
        if mode == "SKIP":
            continue
        K, L = strikes_daily(hsi0, v0, mode)
        sig = (v0 / 100.0)
        p_touch = (prob_touch(hsi0, K, HOLD_TD / 252.0, sig) if K else 0) or 0
        p_touch += (prob_touch(hsi0, L, HOLD_TD / 252.0, sig) if L else 0) or 0
        p_win = 1.0 - p_touch

        # A) 週期權持到期（5 交易日後內在值結算）
        pnl_wk = 0.0
        if K:
            pnl_wk += max(0.0, hsi1 - K)
        if L:
            pnl_wk += max(0.0, L - hsi1)
        prem_wk = bs_strangle_value(hsi0, K, L, sig, 5.0 / 365.0)  # 入場理論權金（參考）
        pnl_wk = prem_wk - pnl_wk  # 賣方：權金 − 結算值

        # B) 月期權持 5 日 BS 平倉
        exp = pick_monthly(d0, expiries)
        pnl_mo = None
        if exp:
            T0 = (exp - d0).days / 365.0
            T1 = (exp - d1).days / 365.0
            if T1 > 0:
                entry = bs_strangle_value(hsi0, K, L, sig, T0)
                exitv = bs_strangle_value(hsi1, K, L, v1 / 100.0, T1)
                pnl_mo = entry - exitv
        trades.append({
            "date": str(d0), "mode": mode, "hsi0": round(hsi0, 1), "v0": round(v0, 2),
            "K": K, "L": L, "p_win": round(p_win, 4),
            "pnl_weekly_pts": round(pnl_wk, 2),
            "pnl_monthly_pts": round(pnl_mo, 2) if pnl_mo is not None else None,
        })
    return trades


def stats(trades, key):
    xs = [t for t in trades if t.get(key) is not None]
    n = len(xs)
    wins = [t for t in xs if t[key] > 0]
    total = sum(t[key] for t in xs)
    worst = min((t[key] for t in xs), default=0)
    return {
        "n": n,
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
        "avg_pts": round(total / n, 2) if n else None,
        "avg_hkd": round(total / n * MULT_HSI, 0) if n else None,
        "total_hkd": round(total * MULT_HSI, 0),
        "worst_pts": round(worst, 1),
        "worst_hkd": round(worst * MULT_HSI, 0),
    }


def buckets(trades):
    """按入場預測勝率分桶，睇實際勝率（月期權 P&L）"""
    bs = {"<80%": [], "80-85%": [], "85-90%": [], "90-95%": [], ">=95%": []}
    for t in trades:
        if t["pnl_monthly_pts"] is None:
            continue
        p = t["p_win"]
        k = "<80%" if p < 0.80 else "80-85%" if p < 0.85 else "85-90%" if p < 0.90 else "90-95%" if p < 0.95 else ">=95%"
        bs[k].append(t)
    out = {}
    for k, xs in bs.items():
        if not xs:
            out[k] = {"n": 0}
            continue
        wins = sum(1 for t in xs if t["pnl_monthly_pts"] > 0)
        tot = sum(t["pnl_monthly_pts"] for t in xs)
        out[k] = {
            "n": len(xs),
            "actual_win_pct": round(wins / len(xs) * 100, 1),
            "avg_pts": round(tot / len(xs), 2),
            "avg_hkd": round(tot / len(xs) * MULT_HSI, 0),
        }
    return out


def sizing_sim(trades):
    """MHI 五級手數（按預測勝率）vs 固定 1 張 HSI。單位：HSI 等值。"""
    def lots_mhi(p):
        if p >= 0.95:
            return 5
        if p >= 0.90:
            return 4
        if p >= 0.85:
            return 3
        if p >= 0.80:
            return 2
        return 1
    fixed_tot = sized_tot = 0.0
    fixed_worst = sized_worst = 0.0
    sized_wins = sized_n = 0
    for t in trades:
        pnl = t.get("pnl_monthly_pts")
        if pnl is None:
            continue
        fixed_tot += pnl * MULT_HSI
        fixed_worst = min(fixed_worst, pnl * MULT_HSI)
        sized = pnl * (lots_mhi(t["p_win"]) / 5.0) * MULT_HSI
        sized_tot += sized
        sized_worst = min(sized_worst, sized)
        sized_n += 1
        if sized > 0:
            sized_wins += 1
    return {
        "fixed_1HSI": {"total_hkd": round(fixed_tot, 0), "worst_hkd": round(fixed_worst, 0)},
        "sized_MHI": {"total_hkd": round(sized_tot, 0), "worst_hkd": round(sized_worst, 0),
                      "win_rate_pct": round(sized_wins / sized_n * 100, 1) if sized_n else None},
    }


def main():
    px = load()
    print(f"數據: {px.index[0]} → {px.index[-1]} ({len(px)} 交易日)")
    trades = run(px)
    res = {
        "weekly_hold_expiry": stats(trades, "pnl_weekly_pts"),
        "monthly_bs_exit_5d": stats(trades, "pnl_monthly_pts"),
        "winprob_buckets_monthly": buckets(trades),
        "sizing": sizing_sim(trades),
    }
    res["data"] = {"start": str(px.index[0]), "end": str(px.index[-1]), "days": len(px), "trades": len(trades)}
    json.dump(res, open(OUT, "w"), ensure_ascii=False, indent=2)

    print("\n=== 日版策略：月期權 vs 週期權 ===")
    for k in ("weekly_hold_expiry", "monthly_bs_exit_5d"):
        s = res[k]
        print(f"  {k:22s}  n={s['n']:3d}  勝率 {s['win_rate_pct']}%  平均 {s['avg_pts']} 點 (${s['avg_hkd']:,.0f})  最差 {s['worst_pts']} 點 (${s['worst_hkd']:,.0f})")
    print("\n=== 預測勝率分桶（月期權）===")
    for k, b in res["winprob_buckets_monthly"].items():
        if b.get("n"):
            print(f"  {k:8s}  n={b['n']:3d}  實際勝率 {b['actual_win_pct']}%  平均 ${b['avg_hkd']:,.0f}")
        else:
            print(f"  {k:8s}  n=0")
    print("\n=== 手數模擬（月期權）===")
    f, s = res["sizing"]["fixed_1HSI"], res["sizing"]["sized_MHI"]
    print(f"  固定 1 張 HSI : 總 ${f['total_hkd']:,.0f}  最差 ${f['worst_hkd']:,.0f}")
    print(f"  勝率調節 MHI  : 總 ${s['total_hkd']:,.0f}  最差 ${s['worst_hkd']:,.0f}  勝率 {s['win_rate_pct']}%")
    print(f"\n✅ 已寫入 {OUT}")


if __name__ == "__main__":
    main()
