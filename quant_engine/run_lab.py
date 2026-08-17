#!/usr/bin/env python3
"""
run_lab.py — 策略回測實驗室
載入全部策略，逐一回測，輸出 JSON 結果供 Dashboard 讀取
"""
import json, os, sys, time
import pandas as pd
import numpy as np
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from quant_engine.backtester import BacktestEngine
from quant_engine.strategies.momentum import MomentumStrategy
from quant_engine.strategies.mean_reversion import MeanReversionStrategy
from quant_engine.strategies.volume_breakout import VolumeBreakout
from quant_engine.strategies.rsi_strategy import RSIStrategy
from quant_engine.strategies.mean_revert_bb import MeanRevertBB
from quant_engine.strategies.rsi_macd import RSIMACDStrategy
from quant_engine.strategies.breakout import BreakoutStrategy
from quant_engine.strategies.vwap_reversion import VWAPReversion
from quant_engine.strategies.short_squeeze import ShortSqueeze
from quant_engine.strategies.gap_reversion import GapReversion
from quant_engine.strategies.smart_money import SmartMoney
from quant_engine.strategies.trend_follow_atr import TrendFollowATR

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_lab.json")


def load_quotes_df():
    qpath = os.path.join(ROOT, "imported", "quotes.json")
    with open(qpath, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict) and "quotes" in raw:
        quotes_dict = raw["quotes"]
    elif isinstance(raw, dict):
        quotes_dict = raw
    else:
        raise ValueError("Unexpected quotes.json format")

    names = raw.get("names", {}) if isinstance(raw, dict) else {}
    rows = []
    for dt_str, stocks in quotes_dict.items():
        for code, vals in stocks.items():
            if isinstance(vals, dict) and vals.get("close") is not None:
                try:
                    rows.append({
                        "date": pd.Timestamp(dt_str),
                        "code": code,
                        "close": float(vals.get("close") or 0),
                        "high": float(vals.get("high") or 0),
                        "low": float(vals.get("low") or 0),
                        "vol": float(vals.get("vol") or 0),
                        "turnover": float(vals.get("turnover") or 0),
                    })
                except (ValueError, TypeError):
                    continue
    df = pd.DataFrame(rows)
    return df, names


def load_ccass_kwargs():
    """Load CCASS data as kwargs for strategies that need it"""
    try:
        from quant_engine.ccass_loader import build_c10_changes
        c10_d5, c10_d20 = build_c10_changes()
        from quant_engine.ccass_loader import build_intermed_changes
        cnt_d5, cnt_d20 = build_intermed_changes()
        return {
            "c10_delta_5d": c10_d5,
            "c10_delta_20d": c10_d20,
            "intermed_cnt_delta_5d": cnt_d5,
            "intermed_cnt_delta_20d": cnt_d20,
        }
    except Exception as e:
        print(f"  ⚠️  CCASS 載入失敗: {e}")
        return {}


def run():
    print("=" * 60)
    print("📊 策略回測實驗室 (Quant Strategy Lab)")
    print("=" * 60)

    # 1. Load quotes
    print("\n[1/4] 載入報價數據...")
    t0 = time.time()
    quotes_df, names_map = load_quotes_df()
    dates = sorted(quotes_df["date"].unique())
    codes = quotes_df["code"].nunique()
    print(f"  ✓ 載入 {len(quotes_df):,} 條報價記錄")
    print(f"  ✓ 交易日: {dates[0].strftime('%Y-%m-%d')} → {dates[-1].strftime('%Y-%m-%d')} ({len(dates)} 日)")
    print(f"  ✓ 股票數: {codes} 隻")

    # Build close pivot
    close_pivot = quotes_df.pivot_table(index="date", columns="code", values="close", aggfunc="last")

    # 2. Load CCASS kwargs
    print("\n[2/4] 載入 CCASS 數據...")
    ccass_kwargs = load_ccass_kwargs()
    if ccass_kwargs:
        print(f"  ✓ CCASS 數據已載入")
    else:
        print(f"  ⚠️  CCASS 數據不可用，CCASS 策略將跳過")

    # 3. Define strategies
    print("\n[3/4] 回測策略...")
    engine = BacktestEngine(initial_capital=1_000_000, position_pct=0.1)

    all_strategies = [
        MomentumStrategy(),
        MeanReversionStrategy(),
        VolumeBreakout(),
        RSIStrategy(),
        MeanRevertBB(),
        RSIMACDStrategy(),
        BreakoutStrategy(),
        VWAPReversion(),
        ShortSqueeze(),
        GapReversion(),
        SmartMoney(),
        TrendFollowATR(),
    ]

    # CCASS strategies need kwargs
    ccass_names = {"CCASS 洗籌", "CCASS 動量歸邊"}
    results = []
    total = len(all_strategies)

    for i, strat in enumerate(all_strategies, 1):
        print(f"  [{i}/{total}] {strat.name}...", end=" ", flush=True)
        try:
            kwargs = ccass_kwargs if strat.name in ccass_names else {}
            result = engine.run(strat, quotes_df, close_pivot, names_map, **kwargs)
            m = result.get("metrics", {})
            ret = m.get("total_return_pct", 0)
            sharpe = m.get("sharpe_ratio", 0)
            win = m.get("win_rate_pct", 0)
            print(f"✅ 回報={ret}%, Sharpe={sharpe}, 勝率={win}%")
            results.append(result)
        except Exception as e:
            print(f"❌ {e}")
            results.append({
                "strategy": strat.name,
                "params": strat.get_params(),
                "description": strat.description,
                "metrics": {},
                "error": str(e),
            })

    # 4. Save output
    print(f"\n[4/4] 輸出結果...")
    output = {
        "generated_at": datetime.now().isoformat(),
        "quotes_range": f"{dates[0].strftime('%Y-%m-%d')} → {dates[-1].strftime('%Y-%m-%d')}",
        "trading_days": len(dates),
        "total_stocks": codes,
        "strategies": results,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, default=str)
    print(f"  ✓ 結果已保存到 {OUTPUT_PATH}")

    elapsed = time.time() - t0
    print(f"\n⏱️  耗時 {elapsed:.1f} 秒")
    print("=" * 60)


if __name__ == "__main__":
    run()
