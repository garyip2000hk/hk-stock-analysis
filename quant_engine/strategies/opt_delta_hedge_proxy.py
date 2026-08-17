from .base_strategy import BaseStrategy
import pandas as pd

class DeltaHedgeRebalanceProxy(BaseStrategy):
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(0, index=df.index)
        if "close" not in df.columns:
            return signals
            
        ret = df["close"].pct_change()
        condition = (ret > 0.05) & (df["vol"] > df["vol"].rolling(20).mean() * 2) & (ret.shift(1) > 0.02)
        signals[condition] = 1
        return signals
