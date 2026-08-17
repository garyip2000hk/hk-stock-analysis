import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class EarningsRunup(BaseStrategy):
    name = "Earnings Runup Proxy"
    description = "業績前異動代理：尋找近 3 個月內累積回報最高，且近期成交穩步放大的股票，捕捉業績前搶跑期權 Call 的需求"
    
    def generate_signals(self, quotes_df, close_pivot, universe):
        df = quotes_df.copy()
        # 過去 60 日回報
        df["ret60"] = df.groupby("code")["close"].pct_change(60)
        # 成交量趨勢：近 5 日均量 > 近 20 日均量
        df["vol_ma5"] = df.groupby("code")["vol"].transform(lambda x: x.rolling(5).mean())
        df["vol_ma20"] = df.groupby("code")["vol"].transform(lambda x: x.rolling(20).mean())
        
        mask = (df["ret60"] > 0.15) & (df["vol_ma5"] > df["vol_ma20"] * 1.2)
        df["score"] = np.where(mask, df["ret60"], 0)
        return df[["date", "code", "score"]].dropna()
