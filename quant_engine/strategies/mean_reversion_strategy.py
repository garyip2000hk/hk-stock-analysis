"""
mean_reversion_strategy.py — 均值回歸策略
買入跌穿 Bollinger Band 下軌嘅股票，等佢回歸均線
"""
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    name = "均值回歸 (Mean Reversion)"
    description = "當股價跌穿 Bollinger Band 下軌 (均線 - K*標準差) 時買入，持有 N 天。"

    def __init__(self, lookback=20, std_mult=2.0, top_n=5, holding=10):
        self.lookback = lookback
        self.std_mult = std_mult
        self.top_n = top_n
        self.holding = holding

    def generate_signals(self, quotes_df, close_pivot, universe):
        sma = close_pivot.rolling(self.lookback).mean()
        std = close_pivot.rolling(self.lookback).std()
        lower = sma - self.std_mult * std

        # 跌穿下軌 => signal
        below = close_pivot < lower

        all_signals = []
        for dt in below.index:
            codes = below.columns[below.loc[dt].fillna(False)]
            if len(codes) == 0:
                continue
            # 按偏離程度排序
            deviation = (close_pivot.loc[dt, codes] - lower.loc[dt, codes]).abs()
            top = deviation.nlargest(self.top_n)
            for code in top.index:
                all_signals.append({
                    "date": dt,
                    "code": code,
                    "score": -top[code],  # 負數 = 跌得越深越好
                })

        return pd.DataFrame(all_signals) if all_signals else pd.DataFrame(columns=["date", "code", "score"])
