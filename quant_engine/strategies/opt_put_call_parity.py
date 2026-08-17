import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class PutCallParityArb(BaseStrategy):
    name = "Put-Call Parity Arb Proxy"
    description = "Put-Call Parity 套利代理：尋找價格瞬間跌破布林下軌且成交量萎縮的極端情況，模擬合成多頭低估時的套利機會"
    
    def generate_signals(self, quotes_df, close_pivot, universe):
        df = quotes_df.copy()
        df["ma20"] = df.groupby("code")["close"].transform(lambda x: x.rolling(20).mean())
        df["std20"] = df.groupby("code")["close"].transform(lambda x: x.rolling(20).std())
        df["bb_lower"] = df["ma20"] - 2.5 * df["std20"]
        
        df["vol_ma20"] = df.groupby("code")["vol"].transform(lambda x: x.rolling(20).mean())
        
        # 跌穿下軌 且 成交量 < 均量
        mask = (df["close"] < df["bb_lower"]) & (df["vol"] < df["vol_ma20"] * 0.5)
        
        df["score"] = np.where(mask, 1.0, 0)
        return df[["date", "code", "score"]].dropna()
