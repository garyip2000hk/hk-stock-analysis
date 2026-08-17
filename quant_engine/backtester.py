"""
backtester.py — 回測引擎
讀取策略信號，模擬交易，計算績效指標
"""
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any


class BacktestEngine:
    """
    向量化回測引擎
    - 讀取 quotes_df (原始) 同 close_pivot (close pivoted)
    - 按策略信號建倉 → 逐日計算回報
    - 輸出：equity curve, trades, metrics
    """

    def __init__(self, initial_capital: float = 1_000_000, position_pct: float = 0.1,
                 commission: float = 0.001, slippage: float = 0.001):
        self.initial_capital = initial_capital
        self.position_pct = position_pct
        self.commission = commission
        self.slippage = slippage

    def run(self, strategy, quotes_df, close_pivot, names_map, **kwargs) -> Dict[str, Any]:
        """
        執行回測，返回完整結果
        """
        universe = quotes_df["code"].unique().tolist()
        signals_df = strategy.generate_signals(quotes_df, close_pivot, universe, **kwargs)

        if signals_df.empty:
            return self._empty_result(strategy)

        dates = sorted(close_pivot.index)
        all_trades = []
        holdings: Dict[str, Dict] = {}  # code -> {entry_date, entry_price, shares}

        equity_curve = []
        cash = self.initial_capital

        for dt in dates:
            # 1. 計算當日持倉市值
            portfolio_value = cash
            for code, pos in list(holdings.items()):
                if code in close_pivot.columns and pd.notna(close_pivot.at[dt, code]):
                    portfolio_value += pos["shares"] * close_pivot.at[dt, code]
                else:
                    portfolio_value += pos["shares"] * pos["entry_price"]

            equity_curve.append({"date": dt, "equity": portfolio_value})

            # 2. 檢查是否有新信號 → 買入
            day_signals = signals_df[signals_df["date"] == dt]
            for _, sig in day_signals.iterrows():
                code = sig["code"]
                if code in holdings:
                    continue
                if code not in close_pivot.columns or pd.isna(close_pivot.at[dt, code]):
                    continue

                price = close_pivot.at[dt, code] * (1 + self.slippage)
                alloc = self.initial_capital * self.position_pct
                shares = int(alloc / price)
                if shares <= 0:
                    continue

                cost = shares * price * (1 + self.commission)
                if cost > cash:
                    continue

                cash -= cost
                holdings[code] = {
                    "entry_date": dt, "entry_price": price, "shares": shares,
                    "name": names_map.get(code, code)
                }

            # 3. 持倉到期退出 (holding days)
            for code in list(holdings.keys()):
                pos = holdings[code]
                entry_idx = dates.index(pos["entry_date"]) if pos["entry_date"] in dates else -1
                cur_idx = dates.index(dt) if dt in dates else -1
                if entry_idx >= 0 and cur_idx >= 0 and (cur_idx - entry_idx) >= strategy.holding:
                    if code in close_pivot.columns and pd.notna(close_pivot.at[dt, code]):
                        sell_price = close_pivot.at[dt, code] * (1 - self.slippage)
                        proceeds = pos["shares"] * sell_price * (1 - self.commission)
                        cash += proceeds
                        all_trades.append({
                            "code": code, "name": pos["name"],
                            "entry_date": pos["entry_date"], "exit_date": dt,
                            "entry_price": round(pos["entry_price"], 3),
                            "exit_price": round(sell_price, 3),
                            "shares": pos["shares"],
                            "pnl": round(proceeds - pos["shares"] * pos["entry_price"] * (1 + self.commission), 2),
                            "return_pct": round((sell_price / pos["entry_price"] - 1) * 100, 2)
                        })
                        del holdings[code]

        # 4. 強制平掉剩餘持倉
        last_dt = dates[-1]
        for code in list(holdings.keys()):
            pos = holdings[code]
            price = close_pivot.at[last_dt, code] if code in close_pivot.columns and pd.notna(close_pivot.at[last_dt, code]) else pos["entry_price"]
            sell_price = price * (1 - self.slippage)
            proceeds = pos["shares"] * sell_price * (1 - self.commission)
            cash += proceeds
            all_trades.append({
                "code": code, "name": pos["name"],
                "entry_date": pos["entry_date"], "exit_date": last_dt,
                "entry_price": round(pos["entry_price"], 3),
                "exit_price": round(sell_price, 3),
                "shares": pos["shares"],
                "pnl": round(proceeds - pos["shares"] * pos["entry_price"] * (1 + self.commission), 2),
                "return_pct": round((sell_price / pos["entry_price"] - 1) * 100, 2)
            })

        eq_df = pd.DataFrame(equity_curve)
        metrics = self._calc_metrics(eq_df, all_trades)

        return {
            "strategy": strategy.name,
            "params": strategy.get_params(),
            "description": strategy.description,
            "metrics": metrics,
            "equity_curve": eq_df.to_dict("records"),
            "trades": all_trades,
            "total_trades": len(all_trades),
            "run_at": datetime.now().isoformat()
        }

    def _calc_metrics(self, eq_df, trades) -> Dict[str, float]:
        if eq_df.empty:
            return {}

        equity = eq_df["equity"]
        daily_ret = equity.pct_change().dropna()
        total_ret = (equity.iloc[-1] / equity.iloc[0] - 1) * 100
        n_days = len(eq_df)
        n_years = n_days / 252
        ann_ret = ((1 + total_ret / 100) ** (1 / max(n_years, 0.01)) - 1) * 100 if n_years > 0 else 0

        # 最大回撤
        peak = equity.cummax()
        drawdown = (equity - peak) / peak * 100
        max_dd = drawdown.min()
        dd_end = drawdown.idxmin()

        # Sharpe / Sortino
        if daily_ret.std() > 0:
            sharpe = daily_ret.mean() / daily_ret.std() * np.sqrt(252)
        else:
            sharpe = 0
        downside = daily_ret[daily_ret < 0]
        if len(downside) > 0 and downside.std() > 0:
            sortino = daily_ret.mean() / downside.std() * np.sqrt(252)
        else:
            sortino = 0

        # Win rate
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        win_rate = len(wins) / len(trades) * 100 if trades else 0
        avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
        avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0

        # Calmar Ratio
        calmar = ann_ret / abs(max_dd) if max_dd != 0 else 0

        return {
            "total_return_pct": round(total_ret, 2),
            "annual_return_pct": round(ann_ret, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "dd_end_date": str(eq_df.at[dd_end, "date"]) if dd_end else "",
            "sharpe_ratio": round(sharpe, 3),
            "sortino_ratio": round(sortino, 3),
            "calmar_ratio": round(calmar, 3),
            "win_rate_pct": round(win_rate, 1),
            "total_trades": len(trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(abs(avg_win * len(wins) / (avg_loss * len(losses))), 2) if losses and avg_loss != 0 else 0,
            "start_date": str(eq_df["date"].iloc[0]),
            "end_date": str(eq_df["date"].iloc[-1]),
            "trading_days": n_days,
        }

    def _empty_result(self, strategy):
        return {
            "strategy": strategy.name,
            "params": strategy.get_params(),
            "description": strategy.description,
            "metrics": {},
            "equity_curve": [],
            "trades": [],
            "total_trades": 0,
            "run_at": datetime.now().isoformat()
        }
