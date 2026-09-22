#!/usr/bin/env python3
"""美股期貨策略回測＋每日訊號（ES-NQ 相對價差／ES 大跌抄底／VIX regime 賣方溢價）。

數據：Desktop/db/IB/Kline/kline_ib_day.parquet（IB Gateway，364+ 日）
輸出：options_data/us_strategies.json
"""
import json
import sys
from pathlib import Path

import numpy as np


def _py(o):
    if isinstance(o, (np.bool_, np.integer)):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    return str(o)
import pandas as pd

HERE = Path(__file__).resolve().parent
IB_KLINE = Path("/home/workspace/Desktop/db/IB/Kline/kline_ib_day.parquet")
OUT = HERE / "options_data" / "us_strategies.json"

Z_ENTER = -1.0      # NQ 相對 ES z-score 入場（NQ 便宜 → 買 NQ 賣 ES）
Z_EXIT = -0.25      # 接近均值平倉
MAX_HOLD = 10       # 最長持有日
DIP_TH = -1.0       # ES 前日跌幅門檻 %


def load() -> dict[str, pd.DataFrame]:
    df = pd.read_parquet(IB_KLINE)
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return {s: g.sort_values("date").reset_index(drop=True)
            for s, g in df.groupby("symbol")}


def backtest_en_spread(es: pd.DataFrame, nq: pd.DataFrame) -> dict:
    m = es.merge(nq, on="date", suffixes=("_es", "_nq"))
    rel = np.log(m["close_nq"] / m["close_es"])
    mean = rel.rolling(20).mean()
    std = rel.rolling(20).std()
    z = (rel - mean) / std
    ret_es = m["close_es"].pct_change() * 100
    ret_nq = m["close_nq"].pct_change() * 100
    spread_ret = ret_nq - ret_es  # 買 NQ 賣 ES，等名義

    trades, i, n = [], 0, len(m)
    while i < n:
        if not np.isnan(z.iloc[i]) and z.iloc[i] <= Z_ENTER:
            j = i + 1
            while j < n:
                if z.iloc[j] >= Z_EXIT or j - i >= MAX_HOLD or z.iloc[j] <= -2.5:
                    break
                j += 1
            j = min(j, n - 1)
            pnl = spread_ret.iloc[i + 1:j + 1].sum()
            trades.append({"entry": m["date"].iloc[i], "exit": m["date"].iloc[j],
                           "z_entry": round(z.iloc[i], 2), "hold": j - i,
                           "pnl_pct": round(pnl, 3), "win": pnl > 0})
            i = j + 1
        else:
            i += 1
    return _sum(trades, "buy_nq_sell_es")


def backtest_en_spread_reverse(es: pd.DataFrame, nq: pd.DataFrame) -> dict:
    m = es.merge(nq, on="date", suffixes=("_es", "_nq"))
    rel = np.log(m["close_nq"] / m["close_es"])
    z = (rel - rel.rolling(20).mean()) / rel.rolling(20).std()
    spread_ret = (m["close_es"].pct_change() - m["close_nq"].pct_change()) * 100
    trades, i, n = [], 0, len(m)
    while i < n:
        if not np.isnan(z.iloc[i]) and z.iloc[i] >= -Z_ENTER:
            j = i + 1
            while j < n:
                if z.iloc[j] <= -Z_EXIT or j - i >= MAX_HOLD or z.iloc[j] >= 2.5:
                    break
                j += 1
            j = min(j, n - 1)
            pnl = spread_ret.iloc[i + 1:j + 1].sum()
            trades.append({"entry": m["date"].iloc[i], "exit": m["date"].iloc[j],
                           "z_entry": round(z.iloc[i], 2), "hold": j - i,
                           "pnl_pct": round(pnl, 3), "win": pnl > 0})
            i = j + 1
        else:
            i += 1
    return _sum(trades, "buy_es_sell_nq")


def backtest_es_dip(es: pd.DataFrame) -> dict:
    ret = es["close"].pct_change() * 100
    o2c = (es["close"] / es["open"] - 1) * 100
    trades = []
    for i in range(1, len(es) - 1):
        if ret.iloc[i] <= DIP_TH:
            trades.append({"entry": es["date"].iloc[i + 1],
                           "prev_ret": round(ret.iloc[i], 2),
                           "c2c": round(ret.iloc[i + 1], 3),
                           "o2c": round(o2c.iloc[i + 1], 3),
                           "win": ret.iloc[i + 1] > 0})
    res = _sum([{**t, "pnl_pct": t["c2c"]} for t in trades], "es_dip_c2c")
    o2c_stats = _sum([{**t, "pnl_pct": t["o2c"]} for t in trades], "es_dip_o2c")
    res["o2c_avg"] = o2c_stats["avg_pct"]
    res["o2c_win_rate"] = o2c_stats["win_rate"]
    res["trades"] = trades
    return res


