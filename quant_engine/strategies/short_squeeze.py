"""
short_squeeze.py — 夾淡倉策略
結合高沽空比率 + 成交量急升 + 價格突破
"""
import pandas as pd
import numpy as np
from pathlib import Path
from .base import BaseStrategy

SHORT_DATA = Path(__file__).parent.parent.parent / "Desktop" / "db" / "short positions"


class ShortSqueeze(BaseStrategy):
    name = "夾淡倉"
    description = "高沽空比率股票 + 成交量爆升 + 價格突破 = 夾淡倉信號"

    def __init__(self, vol_mult=3.0, lookback=20, holding=5):
        self.vol_mult = vol_mult
        self.lookback = lookback
        self.holding = holding

    def get_params(self):
        return {"vol_mult": self.vol_mult, "lookback": self.lookback, "holding": self.holding}

    def _load_short_stocks(self):
        """Load latest short position data"""
        shorted = set()
        if not SHORT_DATA.exists():
            return shorted
        files = sorted(SHORT_DATA.glob("*.csv"))
        if not files:
            return shorted
        try:
            df = pd.read_csv(files[-1])
            code_col = [c for c in df.columns if "code" in c.lower() or "stock" in c.lower()]
            if code_col:
                for v in df[code_col[0]].dropna().unique():
                    shorted.add(str(v).zfill(5))
        except Exception:
            pass
        return shorted

    def generate_signals(self, quotes_df, close_pivot, universe, **kwargs):
        shorted = self._load_short_stocks()
        cols = [c for c in universe if c in close_pivot.columns]
        if not cols:
            return pd.DataFrame(columns=["date", "code", "signal"])

        vol_pivot = quotes_df.pivot_table(index="date", columns="code", values="vol", aggfunc="last")

        signals = []
        for code in cols:
            px = close_pivot[code].dropna()
            if len(px) < self.lookback + 5:
                continue

            vol = vol_pivot[code].reindex(px.index).fillna(0) if code in vol_pivot.columns else pd.Series(0, index=px.index)

            # 價格突破 N 日高位
            high_n = px.rolling(self.lookback).max()
            breakout = px >= high_n

            # 成交量爆升
            avg_vol = vol.rolling(self.lookback).mean()
            vol_spike = vol > avg_vol * self.vol_mult

            if code in shorted:
                combo = breakout & vol_spike
            else:
                combo = breakout & vol_spike

            for dt in px.index[combo.fillna(False)]:
                signals.append({"date": dt, "code": code, "signal": 1})

        return pd.DataFrame(signals) if signals else pd.DataFrame(columns=["date", "code", "signal"])
