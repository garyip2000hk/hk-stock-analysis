"""
vwap_mean_reversion.py — VWAP 均值回歸策略
價格偏離 VWAP 超過 N 個標準差 → 回歸交易
"""
import pandas as pd
import numpy as np
from .base import BaseStrategy


class VWAPMeanReversion(BaseStrategy):
    name = "VWAP 均值回歸"
    description = "成交量加權均價偏離 → 回歸。偏離 z > 2 做空，z < -2 做多。"

    def __init__(self, vwap_period: int = 20, entry_z: float = 2.0,
                 exit_z: float = 0.5, holding: int = 5):
        self.vwap_period = vwap_period
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.holding = holding

    def get_params(self) -> dict:
        return {
            "vwap_period": self.vwap_period,
            "entry_z": self.entry_z,
            "exit_z": self.exit_z,
            "holding": self.holding,
        }

    def generate_signals(self, quotes_df: pd.DataFrame, close_pivot: pd.DataFrame,
                         universe: list, **kwargs) -> pd.DataFrame:
        # 計算每個股票嘅 volume-weighted average price (VWAP)
        signals = []
        for code in universe:
            code_df = quotes_df[quotes_df["code"] == code].sort_values("date")
            if len(code_df) < self.vwap_period * 2:
                continue

            px = code_df.set_index("date")["close"]
            vol = code_df.set_index("date")["vol"]
            turnover = code_df.set_index("date")["turnover"]

            # 用 turnover/vol 算 VWAP（如果有 turnover）
            if turnover.notna().sum() > len(turnover) * 0.5:
                cum_turnover = turnover.rolling(self.vwap_period).sum()
                cum_vol = vol.rolling(self.vwap_period).sum()
                vwap = cum_turnover / cum_vol.replace(0, np.nan)
            else:
                vwap = px.rolling(self.vwap_period).mean()

            # 偏離度
            deviation = px - vwap
            dev_std = deviation.rolling(self.vwap_period).std()
            z_score = deviation / dev_std.replace(0, np.nan)

            # 信號：z < -entry_z（超跌，做多）
            oversold = z_score < -self.entry_z
            for dt in px.index[oversold]:
                signals.append({"date": dt, "code": code, "signal": 1})

        if not signals:
            return pd.DataFrame(columns=["date", "code", "signal"])

        df = pd.DataFrame(signals).sort_values("date").reset_index(drop=True)
        df = df.drop_duplicates(subset=["date", "code"], keep="first")
        return df
