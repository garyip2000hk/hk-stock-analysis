"""vrp_engine.py — 波幅風險溢價（Variance Risk Premium）引擎。

賣期權長期賺錢嘅唯一原因：IV 系統性高過之後真正發生嘅波幅。
呢個溢價叫 VRP。但 VRP 唔係每隻股票、每個時候都存在 —— 要量化。

呢個模組唔用「IV vs HV20」（回望）判斷貴平，因為咁係錯嘅比較：
IV 定價嘅係「未來 N 日」，HV20 量嘅係「過去 20 日」。正確做法係
**前瞻已實現波幅**（forward realized vol）：

    VRP(t) = IV(t) − RV(t → t+N)

即係「當日 IV」減「之後 N 日真正發生嘅波幅」。用歷史數據，就可以
統計每隻股票嘅 VRP 分佈：均值、勝率、標準差。

  VRP 均值高 + 勝率高 + 波動細  →  賣方優勢穩定
  VRP 均值近零或負              →  賣方冇優勢，賣就係賭
  VRP 勝率高但偶爾大負值        →  收細銀雞、爆大炸彈（要嚴控倉位）

另外計「當前 VRP 百分位」：今日嘅 IV 相對呢隻股票自己歷史 VRP 分佈
處於咩位置。百分位高 = 現在特別貴 = 賣方時機好。

數據源：`atm_history.py` 重建嘅乾淨 ATM IV 歷史（唔用 iv_history.parquet
嘅 class summary，因為佢有污染值，例如 JD 顯示 116% 而實際 32%）。

CLI:
    python3 vrp_engine.py --stock 00700
    python3 vrp_engine.py --limit 30              # 全市場 VRP 排行
    python3 vrp_engine.py --tradeable             # 只顯示通過全部 filter
    python3 vrp_engine.py --json
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import atm_history as ah

BASE = Path(__file__).parent
QUOTES = BASE / "imported" / "quotes.json"

HORIZON = 21          # 前瞻交易日（≈ 1 個月）
TARGET_DTE = 30       # 用邊個 DTE 附近嘅 IV
MIN_HISTORY = 40      # 至少要幾個 VRP 觀測值
MIN_OI = 3000         # 最低未平倉（流動性）
MIN_TURNOVER = 3e6    # 最低股票日均成交額
MIN_CHAIN_VOL = 200   # 該到期月全鏈最低日成交（真係有人交易）
MAX_IV = 90.0         # IV 上限：高過呢個通常係停牌／重組殘留報價
MAX_STALE_DAYS = 5    # 期權報價唔可以舊過幾日


def load_quotes() -> pd.DataFrame:
    """股價歷史 → 寬表（index=date, columns=stock_code, values=close）。"""
    if not QUOTES.exists():
        return pd.DataFrame()
    raw = json.loads(QUOTES.read_text())
    q = raw.get("quotes", raw)
    rows: dict[str, dict[str, float]] = {}
    turn: dict[str, dict[str, float]] = {}
    for d, stocks in q.items():
        for code, rec in stocks.items():
            if not isinstance(rec, dict):
                continue
            c = rec.get("close")
            if c:
                rows.setdefault(code, {})[d] = float(c)
            t = rec.get("turnover")
            if t:
                turn.setdefault(code, {})[d] = float(t)
    px = pd.DataFrame(rows)
    px.index = pd.to_datetime(px.index)
    px = px.sort_index()
    tv = pd.DataFrame(turn)
    tv.index = pd.to_datetime(tv.index)
    return px, tv.sort_index()


def realized_vol_forward(px: pd.Series, horizon: int = HORIZON) -> pd.Series:
    """前瞻已實現波幅：由 t 開始之後 horizon 日嘅年化波幅（%）。"""
    ret = np.log(px / px.shift(1))
    fwd = ret.shift(-1).rolling(horizon).std() * math.sqrt(252) * 100
    return fwd.shift(-(horizon - 1))


def realized_vol_back(px: pd.Series, window: int = HORIZON) -> pd.Series:
    ret = np.log(px / px.shift(1))
    return ret.rolling(window).std() * math.sqrt(252) * 100


def _stats(s: pd.Series) -> dict:
    s = s.dropna()
    if len(s) < 5:
        return {"n": len(s)}
    return {
        "n": int(len(s)),
        "mean": round(float(s.mean()), 2),
        "median": round(float(s.median()), 2),
        "std": round(float(s.std(ddof=1)), 2),
        "win_rate": round(float((s > 0).mean() * 100), 1),
        "p05": round(float(s.quantile(0.05)), 2),
        "p95": round(float(s.quantile(0.95)), 2),
        "worst": round(float(s.min()), 2),
    }


def _grade(vrp: dict, cur_pct: float | None, iv_rank: float | None) -> tuple[str, float]:
    """0-10 分：VRP 質素 + 當前時機。"""
    if vrp.get("n", 0) < MIN_HISTORY:
        return "資料不足", 0.0

    score = 0.0
    mean, win, std = vrp["mean"], vrp["win_rate"], vrp["std"]

    # VRP 均值（最重要）
    if mean >= 8:
        score += 3.5
    elif mean >= 5:
        score += 2.8
    elif mean >= 3:
        score += 2.0
    elif mean >= 1:
        score += 1.0

    # 勝率
    if win >= 75:
        score += 2.5
    elif win >= 65:
        score += 2.0
    elif win >= 55:
        score += 1.2
    elif win >= 50:
        score += 0.5

    # 穩定性（VRP 相對標準差）
    sharpe = mean / std if std else 0
    if sharpe >= 0.8:
        score += 2.0
    elif sharpe >= 0.5:
        score += 1.4
    elif sharpe >= 0.3:
        score += 0.8

    # 尾部風險（最壞情況）
    if vrp["worst"] > -15:
        score += 1.0
    elif vrp["worst"] > -30:
        score += 0.5

    # 當前時機
    if cur_pct is not None:
        if cur_pct >= 75:
            score += 1.0
        elif cur_pct >= 55:
            score += 0.5
        elif cur_pct < 25:
            score -= 1.0

    score = max(0.0, min(10.0, score))
    if score >= 8:
        label = "A・賣方優勢強"
    elif score >= 6.5:
        label = "B・賣方優勢好"
    elif score >= 5:
        label = "C・輕微優勢"
    elif score >= 3:
        label = "D・優勢不明"
    else:
        label = "E・不宜賣方"
    return label, round(score, 2)


def analyse(code: str, iv_hist: pd.DataFrame, px: pd.DataFrame, tv: pd.DataFrame,
            horizon: int = HORIZON) -> dict | None:
    """單一股票 VRP 分析。"""
    code = code.zfill(5)
    sub = iv_hist[iv_hist.stock_code == code]
    if sub.empty or code not in px.columns:
        return None

    # 每日揀最接近 TARGET_DTE 嘅到期月（要有流動性）
    liq = sub[sub.oi.fillna(0) >= 500]
    if liq.empty:
        liq = sub
    liq = liq.assign(gap=(liq.dte - TARGET_DTE).abs())
    daily = liq.sort_values(["date", "gap"]).groupby("date").first().reset_index()
    if len(daily) < 10:
        return None

    iv_ser = daily.set_index(pd.to_datetime(daily.date)).atm_iv.sort_index()
    price = px[code].dropna()
    rv_fwd = realized_vol_forward(price, horizon)
    rv_back = realized_vol_back(price, horizon)

    common = iv_ser.index.intersection(rv_fwd.dropna().index)
    if len(common) < 10:
        vrp_ser = pd.Series(dtype=float)
    else:
        vrp_ser = (iv_ser.reindex(common) - rv_fwd.reindex(common)).dropna()

    vrp = _stats(vrp_ser)

    latest = daily.iloc[-1]
    cur_iv = float(latest.atm_iv)
    cur_dte = int(latest.dte)
    cur_rv = None
    rvb = rv_back.dropna()
    if len(rvb):
        asof = rvb.index[rvb.index <= iv_ser.index[-1]]
        if len(asof):
            v = float(rvb.loc[asof[-1]])
            cur_rv = v if v > 0 else None

    # 當前 VRP 用「IV − 近期已實現波幅」做代理（未來 RV 未知）
    cur_vrp = round(cur_iv - cur_rv, 2) if cur_rv else None
    cur_pct = None
    if cur_vrp is not None and len(vrp_ser) >= 20:
        cur_pct = round(float((vrp_ser < cur_vrp).mean() * 100), 1)

    iv_rank = None
    if len(iv_ser) >= 30:
        lo, hi = float(iv_ser.min()), float(iv_ser.max())
        if hi > lo:
            iv_rank = round((cur_iv - lo) / (hi - lo) * 100, 1)

    oi = float(latest.oi or 0)
    chain_vol = float(getattr(latest, "volume", 0) or 0)
    turnover = None
    if code in tv.columns:
        t = tv[code].dropna().tail(20)
        turnover = float(t.mean()) if len(t) else None

    label, score = _grade(vrp, cur_pct, iv_rank)

    latest_mkt = pd.to_datetime(iv_hist.date.max())
    stale_days = int((latest_mkt - pd.to_datetime(latest.date)).days)

    liq_ok = (
        oi >= MIN_OI
        and (turnover or 0) >= MIN_TURNOVER
        and chain_vol >= MIN_CHAIN_VOL
        and cur_iv <= MAX_IV
        and stale_days <= MAX_STALE_DAYS
    )
    hist_ok = vrp.get("n", 0) >= MIN_HISTORY

    return {
        "stock_code": code,
        "name": latest["name"],
        "close": float(latest.close),
        "date": str(latest.date),
        "iv": cur_iv,
        "dte": cur_dte,
        "rv_recent": round(cur_rv, 2) if cur_rv else None,
        "iv_rv_ratio": round(cur_iv / cur_rv, 2) if cur_rv else None,
        "cur_vrp": cur_vrp,
        "cur_vrp_pct": cur_pct,
        "iv_rank": iv_rank,
        "vrp": vrp,
        "oi": oi,
        "chain_vol": chain_vol,
        "stale_days": stale_days,
        "turnover": turnover,
        "liquidity_ok": liq_ok,
        "history_ok": hist_ok,
        "grade": label,
        "score": score,
        "tradeable": score >= 6 and liq_ok and hist_ok,
    }


def scan(horizon: int = HORIZON) -> list[dict]:
    iv_hist = ah.load()
    if iv_hist.empty:
        return []
    px, tv = load_quotes()
    if px.empty:
        return []
    out = []
    for code in sorted(iv_hist.stock_code.dropna().unique()):
        r = analyse(code, iv_hist, px, tv, horizon)
        if r:
            out.append(r)
    return sorted(out, key=lambda r: -r["score"])


def _print_one(r: dict) -> None:
    v = r["vrp"]
    print(f"{r['stock_code']} {r['name']}    收市 {r['close']}    ({r['date']})\n")
    print("── 今日波幅定價 ──")
    print(f"  ATM IV              {r['iv']:.1f}%   (DTE {r['dte']})")
    if r["rv_recent"]:
        print(f"  近期已實現波幅       {r['rv_recent']:.1f}%")
        print(f"  IV / RV             {r['iv_rv_ratio']:.2f}")
    if r["iv_rank"] is not None:
        print(f"  IV Rank             {r['iv_rank']:.0f}%   (自身歷史區間位置)")
    if r["cur_vrp"] is not None:
        pct = f"  → 歷史百分位 {r['cur_vrp_pct']:.0f}%" if r["cur_vrp_pct"] is not None else ""
        print(f"  當前 VRP            {r['cur_vrp']:+.1f}{pct}")

    print(f"\n── 歷史 VRP（前瞻 {HORIZON} 日）──")
    if v.get("n", 0) < 5:
        print(f"  觀測值只有 {v.get('n', 0)} 個，不足以統計")
    else:
        print(f"  觀測值              {v['n']} 個")
        print(f"  VRP 均值            {v['mean']:+.2f}   （IV 平均高過實際波幅幾多）")
        print(f"  VRP 中位數          {v['median']:+.2f}")
        print(f"  勝率                {v['win_rate']:.0f}%   （IV > 之後實際波幅嘅日子）")
        print(f"  標準差              {v['std']:.2f}")
        print(f"  5% / 95% 分位       {v['p05']:+.1f} / {v['p95']:+.1f}")
        print(f"  最壞情況            {v['worst']:+.1f}")
        if v["std"]:
            print(f"  VRP Sharpe          {v['mean'] / v['std']:.2f}")

    print("\n── Filter ──")
    tv_s = f"{r['turnover']/1e6:.1f}M" if r["turnover"] else "—"
    print(f"  未平倉／成交額       {int(r['oi']):,} / {tv_s}   "
          f"{'✓ 夠流通' if r['liquidity_ok'] else '✗ 唔夠流通'}")
    print(f"  歷史充足            {'✓' if r['history_ok'] else '✗'}")
    print(f"\n  ▶ {r['grade']}   (score {r['score']:.2f}/10)")


def main() -> None:
    ap = argparse.ArgumentParser(description="波幅風險溢價（VRP）引擎")
    ap.add_argument("--stock", help="單一股票代號")
    ap.add_argument("--horizon", type=int, default=HORIZON, help="前瞻交易日")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--tradeable", action="store_true", help="只顯示通過全部 filter")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.stock:
        iv_hist = ah.load()
        px, tv = load_quotes()
        r = analyse(a.stock, iv_hist, px, tv, a.horizon)
        if not r:
            print(f"{a.stock} 冇足夠數據")
            return
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            _print_one(r)
        return

    rows = scan(a.horizon)
    if a.tradeable:
        rows = [r for r in rows if r["tradeable"]]
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    print(f"=== VRP 排行（前瞻 {a.horizon} 日，{len(rows)} 隻）===\n")
    print(f"{'代號':>6} {'名稱':<20} {'IV':>6} {'RV':>6} {'IV/RV':>6} "
          f"{'VRP均':>6} {'勝率':>5} {'Sharpe':>7} {'IVR':>5} {'n':>4} {'分':>5}  評級")
    for r in rows[:a.limit]:
        v = r["vrp"]
        if v.get("n", 0) < 5:
            continue
        sh = v["mean"] / v["std"] if v.get("std") else 0
        rv = f"{r['rv_recent']:>6.1f}" if r["rv_recent"] else "     —"
        ratio = f"{r['iv_rv_ratio']:>6.2f}" if r["iv_rv_ratio"] else "     —"
        ivr = f"{r['iv_rank']:>5.0f}" if r["iv_rank"] is not None else "    —"
        print(f" {r['stock_code']:>5} {r['name'][:20]:<20} {r['iv']:>6.1f} {rv} {ratio} "
              f"{v['mean']:>+6.1f} {v['win_rate']:>4.0f}% {sh:>7.2f} {ivr} "
              f"{v['n']:>4} {r['score']:>5.2f}  {r['grade']}")

    ok = [r for r in rows if r["tradeable"]]
    print(f"\n通過全部 filter（分數 ≥ 6、夠流通、歷史夠）：{len(ok)} 隻")
    if ok and not a.tradeable:
        print("  " + "、".join(f"{r['stock_code']} {r['name'][:12]}" for r in ok[:10]))


if __name__ == "__main__":
    main()
