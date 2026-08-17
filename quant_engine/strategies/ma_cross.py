"""
ma_cross.py — 均線交叉策略 (向量化版)
短期均線上穿長期均線(Golden Cross)時買入
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class MACrossStrategy(BaseStrategy):
    name = "均線交叉"
    description = "短期均線上穿長期均線(Golden Cross)時買入"

    def __init__(self, fast=10, slow=50, holding=15):
        self.fast = fast
        self.slow = slow
        self.holding = holding

    def get_params(self):
        return {"fast": self.fast, "slow": self.slow, "holding": self.holding}

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        # 向量化：對整個 pivot 計算 SMA
        fast_ma = close_pivot.rolling(self.fast).mean()
        slow_ma = close_pivot.rolling(self.slow).mean()

        # Golden Cross：今日 fast > slow，昨日 fast <= slow
        cross_up = (fast_ma > slow_ma) & (fast_ma.shift(1) <= slow_ma.shift(1))

        signals = []
        for dt in cross_up.index:
            codes = cross_up.columns[cross_up.loc[dt]].tolist()
            for code in codes:
                if pd.notna(close_pivot.at[dt, code]) and close_pivot.at[dt, code] > 0:
                    signals.append({"date": dt, "code": code, "signal": 1})

        return pd.DataFrame(signals) if signals else pd.DataFrame(columns=["date", "code", "signal"])
