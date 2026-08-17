import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class StatPairsArb(BaseStrategy):
    name = "Stat Pairs Arb Proxy"
    description = "統計套利代理：尋找短期大幅落後於大市/板塊的股票進行均值回歸"
    
    def generate_signals(self, quotes_df, close_pivot, universe):
        df = quotes_df.copy()
        df["ret"] = df.groupby("code")["close"].pct_change()
        
        # 簡單大市平均回報
        market_ret = df.groupby("date")["ret"].transform("mean")
        
        # 過去 5 日累積落後大市 > 5%
        df["rel_ret"] = df["ret"] - market_ret
        df["cum_rel"] = df.groupby("code")["rel_ret"].transform(lambda x: x.rolling(5).sum())
        
        mask = df["cum_rel"] < -0.05
        df["score"] = np.where(mask, -df["cum_rel"], 0)  # 落後越多分數越高
        
        return df[["date", "code", "score"]].dropna()
