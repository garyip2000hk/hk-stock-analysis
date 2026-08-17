"""
momentum_cross.py — 均線動能交叉策略
EMA(fast) 上穿 EMA(slow) 買入，下穿賣出。
"""
import pandas as pd
from .base import BaseStrategy


class MomentumCross(BaseStrategy):
    name = "動能均線交叉"
    description = "EMA(fast) 上穿 EMA(slow) 做多，下穿平倉。可選 RSI 過濾假信號。"

    def __init__(self, fast: int = 10, slow: int = 30, rsi_filter: bool = True):
        self.fast = fast
        self.slow = slow
        self.rsi_filter = rsi_filter

    def get_params(self):
        return {"fast": self.fast, "slow": self.slow, "rsi_filter": self.rsi_filter}

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        ema_fast = close_pivot[universe].apply(lambda s: self.ema(s, self.fast))
        ema_slow = close_pivot[universe].apply(lambda s: self.ema(s, self.slow))

        raw_signal = (ema_fast > ema_slow).astype(int)

        if self.rsi_filter:
            rsi = close_pivot[universe].apply(lambda s: self.rsi(s, 14))
            # 只喺 RSI 30-70 之間開倉，避免追高殺低
            rsi_ok = (rsi > 30) & (rsi < 70)
            raw_signal = raw_signal.where(rsi_ok, 0)

        records = []
        for date in raw_signal.index:
            for code in raw_signal.columns:
                sig = raw_signal.loc[date, code]
                if pd.notna(sig) and sig != 0:
                    records.append({"date": date, "code": code, "signal": int(sig)})

        return pd.DataFrame(records) if records else pd.DataFrame(columns=["date", "code", "signal"])
