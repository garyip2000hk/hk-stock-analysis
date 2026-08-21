"""
hsi_bs_backtest.py — 恒指 Short Strangle Black-Scholes 回測

用 VHSI + 恒指歷史（OpenD kline_index.parquet）+ Black-Scholes 模擬，
評估 gsmart-box 恒指 Short Strangle 嘅勝率 / 期望值 / 尾部風險。

做法（同 gsmart-box strategy.ts 一致）：
  - VHSI 分級：<20 SKIP、20–22 PutOnly、22–40 Full、>40 HighVol
  - 行使價：EM = HSI × VHSI/100 × √(5/252)
            K (Short Call) = roundUp(HSI+EM, 200)
            L (Short Put)  = roundDown(HSI−EM×putMult, 200)
  - 星期五唔開新倉
  - 持倉 N 個交易日後用內在值結算（BS 用 VHSI 做隱含波幅定價）

限制（要知）：
  - 用 VHSI 做「平坦」隱含波幅（無 skew、無期限結構、C/P 同一個 vol）
  - 唔計牛熊證避位（CBBC 歷史暫時得 1 日）
  - 唔計買賣差價、佣金、保證金機會成本
  - 結算用收市價（非結算價）
"""
import json
import math
from pathlib import Path

import pandas as pd

import bs  # 重用現有 Black-Scholes（無風險利率 3.5%）

KLINE_PATH = Path("/home/workspace/Desktop/db/Futu/Kline/kline_index.parquet")
OUT_PATH = Path(__file__).parent / "hsi_bs_backtest.json"

HSI = "HK.800000"
VHSI = "HK.800125"
MULT = 50.0          # 恒指期權每點 $50
TRADING_DAYS = 252


def detect_mode(vhsi):
    if vhsi is None or (isinstance(vhsi, float) and math.isnan(vhsi)):
        return None
    if vhsi < 20:
        return "SKIP"
    if vhsi < 22:
        return "PutOnly"
    if vhsi <= 40:
        return "Full"
    return "HighVol"


def round_up(n, step=200):
    return math.ceil(n / step) * step


def round_down(n, step=200):
    return math.floor(n / step) * step


def calc_strikes(hsi, vhsi, mode):
    """回 {em, K, L}。K/L = 行使價（點），None 代表唔做嗰邊。"""
    em = hsi * (vhsi / 100) * math.sqrt(5 / TRADING_DAYS)
    if mode == "SKIP":
        return {"em": em, "K": None, "L": None}
    if mode == "PutOnly":
        L = round_down(hsi - em * 1.3)
        return {"em": em, "K": None, "L": L}
    # Full / HighVol
    K = round_up(hsi + em)
    L = round_down(hsi - em)
    return {"em": em, "K": K, "L": L}


def load_history():
    df = pd.read_parquet(KLINE_PATH)
    df["time_key"] = pd.to_datetime(df["time_key"])

    def one(code, name):
        d = df[df["code"] == code][["time_key", "close"]].copy()
        d = d.rename(columns={"close": name})
        return d.sort_values("time_key").drop_duplicates("time_key")

    hsi = one(HSI, "hsi")
    vhsi = one(VHSI, "vhsi")
    m = pd.merge(hsi, vhsi, on="time_key", how="outer").sort_values("time_key")
    m["hsi"] = m["hsi"].ffill()
    m["vhsi"] = m["vhsi"].ffill()
    m = m.dropna(subset=["hsi", "vhsi"])
    m["weekday"] = m["time_key"].dt.weekday  # 4 = 星期五
    m = m.reset_index(drop=True)
    return m


def simulate(daily, dte, gating):
    """逐日開倉、持 dte 個交易日後結算。回 trades list。"""
    trades = []
    T = dte / TRADING_DAYS
    n = len(daily)
    for i in range(n - dte):
        e = daily.iloc[i]
        x = daily.iloc[i + dte]
        vhsi = float(e["vhsi"])
        hsi = float(e["hsi"])
        entry_day = e["time_key"]
        exit_day = x["time_key"]

        # 星期五唔開新倉
        if int(e["weekday"]) == 4:
            continue

        mode = detect_mode(vhsi) if gating else "Full"
        if mode is None:
            continue

        strikes = calc_strikes(hsi, vhsi, mode)

        rec = {
            "entry": entry_day.strftime("%Y-%m-%d"),
            "exit": exit_day.strftime("%Y-%m-%d"),
            "mode": mode,
            "hsi": round(hsi, 1),
            "vhsi": round(vhsi, 2),
            "K": strikes["K"],
            "L": strikes["L"],
        }

        if mode == "SKIP":
            rec["pnl_hkd"] = 0.0
            rec["skipped"] = True
            trades.append(rec)
            continue

        # 定價（用 VHSI 做平坦 IV）
        vol = vhsi / 100
        legs = []
        if strikes["K"] is not None:
            p = bs.price(hsi, strikes["K"], T, vol, "C")
            legs.append(("C", strikes["K"], p))
        if strikes["L"] is not None:
            p = bs.price(hsi, strikes["L"], T, vol, "P")
            legs.append(("P", strikes["L"], p))

        prem_pts = sum(p for _, _, p in legs if p is not None)
        if prem_pts is None or prem_pts <= 0:
            rec["pnl_hkd"] = 0.0
            rec["skipped"] = True
            trades.append(rec)
            continue

        sT = float(x["hsi"])
        intr = 0.0
        for cp, k, _ in legs:
            if k is None:
                continue
            intr += max(sT - k, 0.0) if cp == "C" else max(k - sT, 0.0)

        pnl_pts = prem_pts - intr
        pnl_hkd = pnl_pts * MULT

        # BS 贏面 = P(L < S_T < K)（風險中性）
        pw = 1.0
        if strikes["K"] is not None:
            pw_k = bs.prob_below(hsi, strikes["K"], T, vol)
            if pw_k is not None:
                pw = pw_k
        if strikes["L"] is not None:
            pw_l = bs.prob_below(hsi, strikes["L"], T, vol)
            if pw_l is not None:
                pw = (pw if strikes["K"] is not None else 1.0) - pw_l

        rec.update({
            "prem_pts": round(prem_pts, 2),
            "intr": round(intr, 2),
            "pnl_pts": round(pnl_pts, 2),
            "pnl_hkd": round(pnl_hkd, 2),
            "bs_win_prob": round(pw, 4),
            "skipped": False,
        })
        trades.append(rec)

    return trades


