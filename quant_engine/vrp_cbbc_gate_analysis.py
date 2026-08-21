#!/usr/bin/env python3
"""
vrp_cbbc_gate_analysis.py — VRP Gate 對鐵鷹回測效果分析

將真回測引擎出嘅每一筆 condor trade，對返 atm_iv_history 同 quotes，
計出個案 VRP（開倉時 IV − 持有期已實現波幅），再睇：
- VRP > 0 時嘅交易勝率同期望值 vs VRP ≤ 0
- 如果只做 VRP > threshold 嘅倉，整體結果有冇改善
"""

import sys, os, json, math
from datetime import date, datetime, timedelta
from collections import defaultdict

import pandas as pd
import numpy as np

sys.path.insert(0, "/home/workspace/stock-analysis")

# ── 載入數據 ──────────────────────────────────────────────────

iv_df = pd.read_parquet("options_data/atm_iv_history.parquet")
iv_df["date"] = pd.to_datetime(iv_df["date"]).dt.date

with open("imported/quotes.json") as f:
    quotes_raw = json.load(f)
quotes = quotes_raw["quotes"]  # { "YYYY-MM-DD": { code: {close} } }

with open("quant_engine/options_backtest_results.json") as f:
    bt = json.load(f)
condor_result = next(r for r in bt["results"] if r["key"] == "iron_condor")
trades = condor_result["trades_sample"]

# ── 輔助函數 ──────────────────────────────────────────────────

def stock_close(code: str, dt: date) -> float | None:
    """攞某日收市價（冇就揾最近嘅前一日）"""
    for offset in range(7):  # 最多回望 7 日
        d = dt - timedelta(days=offset)
        ds = d.isoformat()
        if ds in quotes and code in quotes[ds]:
            return quotes[ds][code]["close"]
    return None

def realized_vol(prices: list[float], annual: bool = True) -> float:
    """由一列價格計已實現波幅"""
    if len(prices) < 5:
        return None
    log_rets = [math.log(prices[i] / prices[i-1]) for i in range(1, len(prices))]
    daily_std = math.sqrt(np.var(log_rets, ddof=1)) if len(log_rets) >= 2 else 0
    if annual:
        return daily_std * math.sqrt(252) * 100
    return daily_std * 100

def get_atm_iv(code: str, dt: date, target_dte: int) -> float | None:
    """由 atm_iv_history 揾最接近 DTE 嘅 ATM IV"""
    sub = iv_df[(iv_df["stock_code"] == code) & (iv_df["date"] == dt)]
    if sub.empty:
        # 回望最多 5 個交易日
        for offset in range(1, 6):
            d = dt - timedelta(days=offset)
            sub = iv_df[(iv_df["stock_code"] == code) & (iv_df["date"] == d)]
            if not sub.empty:
                break
    if sub.empty:
        return None
    # 揀最近 DTE
    sub = sub.copy()
    sub["dte_diff"] = abs(sub["dte"] - target_dte)
    best = sub.sort_values("dte_diff").iloc[0]
    return float(best["atm_iv"])

def get_iv_rank(code: str, dt: date, iv_val: float) -> float | None:
    """計 IV Rank（IV 喺該股歷史 IV 中嘅百分位）"""
    hist = iv_df[(iv_df["stock_code"] == code) & (iv_df["date"] <= dt)]
    if hist.empty or len(hist) < 20:
        return None
    all_iv = hist["atm_iv"].values
    rank = (all_iv < iv_val).mean() * 100
    return round(rank, 1)

# ── 主分析 ────────────────────────────────────────────────────

print("=" * 60)
print("VRP Gate 鐵鷹回測分析")
print("=" * 60)

records = []
matched = 0
unmatched_dates = set()

for t in trades:
    code = t["code"]
    open_dt = datetime.strptime(t["open"], "%Y-%m-%d").date()
    dte = t["dte"]
    exit_dt = datetime.strptime(t["exit"], "%Y-%m-%d").date()

    # ATM IV at open
    iv_open = get_atm_iv(code, open_dt, dte)
    if iv_open is None:
        print(f"  ⚠ {code} {open_dt}: 無 ATM IV")
        continue

    # Realized vol over holding period
    prices = []
    for offset in range((exit_dt - open_dt).days + 5):
        d = open_dt + timedelta(days=offset)
        p = stock_close(code, d)
        if p is not None:
            prices.append(p)
    rv = realized_vol(prices) if len(prices) >= 5 else None

    if rv is None:
        print(f"  ⚠ {code} {open_dt}: 報價唔夠計 RV（{len(prices)} 日）")
        continue

    vrp = iv_open - rv
    ivr = get_iv_rank(code, open_dt, iv_open)

    records.append({
        **t,
        "iv_open": round(iv_open, 1),
        "rv": round(rv, 1),
        "vrp": round(vrp, 1),
        "iv_rank": round(ivr, 1) if ivr is not None else None,
        "vrp_flag": "POS" if vrp > 0 else "NEG",
    })
    matched += 1

