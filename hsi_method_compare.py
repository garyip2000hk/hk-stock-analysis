"""hsi_method_compare.py — 恒指 Short Strangle 計法對決回測

比較三套計法（全部用同一份 OpenD 歷史數據 + 同一個結算方法，淨係揀價邏輯唔同）：

  A. 現時法（gsmart-box / Zo）：每日按當日 spot+VHSI 重計
     EM = HSI × VHSI/100 × √(5/252)，K=roundUp(spot+EM,200)，L=roundDown(spot−EM,200)
     VHSI 門檻：<20 SKIP／20–22 PutOnly(×1.3)／22–40 Full；持 5 個交易日結算

  B. Excel 法（每週固定帶）：逢週首個交易日用當日 spot+VHSI 定一次帶
     帶 = spot ± mult × VHSI/√52，一星期唔變，週末（第 5 個交易日）結算；冇門檻
     （Excel 實測帶寬 ≈ 1.0–1.17 × 每週 EM，所以試 1.0 同 1.15 兩個 mult）

  C. 校準測試：唔做交易，淨係睇各法嘅帶有幾成時間包住實際走勢（1σ 應該 ~68%）

輸出：hsi_method_compare.json
"""

import json
import math
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent
KLINE = Path("/home/workspace/Desktop/db/Futu/Kline/kline_index.parquet")
OUT = BASE / "hsi_method_compare.json"
PTS_PER_LOT = 50.0
RATE = 0.035
TRADING_DAYS = 252


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(s, k, t, sigma, cp, r=RATE):
    if t <= 0 or sigma <= 0:
        return max(0.0, s - k) if cp == "C" else max(0.0, k - s)
    d1 = (math.log(s / k) + (r + sigma * sigma / 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if cp == "C":
        return s * norm_cdf(d1) - k * math.exp(-r * t) * norm_cdf(d2)
    return k * math.exp(-r * t) * norm_cdf(-d2) - s * norm_cdf(-d1)


def round_up(n, step=200):
    return math.ceil(n / step) * step


def round_down(n, step=200):
    return math.floor(n / step) * step


def load_data():
    df = pd.read_parquet(KLINE)
    hsi = df[df["code"] == "HK.800000"][["time_key", "close"]].rename(columns={"close": "hsi"})
    vhsi = df[df["code"] == "HK.800125"][["time_key", "close"]].rename(columns={"close": "vhsi"})
    m = hsi.merge(vhsi, on="time_key", how="inner").sort_values("time_key").reset_index(drop=True)
    m["date"] = pd.to_datetime(m["time_key"]).dt.strftime("%Y-%m-%d")
    return m


def settle(hsi0, vhsi, k_strike, l_strike, days_ahead, exit_hsi):
    """同一份 premium/結算模型：BS(VHSI, 實際日曆日) 收權金，到期內在值結算。"""
    t_years = (days_ahead * 7.0 / 5.0) / 365.0
    prem = 0.0
    intr = 0.0
    if k_strike:
        prem += bs_price(hsi0, k_strike, t_years, vhsi / 100.0, "C")
        intr += max(0.0, exit_hsi - k_strike)
    if l_strike:
        prem += bs_price(hsi0, l_strike, t_years, vhsi / 100.0, "P")
        intr += max(0.0, l_strike - exit_hsi)
    return prem - intr, prem, intr


def summarize(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    wins = [t for t in trades if t["pnl_pts"] > 0]
    pnls = [t["pnl_hkd"] for t in trades]
    avg_win = sum(t["pnl_hkd"] for t in wins) / len(wins) if wins else 0.0
    losers = [t for t in trades if t["pnl_pts"] <= 0]
    avg_loss = sum(t["pnl_hkd"] for t in losers) / len(losers) if losers else 0.0
    return {
        "n": n,
        "win_rate_pct": round(len(wins) / n * 100, 1),
        "avg_pnl_hkd": round(sum(pnls) / n, 2),
        "total_pnl_hkd": round(sum(pnls), 2),
        "avg_win_hkd": round(avg_win, 2),
        "avg_loss_hkd": round(avg_loss, 2),
        "worst_hkd": round(min(pnls), 2),
        "avg_premium_pts": round(sum(t["prem_pts"] for t in trades) / n, 2),
    }


def method_current(df, hold=5):
    """A. 現時法：每日重計 + 門檻 + 1×EM + 200 對齊。"""
    trades = []
    for i in range(len(df) - hold):
        v = df.loc[i, "vhsi"]
        hsi = df.loc[i, "hsi"]
        if v < 20:
            continue
        em = hsi * (v / 100.0) * math.sqrt(5 / TRADING_DAYS)
        if v < 22:
            K, L = None, round_down(hsi - em * 1.3)
        elif v <= 40:
            K, L = round_up(hsi + em), round_down(hsi - em)
        else:
            K, L = round_up(hsi + em), round_down(hsi - em)
        exit_hsi = df.loc[i + hold, "hsi"]
        pnl, prem, intr = settle(hsi, v, K, L, hold, exit_hsi)
        trades.append({"pnl_pts": pnl, "pnl_hkd": pnl * PTS_PER_LOT, "prem_pts": prem})
    return trades


def method_excel_weekly(df, mult=1.0, gated=False, hold=5):
    """B. Excel 法：逢週首交易日定帶（±mult × VHSI/√52），全週唔變，週末結算。"""
    trades = []
    i = 0
    while i <= len(df) - hold - 1:
        v = df.loc[i, "vhsi"]
        hsi = df.loc[i, "hsi"]
        if gated and v < 20:
            i += 1
            continue
        emw = hsi * (v / 100.0) / math.sqrt(52)
        K = round_up(hsi + emw * mult)
        L = round_down(hsi - emw * mult)
        exit_hsi = df.loc[i + hold, "hsi"]
        pnl, prem, intr = settle(hsi, v, K, L, hold, exit_hsi)
        trades.append({"pnl_pts": pnl, "pnl_hkd": pnl * PTS_PER_LOT, "prem_pts": prem})
        i += hold  # 一星期先入一次
    return trades


def method_daily_rolling(df, mult=1.0, gated=False, hold=5):
    """B2. 對照：同 Excel 尺度但每日都入（睇「每週一次」本身有冇影響）。"""
    trades = []
    for i in range(len(df) - hold):
        v = df.loc[i, "vhsi"]
        hsi = df.loc[i, "hsi"]
        if gated and v < 20:
            continue
        emw = hsi * (v / 100.0) * math.sqrt(5 / TRADING_DAYS)
        K = round_up(hsi + emw * mult)
        L = round_down(hsi - emw * mult)
        exit_hsi = df.loc[i + hold, "hsi"]
        pnl, prem, intr = settle(hsi, v, K, L, hold, exit_hsi)
        trades.append({"pnl_pts": pnl, "pnl_hkd": pnl * PTS_PER_LOT, "prem_pts": prem})
    return trades


def calibration(df, hold=5):
    """C. 校準：各帶寬喺 hold 日內包住現價嘅比例（唔做交易）。"""
    res = {"1xEM_252": [], "1xEM_365": [], "1.15xEM_252": [], "weekly_fixed": []}
    for i in range(len(df) - hold):
        v = df.loc[i, "vhsi"]
        hsi = df.loc[i, "hsi"]
        path = df.loc[i + 1:i + hold, "hsi"]
        exit_hsi = df.loc[i + hold, "hsi"]
        em252 = hsi * (v / 100.0) * math.sqrt(5 / TRADING_DAYS)
        em365 = hsi * (v / 100.0) * math.sqrt(5 / 365.0)
        res["1xEM_252"].append(abs(exit_hsi - hsi) <= em252)
        res["1xEM_365"].append(abs(exit_hsi - hsi) <= em365)
        res["1.15xEM_252"].append(abs(exit_hsi - hsi) <= em252 * 1.15)
    # 每週固定帶（1×EM_w）：全週逐日睇有冇穿
    i = 0
    while i <= len(df) - hold - 1:
        v = df.loc[i, "vhsi"]
        hsi = df.loc[i, "hsi"]
        emw = hsi * (v / 100.0) / math.sqrt(52)
        path = df.loc[i:i + hold, "hsi"]
        inside = all(abs(p - hsi) <= emw for p in path)
        res["weekly_fixed"].append(inside)
        i += hold
    return {k: round(sum(x) / len(x) * 100, 1) if x else None for k, x in res.items()}


def main():
    df = load_data()
    print(f"數據: {df['date'].iloc[0]} → {df['date'].iloc[-1]} ({len(df)} 交易日)")

    out = {"data": {"start": df["date"].iloc[0], "end": df["date"].iloc[-1], "days": len(df)}}

    variants = {
        "A_current_gated": method_current(df),
        "A_current_ungated": method_daily_rolling(df, mult=1.0, gated=False),
        "B_excel_weekly_1.0": method_excel_weekly(df, mult=1.0),
        "B_excel_weekly_1.15": method_excel_weekly(df, mult=1.15),
        "B_excel_weekly_1.0_gated": method_excel_weekly(df, mult=1.0, gated=True),
        "B_excel_weekly_1.15_gated": method_excel_weekly(df, mult=1.15, gated=True),
        "B2_daily_1.15": method_daily_rolling(df, mult=1.15, gated=False),
    }
    out["results"] = {k: summarize(t) for k, t in variants.items()}
    out["calibration_inside_pct"] = calibration(df)

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n{'變種':32s} {'筆數':>5s} {'勝率':>7s} {'期望$':>10s} {'總P&L$':>12s} {'最差$':>10s} {'平均權金pt':>9s}")
    for k, s in out["results"].items():
        if s.get("n"):
            print(f"{k:32s} {s['n']:5d} {s['win_rate_pct']:6.1f}% {s['avg_pnl_hkd']:10.0f} {s['total_pnl_hkd']:12.0f} {s['worst_hkd']:10.0f} {s['avg_premium_pts']:9.1f}")
    print("\n校準（5 日內收市價留喺帶內嘅比例，1σ 理論值 ~68%）：")
    for k, v in out["calibration_inside_pct"].items():
        print(f"  {k:20s} {v}%")
    print(f"\n✅ 已寫入 {OUT}")


if __name__ == "__main__":
    sys.exit(main())
