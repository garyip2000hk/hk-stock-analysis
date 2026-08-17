"""
rsi_macd.py — RSI + MACD 複合策略 (向量化)
RSI 從超賣回升 + MACD 金叉時買入
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class RSIMACDStrategy(BaseStrategy):
    name = "RSI+MACD"
    description = "RSI 從超賣區回升 + MACD 金叉同時出現時買入"

    def __init__(self, rsi_period=14, rsi_low=35, rsi_recovery=40,
                 macd_fast=12, macd_slow=26, macd_signal=9, holding=10):
        self.rsi_period = rsi_period
        self.rsi_low = rsi_low
        self.rsi_recovery = rsi_recovery
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.holding = holding

    def get_params(self):
        return {"rsi_period": self.rsi_period, "rsi_low": self.rsi_low, "rsi_recovery": self.rsi_recovery,
                "macd_fast": self.macd_fast, "macd_slow": self.macd_slow, "macd_signal": self.macd_signal,
                "holding": self.holding}

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        cols = [c for c in universe if c in close_pivot.columns]
        if not cols:
            return pd.DataFrame(columns=["date", "code", "signal"])

        px = close_pivot[cols]

        # RSI 向量化
        rsi_df = px.apply(lambda s: self.rsi(s, self.rsi_period))

        # MACD 向量化 — 拎 hist (第三個值)
        hist_df = pd.DataFrame(index=px.index, columns=cols, dtype=float)
        for code in cols:
            s = px[code].dropna()
            if len(s) < self.macd_slow + self.macd_signal + 5:
                continue
            _, _, hist = self.macd(s, self.macd_fast, self.macd_slow, self.macd_signal)
            hist_df[code] = hist

        # RSI 信號：昨日 < low 或今日 > recovery 且 < 60
        rsi_yest = rsi_df.shift(1)
        rsi_today = rsi_df
        rsi_ok = (rsi_yest < self.rsi_low) | ((rsi_today > self.rsi_recovery) & (rsi_today < 60))

        # MACD 金叉：hist 從負變正
        hist_yest = hist_df.shift(1)
        hist_today = hist_df
        macd_cross = (hist_yest < 0) & (hist_today > 0)

        # 兩者同時
        combo = (rsi_ok & macd_cross).fillna(False)

        signal_dates = []
        for dt in combo.index:
            active = combo.loc[dt]
            active = active[active == True]
            for code in active.index:
                signal_dates.append({"date": dt, "code": code, "signal": 1})

        return pd.DataFrame(signal_dates) if signal_dates else pd.DataFrame(columns=["date", "code", "signal"])
