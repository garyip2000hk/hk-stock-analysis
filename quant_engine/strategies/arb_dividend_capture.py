import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class DividendCaptureArb(BaseStrategy):
    name = "Dividend Capture Proxy"
    description = "收息套利代理：尋找波動率低且處於穩定上升通道的股票，模擬在除淨前買入並透過期權對沖下跌風險"
    
    def generate_signals(self, quotes_df, close_pivot, universe):
        df = quotes_df.copy()
        df["ret"] = df.groupby("code")["close"].pct_change()
        df["std20"] = df.groupby("code")["ret"].transform(lambda x: x.rolling(20).std())
        df["ma60"] = df.groupby("code")["close"].transform(lambda x: x.rolling(60).mean())
        
        # 價格 > 60日均線 且 波動率低於大盤平均
        median_std = df.groupby("date")["std20"].transform("median")
        mask = (df["close"] > df["ma60"]) & (df["std20"] < median_std * 0.8) & (df["std20"] > 0)
        
        df["score"] = np.where(mask, 1.0, 0)
        return df[["date", "code", "score"]].dropna()
