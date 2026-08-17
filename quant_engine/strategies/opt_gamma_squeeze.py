import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class GammaSqueeze(BaseStrategy):
    name = "Gamma Squeeze"
    description = "Gamma Squeeze 代理：尋找價格急升且成交量放大的股票，模擬做市商被迫追入 Delta 的情況"
    
    def generate_signals(self, quotes_df, close_pivot, universe):
        df = quotes_df.copy()
        
        # 價格升幅 > 3%
        df["ret"] = df.groupby("code")["close"].pct_change()
        
        # 成交量 > 20日均量 2 倍
        df["vol_ma20"] = df.groupby("code")["vol"].transform(lambda x: x.rolling(20).mean())
        df["vol_ratio"] = df["vol"] / df["vol_ma20"]
        
        # 產生信號
        mask = (df["ret"] > 0.03) & (df["vol_ratio"] > 2.0)
        df["score"] = np.where(mask, df["vol_ratio"], 0)
        
        return df[["date", "code", "score"]].dropna()
