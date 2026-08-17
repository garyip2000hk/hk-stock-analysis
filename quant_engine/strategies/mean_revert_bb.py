"""
mean_revert_bb.py — 布林通道均值回歸策略
股價跌破下軌買入，升破上軌賣出。
"""
import pandas as pd
from .base import BaseStrategy


class MeanRevertBB(BaseStrategy):
    name = "布林均值回歸"
    description = "股價跌穿布林通道下軌時做多，升穿上軌時平倉。適合震盪市。"

    def __init__(self, window: int = 20, num_std: float = 2.0, hold_days: int = 10):
        self.window = window
        self.num_std = num_std
        self.hold_days = hold_days

    def get_params(self):
        return {"window": self.window, "num_std": self.num_std, "hold_days": self.hold_days}

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        records = []
        for code in universe:
            series = close_pivot[code].dropna()
            if len(series) < self.window + 10:
                continue

            sma, upper, lower = self.bollinger_bands(series, self.window, self.num_std)

            in_trade = False
            entry_date = None

            for date in series.index:
                price = series[date]
                if pd.isna(price):
                    continue

                if not in_trade and pd.notna(lower.get(date)) and price < lower[date]:
                    records.append({"date": date, "code": code, "signal": 1})
                    in_trade = True
                    entry_date = date
                elif in_trade:
                    # 出場條件：升穿上軌 或 持倉超過 hold_days
                    days_held = (date - entry_date).days if entry_date else 0
                    if (pd.notna(upper.get(date)) and price > upper[date]) or days_held > self.hold_days:
                        records.append({"date": date, "code": code, "signal": 0})
                        in_trade = False

        return pd.DataFrame(records) if records else pd.DataFrame(columns=["date", "code", "signal"])
