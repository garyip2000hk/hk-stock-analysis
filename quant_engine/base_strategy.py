"""
base_strategy.py — 策略基類
"""
import pandas as pd
from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    name: str = "BaseStrategy"
    description: str = ""
    holding: int = 10  # 持倉天數

    @abstractmethod
    def generate_signals(self, quotes_df: pd.DataFrame, close_pivot: pd.DataFrame,
                         universe: list) -> pd.DataFrame:
        """
        返回 DataFrame columns=[date, code, signal]
        signal: 1=買入
        """
        ...

    def get_params(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_") and isinstance(v, (int, float, str, bool))}
