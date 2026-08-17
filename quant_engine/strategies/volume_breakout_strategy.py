"""
volume_breakout_strategy.py — 成交量突破策略
成交量突然放大 + 股價上漲 = 可能係突破信號
"""
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


class VolumeBreakoutStrategy(BaseStrategy):
    name = "成交量突破 (Volume Breakout)"
    description = "當成交量突然放大 N 倍且股價上漲時買入，持有 M 天。"

    def __init__(self, lookback=20, vol_mult=3.0, top_n=5, holding=10):
        self.lookback = lookback
        self.vol_mult = vol_mult
        self.top_n = top_n
        self.holding = holding

    def generate_signals(self, quotes_df, close_pivot, universe):
        # 建 volume pivot
        vol_pivot = quotes_df.pivot(index="date", columns="code", values="vol")
        vol_pivot = vol_pivot.sort_index()

        vol_ma = vol_pivot.rolling(self.lookback).mean()
        close_chg = close_pivot.pct_change(1)

        # 成交量放大倍數
        vol_ratio = vol_pivot / vol_ma

        all_signals = []
        for dt in vol_ratio.index:
            if dt not in close_chg.index:
                continue
            # 成交量放大倍以上 且 股價上漲
            mask = (vol_ratio.loc[dt] >= self.vol_mult) & (close_chg.loc[dt] > 0)
            codes = mask.index[mask.fillna(False)]
            if len(codes) == 0:
                continue

            scores = vol_ratio.loc[dt, codes]
            top = scores.nlargest(self.top_n)
            for code in top.index:
                all_signals.append({
                    "date": dt,
                    "code": code,
                    "score": top[code],
                })

        return pd.DataFrame(all_signals) if all_signals else pd.DataFrame(columns=["date", "code", "score"])
