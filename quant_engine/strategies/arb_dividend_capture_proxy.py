from .base_strategy import BaseStrategy
import pandas as pd

class DividendCaptureArbProxy(BaseStrategy):
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(0, index=df.index)
        if "close" not in df.columns:
            return signals
            
        rsi = self.calculate_rsi(df, 14)
        condition = (rsi < 30) & (df["close"] < df["close"].rolling(200).mean() * 0.95)
        signals[condition] = 1
        return signals
        
    def calculate_rsi(self, df, period=14):
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))