def summarize(trades, label):
    entered = [t for t in trades if not t.get("skipped")]
    if not entered:
        return {"label": label, "entered": 0}
    pnls = [t["pnl_hkd"] for t in entered]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    n = len(pnls)

    def avg(xs):
        return round(sum(xs) / len(xs), 2) if xs else 0.0

    # 分模式
    by_mode = {}
    for t in entered:
        m = t["mode"]
        by_mode.setdefault(m, []).append(t["pnl_hkd"])

    return {
        "label": label,
        "entered": n,
        "skipped": len(trades) - n,
        "win_rate_pct": round(len(wins) / n * 100, 1),
        "avg_pnl_hkd": avg(pnls),
        "total_pnl_hkd": round(total, 2),
        "avg_win": avg(wins),
        "avg_loss": avg(losses),
        "profit_factor": round(abs(sum(wins) / sum(losses)), 2) if sum(losses) else None,
        "max_win": round(max(pnls), 2),
        "max_loss": round(min(pnls), 2),
        "by_mode": {m: {
            "n": len(v),
            "win_rate_pct": round(len([p for p in v if p > 0]) / len(v) * 100, 1),
            "avg_pnl_hkd": avg(v),
            "total_pnl_hkd": round(sum(v), 2),
            "max_loss": round(min(v), 2),
        } for m, v in sorted(by_mode.items())},
    }


def run():
    daily = load_history()
    out = {
        "generated_at": pd.Timestamp.now().isoformat(),
        "data": {
            "start": daily["time_key"].min().strftime("%Y-%m-%d"),
            "end": daily["time_key"].max().strftime("%Y-%m-%d"),
            "trading_days": int(len(daily)),
            "hsi_last": round(float(daily["hsi"].iloc[-1]), 1),
            "vhsi_last": round(float(daily["vhsi"].iloc[-1]), 2),
            "multiplier_hkd": MULT,
        },
        "dte": 5,
        "results": {},
    }
    for dte in [5, 10, 20]:
        gated = summarize(simulate(daily, dte, gating=True), f"gated_dte{dte}")
        ungated = summarize(simulate(daily, dte, gating=False), f"always_on_dte{dte}")
        out["results"][f"dte{dte}"] = {"gated": gated, "always_on": ungated}

    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))

    # 打印摘要
    print(f"數據: {out['data']['start']} → {out['data']['end']} ({out['data']['trading_days']} 交易日)")
    print(f"恒指最後 {out['data']['hsi_last']}，VHSI 最後 {out['data']['vhsi_last']}\n")
    for dte in [5, 10, 20]:
        r = out["results"][f"dte{dte}"]
        print(f"═══ DTE {dte}（持 {dte} 交易日）═══")
        for key, tag in [("gated", "有 VHSI 分級"), ("always_on", "無腦長期 Short Strangle")]:
            s = r[key]
            if s.get("entered", 0) == 0:
                print(f"  {tag}: 冇交易")
                continue
            print(f"  {tag}:")
            print(f"    交易 {s['entered']} 筆（skip {s['skipped']}）｜勝率 {s['win_rate_pct']}%｜"
                  f"每筆期望 ${s['avg_pnl_hkd']}｜總 ${s['total_pnl_hkd']}")
            print(f"    平均贏 ${s['avg_win']}／平均輸 ${s['avg_loss']}｜"
                  f"盈虧比 {s['profit_factor']}｜最差單筆 ${s['max_loss']}")
            for m, v in s.get("by_mode", {}).items():
                print(f"      {m}: {v['n']} 筆｜勝率 {v['win_rate_pct']}%｜"
                      f"期望 ${v['avg_pnl_hkd']}｜總 ${v['total_pnl_hkd']}｜最差 ${v['max_loss']}")
        print()

    return out


if __name__ == "__main__":
    run()
