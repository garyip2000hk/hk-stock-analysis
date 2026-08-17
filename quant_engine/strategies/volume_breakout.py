"""
volume_breakout.py — 成交量突變策略
量比放大 3x + 價格突破 20 日高位
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class VolumeBreakout(BaseStrategy):
    name = "放量突破"
    description = "成交量放大 3 倍 + 突破 20 日高位，捕捉資金湧入嘅爆發點"

    def __init__(self, vol_ratio: float = 3.0, lookback: int = 20):
        self.vol_ratio = vol_ratio
        self.lookback = lookback

    def get_params(self) -> dict:
        return {"vol_ratio": self.vol_ratio, "lookback": self.lookback}

    def generate_signals(self, quotes_df: pd.DataFrame, close_pivot: pd.DataFrame,
                         universe: list[str], **kwargs) -> pd.DataFrame:
        vol_pivot = quotes_df.pivot(index="date", columns="code", values="vol")
        high_pivot = quotes_df.pivot(index="date", columns="code", values="high")
        signals = []

        for code in universe:
            if code not in close_pivot.columns:
                continue
            px = close_pivot[code].dropna()
            if code not in vol_pivot.columns or code not in high_pivot.columns:
                continue
            vols = vol_pivot[code].reindex(px.index).dropna()
            highs = high_pivot[code].reindex(px.index).dropna()
            if len(vols) < self.lookback + 5:
                continue

            avg_vol = vols.rolling(self.lookback).mean()
            vol_ratio_now = vols / avg_vol
            high_n = highs.rolling(self.lookback).max().shift(1)

            buy_signal = (vol_ratio_now > self.vol_ratio) & (px > high_n)

            for dt in px.index[buy_signal]:
                signals.append({"date": dt, "code": code, "signal": 1})

        if not signals:
            return pd.DataFrame(columns=["date", "code", "signal"])
        return pd.DataFrame(signals).sort_values("date").reset_index(drop=True)