def vix_regime_stats(data: dict[str, pd.DataFrame]) -> list[dict]:
    spx, vix = data.get("SPX"), data.get("VIX")
    if spx is None or vix is None:
        return []
    m = spx[["date", "close"]].merge(vix[["date", "close"]], on="date",
                                     suffixes=("_spx", "_vix"))
    lr = np.log(m["close_spx"]).diff()
    fwd_rv = (lr.iloc[::-1].rolling(20).std().iloc[::-1] * np.sqrt(252) * 100).shift(-21)
    buckets = {"<15": (0, 15), "15-20": (15, 20), "20-25": (20, 25), "25-40": (25, 99)}
    out = []
    for name, (lo, hi) in buckets.items():
        mask = (m["close_vix"] >= lo) & (m["close_vix"] < hi)
        rv = fwd_rv[mask].dropna()
        vx = m.loc[mask & fwd_rv.notna(), "close_vix"]
        if len(rv) < 5:
            continue
        prem = vx.values - rv.values
        out.append({"bucket": name, "n": int(len(rv)),
                    "mean_vix": round(float(vx.mean()), 1),
                    "realized_vol": round(float(rv.mean()), 1),
                    "seller_premium": round(float(prem.mean()), 1),
                    "premium_hit_rate": round(float((prem > 0).mean()) * 100, 1)})
    return out


def _sum(trades: list[dict], name: str) -> dict:
    n = len(trades)
    if not n:
        return {"name": name, "n": 0}
    pnls = [t["pnl_pct"] for t in trades]
    return {"name": name, "n": n,
            "win_rate": round(sum(t["win"] for t in trades) / n * 100, 1),
            "avg_pct": round(float(np.mean(pnls)), 3),
            "worst": round(float(min(pnls)), 3),
            "best": round(float(max(pnls)), 3),
            "total": round(float(sum(pnls)), 2),
            "trades": trades[-12:]}


def daily_signal(data: dict[str, pd.DataFrame]) -> dict:
    es, nq = data["ES"], data["NQ"]
    m = es.merge(nq, on="date", suffixes=("_es", "_nq")).tail(25)
    rel = np.log(m["close_nq"] / m["close_es"])
    z = (rel.iloc[-1] - rel.rolling(20).mean().iloc[-1]) / rel.rolling(20).std().iloc[-1]
    es_ret = es["close"].pct_change() * 100
    last_ret = es_ret.iloc[-1]
    sig = {"date": es["date"].iloc[-1], "en_z": round(float(z), 2),
           "es_prev_ret": round(float(last_ret), 2)}
    if z <= Z_ENTER:
        sig["spread"] = f"ENTRY 買NQ賣ES（z={z:.2f} ≤ {Z_ENTER}）"
    elif z >= -Z_ENTER:
        sig["spread"] = f"反向訊號 買ES賣NQ（z={z:.2f}）— 對照回測"
    else:
        sig["spread"] = f"觀望（z={z:.2f}，入場要 ≤ {Z_ENTER}）"
    sig["dip"] = ("ENTRY 明日抄底 ES（前日跌 ≥1%）" if last_ret <= DIP_TH
                  else f"無抄底訊號（前日 {last_ret:+.2f}%）")
    return sig


def main():
    data = load()
    es, nq = data["ES"], data["NQ"]
    spread = backtest_en_spread(es, nq)
    rev = backtest_en_spread_reverse(es, nq)
    dip = backtest_es_dip(es)
    regimes = vix_regime_stats(data)
    sig = daily_signal(data)
    out = {"generated": pd.Timestamp.now(tz="Asia/Hong_Kong").isoformat(),
           "params": {"z_enter": Z_ENTER, "z_exit": Z_EXIT, "max_hold": MAX_HOLD,
                      "dip_th": DIP_TH},
           "backtests": {"en_spread": spread, "en_spread_reverse": rev, "es_dip": dip},
           "vix_regime_stats": regimes, "signal": sig}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=_py))
    print(f"ES-NQ 買NQ賣ES: n={spread['n']} 勝率 {spread.get('win_rate')}% "
          f"平均 {spread.get('avg_pct')}% 最差 {spread.get('worst')}%")
    print(f"反向 買ES賣NQ:   n={rev['n']} 勝率 {rev.get('win_rate')}% "
          f"平均 {rev.get('avg_pct')}%")
    print(f"ES 抄底 c2c:     n={dip['n']} 勝率 {dip.get('win_rate')}% "
          f"平均 {dip.get('avg_pct')}% (o2c {dip.get('o2c_avg')}% / {dip.get('o2c_win_rate')}%)")
    for r in regimes:
        print(f"VIX {r['bucket']}: n={r['n']} 均VIX {r['mean_vix']} "
              f"實現 {r['realized_vol']} 賣方溢價 {r['seller_premium']:+.1f} "
              f"命中率 {r['premium_hit_rate']}%")
    print("今日訊號:", json.dumps(sig, ensure_ascii=False))
    print(f"written {OUT}")


if __name__ == "__main__":
    main()
