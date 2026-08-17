"""
run_lab.py — 量化策略實驗室主程序
讀取數據 → 跑所有策略回測 → 輸出結果到 JSON
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from quant_engine.data_loader import load_quotes
from quant_engine.backtester import BacktestEngine
from quant_engine.strategies.momentum_strategy import MomentumStrategy
from quant_engine.strategies.mean_reversion_strategy import MeanReversionStrategy
from quant_engine.strategies.volume_breakout_strategy import VolumeBreakoutStrategy
from quant_engine.strategies.rsi_strategy import RSIStrategy


def run_all(max_days: int = None, initial_capital: float = 1_000_000):
    print("=== 量化策略實驗室 ===\n")

    # 1. Load data
    print("載入數據...")
    t0 = time.time()
    quotes_df, close_pivot, names_map = load_quotes(max_days)
    print(f"  ✓ {len(close_pivot)} 個交易日, {len(close_pivot.columns)} 隻股票 ({time.time()-t0:.1f}s)\n")

    # 2. Define strategies
    strategies = [
        MomentumStrategy(lookback=20, top_n=5, holding=10),
        MomentumStrategy(lookback=60, top_n=10, holding=20),
        MeanReversionStrategy(lookback=20, std_mult=2.0, top_n=5, holding=10),
        MeanReversionStrategy(lookback=10, std_mult=1.5, top_n=3, holding=5),
        VolumeBreakoutStrategy(lookback=20, vol_mult=3.0, top_n=5, holding=10),
        VolumeBreakoutStrategy(lookback=10, vol_mult=5.0, top_n=3, holding=5),
        RSIStrategy(rsi_period=14, oversold=30, top_n=5, holding=10),
        RSIStrategy(rsi_period=7, oversold=25, top_n=3, holding=5),
    ]

    # 3. Run backtests
    engine = BacktestEngine(initial_capital=initial_capital)
    results = []

    for strat in strategies:
        print(f"回測: {strat.name} ({strat.get_params()})...")
        t0 = time.time()
        result = engine.run(strat, quotes_df, close_pivot, names_map)
        elapsed = time.time() - t0
        m = result.get("metrics", {})
        print(f"  ✓ 回報 {m.get('total_return_pct', 0):.1f}% | "
              f"Sharpe {m.get('sharpe_ratio', 0):.2f} | "
              f"MaxDD {m.get('max_drawdown_pct', 0):.1f}% | "
              f"WinRate {m.get('win_rate_pct', 0):.0f}% | "
              f"{result['total_trades']} 筆交易 ({elapsed:.1f}s)")
        results.append(result)

    # 4. Save results
    out_path = Path(__file__).parent / "backtest_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✓ 結果已儲存到 {out_path}")

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="量化策略實驗室")
    parser.add_argument("--days", type=int, default=None, help="只用最近 N 個交易日")
    parser.add_argument("--capital", type=float, default=1_000_000, help="初始資金")
    args = parser.parse_args()
    run_all(max_days=args.days, initial_capital=args.capital)
