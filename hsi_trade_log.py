#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""生成恒指 Short Strangle 落盤記錄（回測逐筆，精簡欄位）→ hsi_trade_log.json

- 日版：每日重計 + VHSI 門檻 + 1×EM + 200 對齊，持 5 個交易日結算
- 週版：Excel 週帶（1.15 × VHSI/√52），逢週首交易日定帶、全週唔變，持 5 個交易日結算
只保留重要欄位：日期 / 模式 / K / L / 權金(點) / 盈虧(HKD) / 勝負
"""
import json, math
import pandas as pd
from pathlib import Path

BASE = Path(__file__).parent
KLINE = Path("/home/workspace/Desktop/db/Futu/Kline/kline_index.parquet")
OUT = BASE / "hsi_trade_log.json"

TRADING_DAYS = 252
RATE = 0.035
PTS_PER_LOT = 50.0
HOLD = 5


def norm_cdf(x):
    import math
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(s, k, t, sigma, cp):
    if s <= 0 or k <= 0 or t <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(s / k) + (RATE + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if cp == "C":
        return s * norm_cdf(d1) - k * math.exp(-RATE * t) * norm_cdf(d2)
    return k * math.exp(-RATE * t) * norm_cdf(-d2) - s * norm_cdf(-d1)


def round_up(n, step=200):
    return int(math.ceil(n / step) * step)


def round_down(n, step=200):
    return int(math.floor(n / step) * step)


def load_data():
    df = pd.read_parquet(KLINE)
    hsi = df[df["code"] == "HK.800000"][["time_key", "close"]].rename(columns={"close": "hsi"})
    vhsi = df[df["code"] == "HK.800125"][["time_key", "close"]].rename(columns={"close": "vhsi"})
    m = hsi.merge(vhsi, on="time_key", how="inner").sort_values("time_key").reset_index(drop=True)
    m["date"] = pd.to_datetime(m["time_key"]).dt.strftime("%Y-%m-%d")
    m["weekday"] = pd.to_datetime(m["time_key"]).dt.weekday
    return m


def settle(hsi0, vhsi, K, L, hold, exit_hsi):
    t_years = (hold * 7.0 / 5.0) / 365.0
    prem = 0.0
    intr = 0.0
    if K:
        prem += bs_price(hsi0, K, t_years, vhsi / 100.0, "C")
        intr += max(0.0, exit_hsi - K)
    if L:
        prem += bs_price(hsi0, L, t_years, vhsi / 100.0, "P")
        intr += max(0.0, L - exit_hsi)
    return prem - intr, prem


def mode_label(m):
    return {"Full": "Full", "PutOnly": "PutOnly", "HighVol": "Full"}.get(m, m)


def daily_trades(df):
    trades = []
    for i in range(len(df) - HOLD):
        v = float(df.loc[i, "vhsi"])
        hsi = float(df.loc[i, "hsi"])
        if int(df.loc[i, "weekday"]) == 4:  # 星期五唔開新倉
            continue
        if v < 20:
            continue
        em = hsi * (v / 100.0) * math.sqrt(5 / TRADING_DAYS)
        if v < 22:
            K, L = None, round_down(hsi - em * 1.3)
            mode = "PutOnly"
        else:
            K, L = round_up(hsi + em), round_down(hsi - em)
            mode = "Full"
        exit_hsi = float(df.loc[i + HOLD, "hsi"])
        pnl, prem = settle(hsi, v, K, L, HOLD, exit_hsi)
        trades.append({
            "date": df.loc[i, "date"],
            "mode": mode,
            "K": K, "L": L,
            "prem_pts": round(prem, 1),
            "pnl_hkd": round(pnl * PTS_PER_LOT, 0),
            "win": pnl > 0,
        })
    return trades


def weekly_trades(df):
    trades = []
    i = 0
    while i <= len(df) - HOLD - 1:
        v = float(df.loc[i, "vhsi"])
        hsi = float(df.loc[i, "hsi"])
        emw = hsi * (v / 100.0) / math.sqrt(52)
        K = round_up(hsi + emw * 1.15)
        L = round_down(hsi - emw * 1.15)
        exit_hsi = float(df.loc[i + HOLD, "hsi"])
        pnl, prem = settle(hsi, v, K, L, HOLD, exit_hsi)
        trades.append({
            "date": df.loc[i, "date"],
            "mode": "週帶",
            "K": K, "L": L,
            "prem_pts": round(prem, 1),
            "pnl_hkd": round(pnl * PTS_PER_LOT, 0),
            "win": pnl > 0,
        })
        i += HOLD  # 一星期一次
    return trades


def main():
    df = load_data()
    d = daily_trades(df)
    w = weekly_trades(df)
    out = {
        "generated_at": pd.Timestamp.now().isoformat(),
        "data": {"start": df["date"].iloc[0], "end": df["date"].iloc[-1]},
        "daily": d,
        "weekly": w,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"日版 {len(d)} 筆 / 週版 {len(w)} 筆 → {OUT}")


if __name__ == "__main__":
    main()
