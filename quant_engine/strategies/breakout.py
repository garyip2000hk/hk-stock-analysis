"""
breakout.py — 突破策略 (向量化版)
股價突破 N 日新高 + 成交量放大 → 買入
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class BreakoutStrategy(BaseStrategy):
    name = "新高突破"
    description = "股價突破過去 N 日最高價時買入，配合成交量確認"

    def __init__(self, lookback=20, vol_multiplier=1.5, holding=10):
        self.lookback = lookback
        self.vol_multiplier = vol_multiplier
        self.holding = holding

    def get_params(self):
        return {"lookback": self.lookback, "vol_multiplier": self.vol_multiplier, "holding": self.holding}

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        # 向量化：pivot volume
        vol_pivot = quotes_df.pivot_table(index="date", columns="code", values="vol", aggfunc="last")
        vol_pivot = vol_pivot.reindex(close_pivot.index)

        # N 日最高（用 shift(1) 避免 look-ahead）
        rolling_high = close_pivot.shift(1).rolling(self.lookback).max()

        # N 日平均成交量
        rolling_vol = vol_pivot.rolling(self.lookback).mean()

        # 突破條件：今日收盤 > N 日高點
        breakout = close_pivot > rolling_high

        # 成交量條件：今日成交量 > N 日均量 * 倍數
        vol_confirm = vol_pivot > (rolling_vol * self.vol_multiplier)

        # 兩者同時滿足（fillna 避免 NaN mask 錯誤）
        buy_signal = (breakout.fillna(False) & vol_confirm.fillna(False))

        signals = []
        for dt in buy_signal.index:
            row = buy_signal.loc[dt]
            codes = row[row == True].index.tolist()
            for code in codes:
                signals.append({"date": dt, "code": code, "signal": 1})

        return pd.DataFrame(signals) if signals else pd.DataFrame(columns=["date", "code", "signal"])