if not records:
    print("\n❌ 無任何 trade 匹配到 VRP 數據，停。")
    sys.exit(1)

df = pd.DataFrame(records)
print(f"\n📊 匹配成功: {matched}/{len(trades)} 筆")
print(f"   已實現波幅可計數: {matched}")

# ── 分組比較 ──────────────────────────────────────────────────

def stats(subset: pd.DataFrame, label: str):
    n = len(subset)
    if n == 0:
        print(f"\n{label}: 0 筆交易（無數據）")
        return
    win_rate = (subset["pnl"] > 0).mean() * 100
    avg_pnl = subset["pnl"].mean()
    total_pnl = subset["pnl"].sum()
    worst = subset["pnl"].min()
    best = subset["pnl"].max()
    avg_ret = subset["ret_pct"].mean()
    print(f"\n{label} ({n} 筆):")
    print(f"   勝率: {win_rate:.1f}%")
    print(f"   平均 P&L: ${avg_pnl:,.0f}")
    print(f"   總 P&L: ${total_pnl:,.0f}")
    print(f"   平均回報: {avg_ret:.1f}%")
    print(f"   最差: ${worst:,.0f}  最佳: ${best:,.0f}")

# 整體
stats(df, "📦 全部鐵鷹")

# VRP > 0 vs VRP ≤ 0
pos = df[df["vrp"] > 0]
neg = df[df["vrp"] <= 0]
stats(pos, "🟢 VRP > 0（IV 貴過實况）")
stats(neg, "🔴 VRP ≤ 0（IV 平過／等於實况）")

# VRP 分桶
bins = [(-999, -5, "VRP < -5"), (-5, 0, "VRP -5 ~ 0"), (0, 5, "VRP 0 ~ 5"),
        (5, 15, "VRP 5 ~ 15"), (15, 999, "VRP > 15")]
for lo, hi, label in bins:
    sub = df[(df["vrp"] > lo) & (df["vrp"] <= hi)]
    if len(sub) > 0:
        stats(sub, f"📊 {label}")

# ── IV Rank 分組 ──────────────────────────────────────────────

print("\n" + "=" * 60)
print("IV Rank Gate（前瞻過濾，唔係事後 VRP）")
print("=" * 60)

df_with_ivr = df[df["iv_rank"].notna()]
stats(df_with_ivr, f"📦 有 IV Rank（{len(df_with_ivr)} 筆）")

for thresh, label in [(50, "IVR > 50"), (60, "IVR > 60"), (70, "IVR > 70"), (80, "IVR > 80")]:
    sub = df_with_ivr[df_with_ivr["iv_rank"] > thresh]
    stats(sub, f"📊 {label}")

# ── 交叉：IVR 高 + VRP 正 ───────────────────────────────────

print("\n" + "=" * 60)
print("組合 Gate：IV Rank > 60 AND VRP > 0")
print("=" * 60)
combo = df_with_ivr[(df_with_ivr["iv_rank"] > 60) & (df_with_ivr["vrp"] > 0)]
stats(combo, "🎯 IVR>60 + VRP>0")

# ── VRP 分佈圖 ───────────────────────────────────────────────

print("\n" + "=" * 60)
print("VRP 分佈")
print("=" * 60)
print(f"   平均 VRP: {df.vrp.mean():.1f}")
print(f"   中位 VRP: {df.vrp.median():.1f}")
print(f"   VRP > 0 佔比: {(df.vrp > 0).mean()*100:.1f}%")
print(f"   標準差: {df.vrp.std():.1f}")

# ── 輸出原始 CSV ──────────────────────────────────────────────

csv_path = "vrp_condor_analysis.csv"
df.to_csv(csv_path, index=False)
print(f"\n✅ 原始數據已寫入: {csv_path}")
print(f"   欄位: {', '.join(df.columns)}")
