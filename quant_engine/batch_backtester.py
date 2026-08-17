"""
batch_backtester.py — 批量回測引擎
讀取 Futu K-line 數據，對所有已註冊策略逐一回測，輸出 tier 評級結果。

Tier 評級標準 (量化策略):
  S tier: Sharpe > 1.5 且 max_dd < 15% 且 win_rate > 55%
  A tier: Sharpe > 1.0 且 max_dd < 20% 且 win_rate > 50%
  B tier: Sharpe > 0.5 且 max_dd < 30% 且 win_rate > 45%
  C tier: 其他

Usage:
  python3 batch_backtester.py              # 跑所有策略
  python3 batch_backtester.py --tier S,A   # 只顯示 S/A tier
  python3 batch_backtester.py --top 10     # 只顯示 top 10
"""

import json
import os
import sys
import argparse
import importlib
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path

# Add paths
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategies"))

from backtester import BacktestEngine
from strategy_registry import get_all_strategies

KLINE_PATH = "/home/workspace/Desktop/db/Futu/Kline/kline_day.parquet"
QUOTES_DIR = "/home/workspace/Desktop/db/quotes"
OUTPUT_PATH = "/home/workspace/stock-analysis/quant_engine/batch_backtest_results.json"
TIER_PATH = "/home/workspace/stock-analysis/quant_engine/strategy_tiers.json"

INITIAL_CAPITAL = 1_000_000


