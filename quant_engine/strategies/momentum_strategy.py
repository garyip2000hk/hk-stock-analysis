"""
momentum_strategy.py — 動能策略
買入近期漲幅最大的股票，持有 N 天後賣出
"""
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


class MomentumStrategy(BaseStrategy):
    name = "動能策略 (Momentum)"
    description = "買入過去 N 日漲幅最大嘅 Top K 股票，持有 M 天後賣出。追強棄弱。"

    def __init__(self, lookback=20, top_n=5, holding=10):
        self.lookback = lookback
        self.top_n = top_n
        self.holding = holding

    def generate_signals(self, quotes_df, close_pivot, universe):
        returns = close_pivot.pct_change(self.lookback)

        all_signals = []
        for dt in returns.index:
            row = returns.loc[dt].dropna()
            if row.empty:
                continue
            top = row.nlargest(self.top_n)
            for code in top.index:
                all_signals.append({
                    "date": dt,
                    "code": code,
                    "score": top[code],
                })

        return pd.DataFrame(all_signals) if all_signals else pd.DataFrame(columns=["date", "code", "score"])
