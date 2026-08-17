"""
smart_money.py — CCASS 參與者數量收窄 + 成交量配合
持股人數持續收窄 = 洗籌，配合成交突破 = 主力準備拉升
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class SmartMoney(BaseStrategy):
    name = "CCASS 洗籌"
    description = "CCASS 參與者收窄 + 成交量放大 = 主力洗籌後拉升"

    def __init__(self, vol_mult=2.0, holding=10):
        self.vol_mult = vol_mult
        self.holding = holding

    def get_params(self):
        return {"vol_mult": self.vol_mult, "holding": self.holding}

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        cnt_d5 = kwargs.get("intermed_cnt_delta_5d")
        if cnt_d5 is None:
            return pd.DataFrame(columns=["date", "code", "signal"])

        vol_pivot = quotes_df.pivot_table(
            index="date", columns="code", values="vol", aggfunc="last"
        )

        signals = []
        for dt in cnt_d5.index:
            row = cnt_d5.loc[dt].dropna()
            shrinking = row[row < -3]
            for code in shrinking.index:
                if code not in close_pivot.columns or code not in vol_pivot.columns:
                    continue
                px = close_pivot[code].dropna()
                vol = vol_pivot[code].reindex(px.index).fillna(0)
                if len(px) < 20:
                    continue
                avg_vol = vol.rolling(20).mean()
                recent = px.index[px.index <= dt]
                if len(recent) < 1:
                    continue
                latest = recent[-1]
                if latest in vol.index and latest in avg_vol.index:
                    if vol[latest] > avg_vol[latest] * self.vol_mult:
                        signals.append({"date": dt, "code": code, "signal": 1})

        return pd.DataFrame(signals) if signals else pd.DataFrame(columns=["date", "code", "signal"])
