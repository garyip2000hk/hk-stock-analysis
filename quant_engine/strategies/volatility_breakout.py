"""
波動率突破策略 (Volatility Breakout)
結合 ATR 突破 + 成交量爆升，捕捉大行情起點
參考 Keltner Channel / Donchian Channel 系統
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class VolatilityBreakoutStrategy(BaseStrategy):
    name = "Volatility Breakout"
    description = "波動率突破：價格突破 ATR 通道 + 成交量放大 = 進場。ATR trailing stop 離場。"

    def __init__(self, atr_period: int = 14, atr_mult: float = 2.0,
                 vol_ratio: float = 1.5, top_n: int = 10):
        self.atr_period = atr_period
        self.atr_mult = atr_mult
        self.vol_ratio = vol_ratio
        self.top_n = top_n

    def get_params(self) -> dict:
        return {
            "atr_period": self.atr_period, "atr_mult": self.atr_mult,
            "vol_ratio": self.vol_ratio, "top_n": self.top_n,
        }

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        high_pivot = quotes_df.pivot(index="date", columns="code", values="high")
        low_pivot = quotes_df.pivot(index="date", columns="code", values="low")
        vol_pivot = quotes_df.pivot(index="date", columns="code", values="vol")

        signals_list = []

        for code in universe:
            if code not in close_pivot.columns:
                continue
            if code not in high_pivot.columns or code not in low_pivot.columns:
                continue

            close = close_pivot[code].dropna()
            high = high_pivot[code].dropna()
            low = low_pivot[code].dropna()
            vol = vol_pivot[code].dropna()

            if len(close) < self.atr_period * 3:
                continue

            # ATR
            atr = self.atr(high, low, close, self.atr_period)

            # EMA 作為中線
            ema = self.ema(close, self.atr_period)

            # Upper/Lower channel
            upper = ema + self.atr_mult * atr
            lower = ema - self.atr_mult * atr

            # Volume MA
            vol_ma = vol.rolling(self.atr_period).mean()

            # Breakout signals
            breakout_up = close > upper.shift(1)
            breakout_down = close < lower.shift(1)
            vol_confirm = vol > vol_ma * self.vol_ratio

            long_signal = breakout_up & vol_confirm
            short_signal = breakout_down & vol_confirm

            for date in close.index:
                if date in long_signal.index and long_signal.loc[date]:
                    signals_list.append({"date": date, "code": code, "signal": 1})
                elif date in short_signal.index and short_signal.loc[date]:
                    signals_list.append({"date": date, "code": code, "signal": -1})

        return pd.DataFrame(signals_list) if signals_list else pd.DataFrame(columns=["date", "code", "signal"])
