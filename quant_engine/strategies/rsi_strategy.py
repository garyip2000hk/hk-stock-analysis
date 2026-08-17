"""
rsi_strategy.py — RSI 超賣反彈策略
RSI 跌入超賣區 (<30) 時買入，等反彈
"""
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


class RSIStrategy(BaseStrategy):
    name = "RSI 超賣反彈 (RSI Oversold)"
    description = "當 RSI 跌入超賣區 (< 30) 時買入，持有 N 天後賣出。"

    def __init__(self, rsi_period=14, oversold=30, top_n=5, holding=10):
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.top_n = top_n
        self.holding = holding

    def generate_signals(self, quotes_df, close_pivot, universe):
        # 計算 RSI
        delta = close_pivot.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)

        avg_gain = gain.rolling(self.rsi_period, min_periods=self.rsi_period).mean()
        avg_loss = loss.rolling(self.rsi_period, min_periods=self.rsi_period).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))

        # 超賣信號
        oversold_mask = rsi < self.oversold

        all_signals = []
        for dt in rsi.index:
            codes = oversold_mask.columns[oversold_mask.loc[dt].fillna(False)]
            if len(codes) == 0:
                continue

            # RSI 越低越好
            rsi_vals = rsi.loc[dt, codes]
            top = rsi_vals.nsmallest(self.top_n)
            for code in top.index:
                all_signals.append({
                    "date": dt,
                    "code": code,
                    "score": top[code],
                })

        return pd.DataFrame(all_signals) if all_signals else pd.DataFrame(columns=["date", "code", "score"])