def load_futu_kline() -> tuple:
    """Load Futu K-line data and convert to quotes_df + close_pivot format."""
    df = pd.read_parquet(KLINE_PATH)
    df["code"] = df["code"].str.replace("HK.", "", regex=False).str.lstrip("0")
    # Keep codes as-is (numeric strings like "1", "5", "700", "9988")
    df["date"] = pd.to_datetime(df["time_key"]).dt.date
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["open"] = pd.to_numeric(df["open"], errors="coerce")
    df["high"] = pd.to_numeric(df["high"], errors="coerce")
    df["low"] = pd.to_numeric(df["low"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce")

    # Build close pivot (index=date, columns=code, values=close)
    close_pivot = df.pivot_table(index="date", columns="code", values="close")
    close_pivot = close_pivot.sort_index()

    # Build quotes_df in the format strategies expect
    df["vol"] = df["volume"]  # alias for strategies that expect "vol"
    quotes_df = df[["date", "code", "open", "high", "low", "close", "volume", "vol", "turnover"]].copy()
    quotes_df = quotes_df.sort_values(["date", "code"])

    # Names map (basic)
    names_map = {}
    try:
        names_file = "/home/workspace/Desktop/db/Futu/Snapshot/snapshot_20260815.parquet"
        if os.path.exists(names_file):
            snap = pd.read_parquet(names_file)
            for _, row in snap.iterrows():
                code = str(row.get("code", "")).replace("HK.", "").lstrip("0")
                name = row.get("name", "")
                if code and name:
                    names_map[code] = str(name)
    except:
        pass

    return quotes_df, close_pivot, names_map


def classify_tier(metrics: dict) -> str:
    """Classify strategy performance into S/A/B/C tier."""
    if not metrics:
        return "C"

    sharpe = metrics.get("sharpe_ratio", 0)
    max_dd = abs(metrics.get("max_drawdown_pct", 100))
    win_rate = metrics.get("win_rate_pct", 0)
    sortino = metrics.get("sortino_ratio", 0)
    calmar = metrics.get("calmar_ratio", 0)
    pf = metrics.get("profit_factor", 0)

    # Composite score (weighted)
    score = (
        sharpe * 25 +          # Sharpe 最重要
        sortino * 15 +          # 下行風險
        calmar * 10 +           # 回撤調整回報
        win_rate * 0.5 +        # 勝率
        min(pf, 5) * 10 +       # 利潤因子 (cap at 5)
        max(0, 20 - max_dd) * 2 # 回撤懲罰 (dd > 20% 逐漸扣分)
    )

    # Tier boundaries
    if sharpe > 1.5 and max_dd < 15 and win_rate > 55:
        return "S"
    elif sharpe > 1.0 and max_dd < 20 and win_rate > 50:
        return "A"
    elif sharpe > 0.5 and max_dd < 30 and win_rate > 45:
        return "B"
    else:
        return "C"


def run_batch(strategies=None, verbose=True):
    """Run all registered strategies and return tiered results."""
    quotes_df, close_pivot, names_map = load_futu_kline()
    universe = sorted(close_pivot.columns.tolist())

    if verbose:
        print(f"📊 數據: {len(universe)} 隻股票, {len(close_pivot)} 個交易日")
        print(f"   期間: {close_pivot.index[0]} → {close_pivot.index[-1]}")
        print()

    if strategies is None:
        strategies = get_all_strategies()

    engine = BacktestEngine(initial_capital=INITIAL_CAPITAL, position_pct=0.1,
                            commission=0.001, slippage=0.001)

    results = []
    for i, (name, strat_cls, category, description) in enumerate(strategies, 1):
        try:
            strat = strat_cls()
            if verbose:
                print(f"[{i}/{len(strategies)}] 回測: {name} ({category})...", end=" ", flush=True)

            result = engine.run(strat, quotes_df, close_pivot, names_map)
            tier = classify_tier(result.get("metrics", {}))
            m = result.get("metrics", {})

            entry = {
                "name": name,
                "category": category,
                "description": description,
                "tier": tier,
                "composite_score": round(
                    m.get("sharpe_ratio", 0) * 25 +
                    m.get("sortino_ratio", 0) * 15 +
                    m.get("calmar_ratio", 0) * 10 +
                    m.get("win_rate_pct", 0) * 0.5 +
                    min(m.get("profit_factor", 0), 5) * 10 +
                    max(0, 20 - abs(m.get("max_drawdown_pct", 0))) * 2
                , 1),
                "sharpe": m.get("sharpe_ratio", 0),
                "sortino": m.get("sortino_ratio", 0),
                "calmar": m.get("calmar_ratio", 0),
                "total_return_pct": m.get("total_return_pct", 0),
                "annual_return_pct": m.get("annual_return_pct", 0),
                "max_drawdown_pct": m.get("max_drawdown_pct", 0),
                "win_rate_pct": m.get("win_rate_pct", 0),
                "profit_factor": m.get("profit_factor", 0),
                "total_trades": m.get("total_trades", 0),
                "winning_trades": m.get("winning_trades", 0),
                "losing_trades": m.get("losing_trades", 0),
                "avg_win": m.get("avg_win", 0),
                "avg_loss": m.get("avg_loss", 0),
                "start_date": m.get("start_date", ""),
                "end_date": m.get("end_date", ""),
                "trading_days": m.get("trading_days", 0),
                "params": result.get("params", {}),
                "backtested_at": result.get("run_at", ""),
            }
            results.append(entry)

            if verbose:
                tier_emoji = {"S": "🏆", "A": "✅", "B": "🟡", "C": "🔴"}.get(tier, "❓")
                sr = m.get("sharpe_ratio", 0)
                wr = m.get("win_rate_pct", 0)
                dd = m.get("max_drawdown_pct", 0)
                ret = m.get("total_return_pct", 0)
                print(f"{tier_emoji} {tier} | Sharpe {sr:.2f} | WR {wr:.0f}% | DD {dd:.1f}% | Ret {ret:+.1f}%")

        except Exception as e:
            if verbose:
                print(f"❌ 錯誤: {e}")
            results.append({
                "name": name, "category": category, "description": description,
                "tier": "C", "error": str(e), "composite_score": 0,
            })

    # Sort by composite score descending
    results.sort(key=lambda x: x.get("composite_score", 0), reverse=True)

    # Assign rank
    for i, r in enumerate(results, 1):
        r["rank"] = i

    return results


def save_results(results: list):
    """Save batch results and tier summary."""
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    # Full results
    output = {
        "run_at": datetime.now().isoformat(),
        "total_strategies": len(results),
        "tier_counts": {},
        "results": results,
    }
    for r in results:
        t = r.get("tier", "C")
        output["tier_counts"][t] = output["tier_counts"].get(t, 0) + 1

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    # Tier summary (lighter, for GSmart Box API)
    tier_summary = {
        "updated_at": datetime.now().isoformat(),
        "tiers": {}
    }
    for r in results:
        t = r.get("tier", "C")
        if t not in tier_summary["tiers"]:
            tier_summary["tiers"][t] = []
        tier_summary["tiers"][t].append({
            "rank": r.get("rank"),
            "name": r["name"],
            "category": r["category"],
            "description": r["description"],
            "composite_score": r.get("composite_score", 0),
            "sharpe": r.get("sharpe", 0),
            "win_rate_pct": r.get("win_rate_pct", 0),
            "max_drawdown_pct": r.get("max_drawdown_pct", 0),
            "total_return_pct": r.get("total_return_pct", 0),
            "annual_return_pct": r.get("annual_return_pct", 0),
            "total_trades": r.get("total_trades", 0),
            "profit_factor": r.get("profit_factor", 0),
        })

    with open(TIER_PATH, "w", encoding="utf-8") as f:
        json.dump(tier_summary, f, ensure_ascii=False, indent=2)

    return output


def main():
    parser = argparse.ArgumentParser(description="批量回測引擎")
    parser.add_argument("--tier", type=str, default="", help="只顯示指定 tier (e.g. S,A)")
    parser.add_argument("--top", type=int, default=0, help="只顯示 top N 策略")
    parser.add_argument("--quiet", action="store_true", help="安靜模式")
    args = parser.parse_args()

    results = run_batch(verbose=not args.quiet)

    if args.tier:
        allowed = set(args.tier.upper().split(","))
        results = [r for r in results if r.get("tier") in allowed]

    if args.top > 0:
        results = results[:args.top]

    output = save_results(results)

    print(f"\n{'='*60}")
    print(f"📊 批量回測完成: {output['total_strategies']} 個策略")
    print(f"   S tier: {output['tier_counts'].get('S', 0)}")
    print(f"   A tier: {output['tier_counts'].get('A', 0)}")
    print(f"   B tier: {output['tier_counts'].get('B', 0)}")
    print(f"   C tier: {output['tier_counts'].get('C', 0)}")
    print(f"\n📁 結果已儲存:")
    print(f"   完整: {OUTPUT_PATH}")
    print(f"   分級: {TIER_PATH}")

    # Print top 5
    print(f"\n🏆 Top 5 策略:")
    for r in results[:5]:
        tier_emoji = {"S": "🏆", "A": "✅", "B": "🟡", "C": "🔴"}.get(r.get("tier"), "❓")
        print(f"   {r['rank']}. {tier_emoji} [{r['tier']}] {r['name']} | "
              f"Sharpe {r.get('sharpe', 0):.2f} | WR {r.get('win_rate_pct', 0):.0f}% | "
              f"DD {r.get('max_drawdown_pct', 0):.1f}% | Ret {r.get('total_return_pct', 0):+.1f}%")


if __name__ == "__main__":
    main()
