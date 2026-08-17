"""
obv_divergence.py — OBV 背離策略
價格創新低但 OBV 未創新低 = 看多背離
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class OBVDivergence(BaseStrategy):
    name = "OBV 背離"
    description = "價格新低但 OBV 未新低 = 看多背離信號"

    def __init__(self, lookback=20, holding=10):
        self.lookback = lookback
        self.holding = holding

    def get_params(self):
        return {"lookback": self.lookback, "holding": self.holding}

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        vol_pivot = quotes_df.pivot_table(
            index="date", columns="code", values="vol", aggfunc="last"
        )
        cols = [c for c in universe if c in close_pivot.columns and c in vol_pivot.columns]
        signals = []
        for code in cols:
            px = close_pivot[code].dropna()
            vol = vol_pivot[code].reindex(px.index).fillna(0)
            if len(px) < self.lookback * 2:
                continue
            direction = np.sign(px.diff())
            obv = (vol * direction).cumsum()

            for i in range(self.lookback, len(px)):
                window_px = px.iloc[i - self.lookback:i]
                window_obv = obv.iloc[i - self.lookback:i]
                curr_px = px.iloc[i]
                curr_obv = obv.iloc[i]
                if curr_px <= window_px.min() * 1.02 and curr_obv > window_obv.min() * 1.05:
                    signals.append({"date": px.index[i], "code": code, "signal": 1})

        return pd.DataFrame(signals) if signals else pd.DataFrame(columns=["date", "code", "signal"])
