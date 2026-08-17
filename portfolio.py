"""portfolio.py — 每日組合權益曲線 + 真 Sharpe / 最大回撤 / 水下期。

審查報告 A5：原本 `condor_engine` 嘅 `sharpe` 只係
`ret_on_risk.mean() / ret_on_risk.std()` —— 即係**單筆回報嘅信噪比**，
冇年化、冇無風險利率、冇組合層波動、更冇處理同時開 5-11 張倉嘅重疊。
呢個數字唔可以叫 Sharpe，亦唔可以拿去同任何基準比較。

呢個模組由逐日 MTM 重建真正嘅組合權益曲線：

    每張倉都用「風險 1 單位」開（1 單位 = 該張嘅 max_loss）
    某日組合權益 = Σ（每張倉當日 MTM 損益 ÷ 該張 max_loss）
    每日回報 = Δ權益 ÷ 組合本金

**組合本金假設**（要講清楚，因為佢直接決定 Sharpe 大細）：
本金 = `capital_units` × 1 單位風險，預設 `capital_units` = 樣本期內
觀察到嘅最大同時持倉數。即係「準備好最壞情況下全部倉一齊蝕盡」嘅資本。
呢個係保守但誠實嘅口徑；如果你實際上會用更少本金（即槓桿更高），
Sharpe 會按比例放大，但回撤亦一樣放大。

用法：

    import portfolio as pf
    eq = pf.equity_curve(trades)          # trades 要有 mtm 逐日序列
    m  = pf.metrics(eq)
    print(pf.fmt(m))
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def equity_curve(trades: list[dict],
                 capital_units: float | None = None) -> pd.Series:
    """由每張倉嘅逐日 MTM 砌組合權益曲線（單位：組合本金嘅比例）。

    trades 每張倉需要：
      - "mtm": {date_str: pnl_per_share}   逐日未實現／已實現損益
      - "max_loss": float                  該張嘅 1 單位風險
      - "open", "exit_date": date_str      用嚟數同時持倉

    冇 "mtm" 嘅倉會被跳過（並唔會靜靜當零）—— 回傳嘅 Series 屬性
    `.attrs["skipped_no_mtm"]` 會記低數量。
    """
    # 重要：`mtm` 係**累計**損益，唔係逐日增量。所以某張倉平倉之後，
    # 佢嘅已實現損益必須繼續留在權益曲線上；如果只係加當日仍然持倉嘅
    # 倉位，平倉日之後權益會無故跌返落去 → 假回撤、假波幅、假 Sharpe。
    # 下面分開處理「未平倉浮動」同「已平倉實現」兩部分。
    unreal: dict[str, float] = defaultdict(float)
    open_count: dict[str, int] = defaultdict(int)
    closes: list[tuple[str, float]] = []      # (平倉日, 最終損益/風險單位)
    skipped = 0

    for t in trades:
        mtm = t.get("mtm")
        ml = t.get("max_loss")
        if not mtm or not ml:
            skipped += 1
            continue
        ml = float(ml)
        for d, pnl in mtm.items():
            unreal[d] += float(pnl) / ml
            open_count[d] += 1
        last_d = max(mtm)
        closes.append((last_d, float(mtm[last_d]) / ml))

    if not unreal:
        s = pd.Series(dtype=float)
        s.attrs["skipped_no_mtm"] = skipped
        s.attrs["capital_units"] = capital_units or 0
        return s

    days = sorted(unreal)
    idx = pd.to_datetime(days)

    realized_on: dict[str, float] = defaultdict(float)
    for d, v in closes:
        realized_on[d] += v

    vals, cum_realized = [], 0.0
    for d in days:
        # 當日權益 = 之前已平倉累計 + 當日全部有報價倉位嘅累計損益
        vals.append(cum_realized + unreal[d])
        cum_realized += realized_on.get(d, 0.0)
    raw = pd.Series(vals, index=idx)

    if capital_units is None:
        capital_units = max(open_count.values())
    capital_units = max(float(capital_units), 1.0)

    eq = raw / capital_units
    eq.attrs["skipped_no_mtm"] = skipped
    eq.attrs["capital_units"] = capital_units
    eq.attrs["max_concurrent"] = max(open_count.values())
    return eq


def metrics(equity: pd.Series, rf_annual: float = 0.0) -> dict:
    """由權益曲線（本金比例）算真 Sharpe / 最大回撤 / 最長水下期。

    equity 係「累計損益 ÷ 本金」，所以 equity=0.05 代表賺 5% 本金。
    """
    if equity is None or len(equity) < 5:
        return {"n_days": 0 if equity is None else len(equity)}

    eq = equity.sort_index()
    nav = 1.0 + eq                                # 由 1.0 起步嘅淨值
    ret = nav.pct_change().dropna()

    ann_ret = float(nav.iloc[-1] / nav.iloc[0]) ** (TRADING_DAYS / len(nav)) - 1
    ann_vol = float(ret.std(ddof=1)) * math.sqrt(TRADING_DAYS) if len(ret) > 2 else None
    sharpe = ((ann_ret - rf_annual) / ann_vol) if ann_vol else None

    down = ret[ret < 0]
    dvol = float(down.std(ddof=1)) * math.sqrt(TRADING_DAYS) if len(down) > 2 else None
    sortino = ((ann_ret - rf_annual) / dvol) if dvol else None

    peak = nav.cummax()
    dd = nav / peak - 1.0
    max_dd = float(dd.min())
    dd_end = dd.idxmin()
    dd_start = nav.loc[:dd_end].idxmax()

    underwater = (nav < peak)
    longest, cur = 0, 0
    for flag in underwater:
        cur = cur + 1 if flag else 0
        longest = max(longest, cur)

    return {
        "n_days": int(len(nav)),
        "capital_units": eq.attrs.get("capital_units"),
        "max_concurrent": eq.attrs.get("max_concurrent"),
        "total_return_pct": round(float(nav.iloc[-1] / nav.iloc[0] - 1) * 100, 2),
        "ann_return_pct": round(ann_ret * 100, 2),
        "ann_vol_pct": round(ann_vol * 100, 2) if ann_vol else None,
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "sortino": round(sortino, 2) if sortino is not None else None,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "max_dd_from": str(dd_start.date()),
        "max_dd_to": str(dd_end.date()),
        "longest_underwater_days": int(longest),
        "calmar": round(ann_ret / abs(max_dd), 2) if max_dd else None,
        "skipped_no_mtm": eq.attrs.get("skipped_no_mtm", 0),
    }


def block_bootstrap_ci(x, block: int = 21, n_boot: int = 2000,
                       stat=None, alpha: float = 0.05,
                       seed: int = 7) -> dict | None:
    """重疊樣本嘅置信區間（審查報告 B1）。

    VRP 觀測用 21 日重疊窗，相鄰觀測共用 20/21 資料 → 普通 bootstrap
    會嚴重低估標準誤。Block bootstrap 以長度 = 重疊窗長嘅連續區塊重抽，
    保留自相關結構。

    預設統計量 = 勝率（x > 0 嘅比例，%）。
    """
    a = np.asarray(pd.Series(x).dropna(), dtype=float)
    n = len(a)
    if n < block * 2:
        return None
    _named = {
        "win_rate": lambda v: float((v > 0).mean() * 100),
        "mean": lambda v: float(v.mean()),
        "median": lambda v: float(np.median(v)),
        "sharpe": lambda v: float(v.mean() / v.std(ddof=1)) if v.std(ddof=1) else 0.0,
    }
    if stat is None:
        stat = _named["win_rate"]
    elif isinstance(stat, str):
        if stat not in _named:
            raise ValueError(f"未知統計量 {stat!r}，可用：{sorted(_named)}")
        stat = _named[stat]

    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    starts_max = n - block
    out = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        starts = rng.integers(0, starts_max + 1, size=n_blocks)
        sample = np.concatenate([a[s:s + block] for s in starts])[:n]
        out[b] = stat(sample)

    lo, hi = np.quantile(out, [alpha / 2, 1 - alpha / 2])
    return {
        "point": round(stat(a), 2),
        "lo": round(float(lo), 2),
        "hi": round(float(hi), 2),
        "block": block,
        "n_obs": n,
        "n_independent": round(n / block, 1),
        "n_boot": n_boot,
    }


def fmt(m: dict) -> str:
    if not m.get("n_days"):
        return "組合曲線樣本不足（冇逐日 MTM）。"
    return "\n".join(x for x in [
        "── 組合層（每張倉風險 1 單位，本金 = 最大同時持倉數）──",
        f"  交易日數            {m['n_days']:>9}",
        f"  最大同時持倉        {m.get('max_concurrent') or 0:>9}",
        f"  總回報              {m['total_return_pct']:>8.2f}%  （本金比例）",
        f"  年化回報            {m['ann_return_pct']:>8.2f}%",
        f"  年化波幅            {m['ann_vol_pct']:>8.2f}%" if m.get("ann_vol_pct") else "",
        f"  Sharpe（真・年化）  {m['sharpe']:>8.2f}" if m.get("sharpe") is not None else "",
        f"  Sortino             {m['sortino']:>8.2f}" if m.get("sortino") is not None else "",
        f"  最大回撤            {m['max_drawdown_pct']:>8.2f}%  "
        f"（{m['max_dd_from']} → {m['max_dd_to']}）",
        f"  最長水下期          {m['longest_underwater_days']:>9} 個交易日",
        f"  Calmar              {m['calmar']:>8.2f}" if m.get("calmar") is not None else "",
        f"  ⚠ {m['skipped_no_mtm']} 張倉冇逐日 MTM，未計入曲線"
        if m.get("skipped_no_mtm") else "",
    ] if x)
