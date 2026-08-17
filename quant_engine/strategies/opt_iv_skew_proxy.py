from .base_strategy import BaseStrategy
import pandas as pd

class IVSkewTradingProxy(BaseStrategy):
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(0, index=df.index)
        if "close" not in df.columns:
            return signals
            
        hist_vol = df["close"].pct_change().rolling(20).std()
        condition = (hist_vol > hist_vol.rolling(60).mean() * 1.5) & (df["close"] > df["close"].shift(1))
        signals[condition] = 1
        return signals
