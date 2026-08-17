"""
momentum.py — 動量策略
過去 N 日漲幅最大的股票買入
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    name = "動量選股"
    description = "買入過去 N 日漲幅最大的 Top K 隻股票"

    def __init__(self, lookback: int = 20, top_k: int = 10, holding: int = 10):
        self.lookback = lookback
        self.top_k = top_k
        self.holding = holding

    def get_params(self):
        return {"lookback": self.lookback, "top_k": self.top_k, "holding": self.holding}

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        signals = []
        dates = sorted(close_pivot.index)
        rebalance_dates = dates[self.lookback::self.holding]  # 每 holding 日重新平衡

        for dt in rebalance_dates:
            idx = dates.index(dt)
            if idx < self.lookback:
                continue

            past_dt = dates[idx - self.lookback]
            # 計算 lookback 期間回報
            rets = {}
            for code in universe:
                if code not in close_pivot.columns:
                    continue
                cur = close_pivot.at[dt, code]
                prev = close_pivot.at[past_dt, code]
                if pd.notna(cur) and pd.notna(prev) and prev > 0:
                    rets[code] = (cur - prev) / prev

            if not rets:
                continue

            # 選 Top K
            ranked = sorted(rets.items(), key=lambda x: x[1], reverse=True)
            for code, ret in ranked[:self.top_k]:
                if ret > 0:  # 只買正回報的
                    signals.append({"date": dt, "code": code, "signal": 1})

        return pd.DataFrame(signals) if signals else pd.DataFrame(columns=["date", "code", "signal"])
