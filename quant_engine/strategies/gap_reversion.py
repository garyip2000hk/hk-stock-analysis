"""
gap_reversion.py — 跳空缺口回歸策略
開市跳空後回補缺口嘅概率統計
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class GapReversion(BaseStrategy):
    name = "缺口回歸"
    description = "開市跳空缺口（>2%）後，等待回補缺口時反向交易"

    def __init__(self, gap_pct=2.0, holding=5):
        self.gap_pct = gap_pct / 100
        self.holding = holding

    def get_params(self):
        return {"gap_pct": self.gap_pct * 100, "holding": self.holding}

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        cols = [c for c in universe if c in close_pivot.columns]
        if not cols:
            return pd.DataFrame(columns=["date", "code", "signal"])

        low_pivot = quotes_df.pivot_table(index="date", columns="code", values="low", aggfunc="last")
        high_pivot = quotes_df.pivot_table(index="date", columns="code", values="high", aggfunc="last")

        signals = []
        for code in cols:
            px = close_pivot[code].dropna()
            if len(px) < 10:
                continue

            prev_close = px.shift(1)
            today_low = low_pivot[code].reindex(px.index) if code in low_pivot.columns else px
            today_high = high_pivot[code].reindex(px.index) if code in high_pivot.columns else px

            # 跳空低開（gap down）> gap_pct
            gap_down = (today_low / prev_close - 1) < -self.gap_pct
            # 跳空高開（gap up）> gap_pct
            gap_up = (today_high / prev_close - 1) > self.gap_pct

            # 跌空後做多（回補缺口概率高）
            for dt in px.index[gap_down.fillna(False)]:
                signals.append({"date": dt, "code": code, "signal": 1})

        return pd.DataFrame(signals) if signals else pd.DataFrame(columns=["date", "code", "signal"])
