"""
vwap_reversion.py — VWAP 均值回歸策略
價格遠離 VWAP 時反向交易
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class VWAPReversion(BaseStrategy):
    name = "VWAP 均值回歸"
    description = "價格偏離 VWAP 超過 2 個標準差時反向交易，等待回歸"

    def __init__(self, lookback=20, entry_z=2.0, exit_z=0.5, holding=5):
        self.lookback = lookback
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.holding = holding

    def get_params(self):
        return {"lookback": self.lookback, "entry_z": self.entry_z,
                "exit_z": self.exit_z, "holding": self.holding}

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        cols = [c for c in universe if c in close_pivot.columns]
        if not cols:
            return pd.DataFrame(columns=["date", "code", "signal"])

        vol_pivot = quotes_df.pivot_table(index="date", columns="code", values="vol", aggfunc="last")
        turnover_pivot = quotes_df.pivot_table(index="date", columns="code", values="turnover", aggfunc="last")

        signals = []
        for code in cols:
            px = close_pivot[code].dropna()
            if len(px) < self.lookback + 5:
                continue

            vol = vol_pivot[code].reindex(px.index).fillna(0) if code in vol_pivot.columns else pd.Series(0, index=px.index)
            to = turnover_pivot[code].reindex(px.index).fillna(0) if code in turnover_pivot.columns else pd.Series(0, index=px.index)

            cum_vol = vol.rolling(self.lookback).sum()
            cum_to = to.rolling(self.lookback).sum()
            vwap = cum_to / cum_vol.replace(0, np.nan)

            deviation = px - vwap
            dev_std = deviation.rolling(self.lookback).std()
            z_score = deviation / dev_std.replace(0, np.nan)

            # 跌穿 -2σ 買入
            buy_z = (z_score.shift(1) < -self.entry_z) & (z_score >= -self.entry_z)
            for dt in px.index[buy_z.fillna(False)]:
                signals.append({"date": dt, "code": code, "signal": 1})

        return pd.DataFrame(signals) if signals else pd.DataFrame(columns=["date", "code", "signal"])
