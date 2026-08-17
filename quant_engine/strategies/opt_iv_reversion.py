import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class IVReversion(BaseStrategy):
    name = "IV Reversion Proxy"
    description = "引伸波幅回歸代理：以 ATR 偏離歷史均值作為波幅過高指標，反向操作做空波幅（以均值回歸代表）"
    
    def generate_signals(self, quotes_df, close_pivot, universe):
        df = quotes_df.copy()
        
        # 計算 ATR (簡單版 = High - Low)
        df["atr"] = df["high"] - df["low"]
        df["atr_ma20"] = df.groupby("code")["atr"].transform(lambda x: x.rolling(20).mean())
        
        # 當 ATR 極高且當日下跌，做多（預期波幅收縮且價格反彈）
        df["ret"] = df.groupby("code")["close"].pct_change()
        mask = (df["atr"] > df["atr_ma20"] * 1.5) & (df["ret"] < -0.02)
        
        df["score"] = np.where(mask, 1.0, 0)
        return df[["date", "code", "score"]].dropna()
