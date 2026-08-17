"""
ccass_concentration.py — CCASS 頭10大集中度歸邊策略
當頭10大持倉比例持續上升（5日 vs 20日），代表大戶暗中收貨
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class CCASSConcentration(BaseStrategy):
    name = "CCASS 歸邊"
    description = "CCASS 頭10大集中度持續上升 = 大戶收貨信號"

    def __init__(self, threshold=2.0, holding=10):
        self.threshold = threshold  # 5d vs 20d delta 百分點閾值
        self.holding = holding

    def get_params(self):
        return {"threshold": self.threshold, "holding": self.holding}

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        c10_d5 = kwargs.get("c10_delta_5d")
        c10_d20 = kwargs.get("c10_delta_20d")
        if c10_d5 is None or c10_d20 is None:
            return pd.DataFrame(columns=["date", "code", "signal"])

        signals = []
        for dt in c10_d5.index:
            if dt not in c10_d20.index:
                continue
            row5 = c10_d5.loc[dt].dropna()
            row20 = c10_d20.loc[dt].dropna()
            common = row5.index.intersection(row20.index)
            for code in common:
                d5 = row5[code]
                d20 = row20[code]
                if d5 > self.threshold and d5 > d20:
                    if code in close_pivot.columns:
                        signals.append({"date": dt, "code": code, "signal": 1})

        return pd.DataFrame(signals) if signals else pd.DataFrame(columns=["date", "code", "signal"])
