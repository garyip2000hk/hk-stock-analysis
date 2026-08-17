from .base_strategy import BaseStrategy
import pandas as pd

class EarningsRunupProxyStrategy(BaseStrategy):
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(0, index=df.index)
        if "close" not in df.columns:
            return signals
            
        ret_std = df["close"].pct_change().rolling(20).std()
        condition = (ret_std < ret_std.rolling(50).mean() * 0.5) & (df["vol"] > df["vol"].rolling(20).mean() * 1.5)
        signals[condition] = 1
        return signals
