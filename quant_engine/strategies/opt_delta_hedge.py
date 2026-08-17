import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class DeltaHedgeRebalance(BaseStrategy):
    name = "Delta Hedge Rebalance Proxy"
    description = "Delta 對沖重平衡代理：偵測價格順勢且成交量極大的 K 線，模擬莊家 Delta Hedging 帶來的追漲殺跌效應"
    
    def generate_signals(self, quotes_df, close_pivot, universe):
        df = quotes_df.copy()
        df["ret"] = df.groupby("code")["close"].pct_change()
        df["vol_ma20"] = df.groupby("code")["vol"].transform(lambda x: x.rolling(20).mean())
        df["vol_ratio"] = df["vol"] / df["vol_ma20"]
        
        # 價格大升且成交量爆發
        mask_long = (df["ret"] > 0.05) & (df["vol_ratio"] > 3.0)
        # 價格大跌且成交量爆發
        mask_short = (df["ret"] < -0.05) & (df["vol_ratio"] > 3.0)
        
        df["score"] = np.where(mask_long, df["vol_ratio"], np.where(mask_short, -df["vol_ratio"], 0))
        return df[["date", "code", "score"]].dropna()
