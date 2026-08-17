import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class IVSkewTrading(BaseStrategy):
    name = "IV Skew Trading Proxy"
    description = "IV Skew 交易代理：尋找連續多日小幅下跌但未破前低的股票，模擬 Put 偏斜度極高時的反向賣出 Put 策略 (看好)"
    
    def generate_signals(self, quotes_df, close_pivot, universe):
        df = quotes_df.copy()
        df["ret"] = df.groupby("code")["close"].pct_change()
        
        # 連續 3 日下跌
        df["down_days"] = df.groupby("code")["ret"].transform(lambda x: (x < 0).rolling(3).sum())
        
        # 60 日低位
        df["low60"] = df.groupby("code")["low"].transform(lambda x: x.rolling(60).min())
        
        # 連跌 3 日但距離 60 日低位仍有 > 5% 空間
        mask = (df["down_days"] == 3) & (df["close"] > df["low60"] * 1.05)
        
        df["score"] = np.where(mask, 1.0, 0)
        return df[["date", "code", "score"]].dropna()
