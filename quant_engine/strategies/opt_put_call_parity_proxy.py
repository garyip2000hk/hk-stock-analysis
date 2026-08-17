from .base_strategy import BaseStrategy
import pandas as pd

class PutCallParityArbProxy(BaseStrategy):
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(0, index=df.index)
        if "close" not in df.columns:
            return signals
            
        bb_std = df["close"].rolling(20).std() * 2
        bb_low = df["close"].rolling(20).mean() - bb_std
        condition = (df["close"] < bb_low) & (df["vol"] > df["vol"].rolling(20).mean() * 2)
        signals[condition] = 1
        return signals
