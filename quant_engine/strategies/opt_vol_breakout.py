import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class VolBreakout(BaseStrategy):
    name = "Volatility Breakout Proxy"
    description = "波幅突破代理：預期波動率放大的方向性跟隨，當波幅壓縮至極點後出現實體陽燭"
    
    def generate_signals(self, quotes_df, close_pivot, universe):
        df = quotes_df.copy()
        
        # 波幅壓縮 (BB Width 縮小)
        df["std20"] = df.groupby("code")["close"].transform(lambda x: x.rolling(20).std())
        df["ma20"] = df.groupby("code")["close"].transform(lambda x: x.rolling(20).mean())
        df["bb_width"] = df["std20"] / df["ma20"]
        
        # 突破: 收市 > 開市 且 收市 > ma20
        mask = (df["bb_width"] < 0.05) & (df["close"] > df["open"]) & (df["close"] > df["ma20"])
        
        df["score"] = np.where(mask, 1.0, 0)
        return df[["date", "code", "score"]].dropna()
