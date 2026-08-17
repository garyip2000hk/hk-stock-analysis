"""
base_strategy.py — 策略基類
所有策略必須繼承並實作 generate_signals()
"""
from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    name = "Base"
    description = ""
    holding = 10  # 默認持有天數

    @abstractmethod
    def generate_signals(self, quotes_df, close_pivot, universe):
        """返回 DataFrame: columns = [date, code, score]"""
        pass

    def get_params(self):
        return {
            k: v for k, v in self.__dict__.items()
            if not k.startswith("_") and not callable(v)
        }
