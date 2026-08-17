"""
trend_follow_atr.py — ATR 趨勢跟隨策略
用 ATR 設止損，趨勢方向用 EMA 20/50 判斷
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class TrendFollowATR(BaseStrategy):
    name = "ATR 趨勢跟隨"
    description = "EMA20/50 金叉 + ATR 止損嘅趨勢跟隨策略"

    def __init__(self, fast=20, slow=50, atr_mult=2.0, holding=15):
        self.fast = fast
        self.slow = slow
        self.atr_mult = atr_mult
        self.holding = holding

    def get_params(self):
        return {"fast": self.fast, "slow": self.slow, "atr_mult": self.atr_mult, "holding": self.holding}

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        cols = [c for c in universe if c in close_pivot.columns]
        signals = []
        for code in cols:
            px = close_pivot[code].dropna()
            if len(px) < self.slow + 5:
                continue
            ema_fast = px.ewm(span=self.fast, adjust=False).mean()
            ema_slow = px.ewm(span=self.slow, adjust=False).mean()
            cross = (ema_fast > ema_slow) & (ema_fast.shift(1) <= ema_slow.shift(1))
            for dt in px.index[cross.fillna(False)]:
                signals.append({"date": dt, "code": code, "signal": 1})
        return pd.DataFrame(signals) if signals else pd.DataFrame(columns=["date", "code", "signal"])
