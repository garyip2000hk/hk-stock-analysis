import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class EarningsRunupProxyStrategy(BaseStrategy):
    """
    業績期權波動率放大 (Proxy)
    尋找歷史波動率低，但即將有突破的信號 (用 Bollinger Band 收窄作為 Proxy)
    """
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        ma = close.rolling(20).mean()
        std = close.rolling(20).std()
        upper = ma + 2 * std
        lower = ma - 2 * std
        band_width = (upper - lower) / ma
        
        # 波幅極度收窄後，成交量放大 (預示即將突破，適合買入 Straddle/Strangle Proxy)
        vol_ma = df["vol"].rolling(20).mean()
        
        signal = pd.Series(0, index=df.index)
        # 簡單看多波動率 (這裡用突破上軌作為順勢看多信號)
        signal[(band_width < 0.10) & (df["vol"] > vol_ma * 1.5) & (close > upper)] = 1
        signal[(band_width < 0.10) & (df["vol"] > vol_ma * 1.5) & (close < lower)] = -1
        
        return signal

class DividendCaptureArbProxy(BaseStrategy):
    """
    股息套利 (Proxy)
    尋找高息股在特定日期的異常表現 (這裡簡化為長期穩定且近期下跌的吸納機會)
    """
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        ma200 = close.rolling(200).mean()
        rsi = self._rsi(close, 14)
        
        signal = pd.Series(0, index=df.index)
        # 長期向上，短期超賣
        signal[(close > ma200) & (rsi < 30)] = 1
        return signal
        
    def _rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

class PutCallParityArbProxy(BaseStrategy):
    """
    Put-Call Parity 套利 (Proxy)
    期權平價公式套利。在日線級別難以直接回測，用極端價量背離作為標的異動信號。
    """
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        vol = df["vol"]
        vol_ma = vol.rolling(20).mean()
        
        signal = pd.Series(0, index=df.index)
        # 價格大跌但成交量急縮 (可能存在定價偏差)
        signal[(close.pct_change() < -0.03) & (vol < vol_ma * 0.5)] = 1
        return signal

class DeltaHedgeRebalanceProxy(BaseStrategy):
    """
    Delta Hedging 再平衡 (Proxy)
    捕捉造市商 Delta Hedging 帶來的推力。
    """
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        signal = pd.Series(0, index=df.index)
        # 連續三日大漲，造市商被迫追入正股 (Gamma Squeeze Proxy)
        ret = close.pct_change()
        signal[(ret > 0.02) & (ret.shift(1) > 0.02) & (ret.shift(2) > 0.02)] = 1
        return signal

class IVSkewTradingProxy(BaseStrategy):
    """
    IV Skew 交易 (Proxy)
    利用波動率偏斜，這裡用歷史波動率的極值作為信號。
    """
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        ret = close.pct_change()
        hv20 = ret.rolling(20).std() * np.sqrt(252)
        hv20_ma = hv20.rolling(60).mean()
        
        signal = pd.Series(0, index=df.index)
        # HV20 遠高於長期均值，且股價開始企穩 (適合 Short Put 或 Iron Condor)
        signal[(hv20 > hv20_ma * 1.5) & (ret > 0)] = 1
        return signal
