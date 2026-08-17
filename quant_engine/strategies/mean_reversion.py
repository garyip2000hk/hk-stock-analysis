"""
mean_reversion.py — 均值回歸策略
RSI 超賣 + 股價跌破布林下軌時買入
"""
import pandas as pd
from .base import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    name = "均值回歸"
    description = "RSI 超賣 + 股價跌破布林下軌時買入，等待回歸均值"

    def __init__(self, rsi_period=14, rsi_threshold=30, bb_window=20, bb_std=2.0, holding=10):
        self.rsi_period = rsi_period
        self.rsi_threshold = rsi_threshold
        self.bb_window = bb_window
        self.bb_std = bb_std
        self.holding = holding

    def get_params(self):
        return {"rsi_period": self.rsi_period, "rsi_threshold": self.rsi_threshold,
                "bb_window": self.bb_window, "bb_std": self.bb_std, "holding": self.holding}

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        signals = []
        dates = sorted(close_pivot.index)
        warmup = max(self.rsi_period, self.bb_window) + 5

        for i in range(warmup, len(dates), self.holding):
            dt = dates[i]
            for code in universe:
                if code not in close_pivot.columns:
                    continue
                series = close_pivot[code].loc[:dt]
                if len(series) < warmup:
                    continue

                rsi_val = self.rsi(series, self.rsi_period)
                mid, upper, lower = self.bollinger_bands(series, self.bb_window, self.bb_std)

                current_rsi = rsi_val.iloc[-1]
                current_close = series.iloc[-1]
                current_lower = lower.iloc[-1]

                if pd.notna(current_rsi) and pd.notna(current_lower):
                    if current_rsi < self.rsi_threshold and current_close < current_lower:
                        signals.append({"date": dt, "code": code, "signal": 1})

        return pd.DataFrame(signals) if signals else pd.DataFrame(columns=["date", "code", "signal"])
