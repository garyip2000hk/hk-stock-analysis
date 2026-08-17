"""
ccass_momentum.py — CCASS 歸邊 + 動能策略
結合 CCASS 持倉集中度變化 + MACD 金叉，捕捉莊家建倉跡象
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class CCASSMomentum(BaseStrategy):
    name = "CCASS 歸邊動能"
    description = "CCASS 持倉集中度上升 + MACD 金叉，捕捉莊家建倉跡象"

    def __init__(self, top_n: int = 10, macd_fast: int = 12, macd_slow: int = 26,
                 macd_signal: int = 9, holding: int = 10):
        self.top_n = top_n
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.holding = holding

    def get_params(self) -> dict:
        return {"top_n": self.top_n, "macd_fast": self.macd_fast,
                "macd_slow": self.macd_slow, "macd_signal": self.macd_signal}

    def generate_signals(self, quotes_df: pd.DataFrame, close_pivot: pd.DataFrame,
                         universe: list, **kwargs) -> pd.DataFrame:
        # 由 run_lab.py 傳入嘅 CCASS pivot (date × code)
        ccass_pivot = kwargs.get("ccass_c10_pivot")
        signals = []

        for code in universe:
            if code not in close_pivot.columns:
                continue
            px = close_pivot[code].dropna()
            if len(px) < self.macd_slow + self.macd_signal + 5:
                continue

            macd_line, signal_line, hist = self.macd(
                px, self.macd_fast, self.macd_slow, self.macd_signal)

            # MACD 金叉
            macd_cross = (macd_line > signal_line) & (macd_line.shift(1) <= signal_line.shift(1))

            if ccass_pivot is not None and code in ccass_pivot.columns:
                # 用 CCASS c10 變化做加分
                c10 = ccass_pivot[code].dropna()
                if len(c10) >= 2:
                    c10_change = c10.iloc[-1] - c10.iloc[0]
                    # CCASS 上升 + MACD 金叉 = 強信號
                    if c10_change > 0:
                        for dt in px.index[macd_cross]:
                            signals.append({"date": dt, "code": code, "signal": 1})
                    else:
                        # 純 MACD 金叉，但只喺 hist 為正時
                        strong_cross = macd_cross & (hist > 0)
                        for dt in px.index[strong_cross]:
                            signals.append({"date": dt, "code": code, "signal": 1})
            else:
                # 冇 CCASS 數據，純 MACD 金叉 + hist 正
                strong_cross = macd_cross & (hist > 0)
                for dt in px.index[strong_cross]:
                    signals.append({"date": dt, "code": code, "signal": 1})

        if not signals:
            return pd.DataFrame(columns=["date", "code", "signal"])
        return pd.DataFrame(signals).sort_values("date").reset_index(drop=True)
