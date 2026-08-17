import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class CalendarSpreadArb(BaseStrategy):
    name = "Calendar Spread Proxy"
    description = "跨期套利代理：尋找短期極度超買/超賣但中長期趨勢不變的股票"
    
    def generate_signals(self, quotes_df, close_pivot, universe):
        df = quotes_df.copy()
        
        df["ma50"] = df.groupby("code")["close"].transform(lambda x: x.rolling(50).mean())
        df["ret3"] = df.groupby("code")["close"].pct_change(3)
        
        # 長期趨勢向上 (close > ma50)，但短期急挫 (3日跌 > 5%)
        mask_long = (df["close"] > df["ma50"]) & (df["ret3"] < -0.05)
        
        # 長期趨勢向下 (close < ma50)，但短期急升 (3日升 > 5%)
        mask_short = (df["close"] < df["ma50"]) & (df["ret3"] > 0.05)
        
        df["score"] = np.where(mask_long, 1.0, np.where(mask_short, -1.0, 0))
        return df[["date", "code", "score"]].dropna()
