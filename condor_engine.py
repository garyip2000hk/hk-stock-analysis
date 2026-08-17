"""condor_engine.py — Iron Condor 建構 + 真實歷史回測。

由 VRP 引擎揀出「IV 貴」嘅標的之後，呢個模組負責：

  1. 建構：用 delta 揀 short strike（預設 0.20 delta），
     wing 揀落一至兩格行使價，計淨收入／最大蝕／盈虧平衡。
  2. 過濾：短腳必須有真成交同未平倉，credit/width 要夠。
  3. 回測：逐日開倉、**逐日用真實期權鏈重估**、50% 止賺 / 2× 止損 /
     14 DTE 強制平倉，計真實勝率同期望值。

## Stage 0 修正（2026-08-17，對應審查報告 A1-A6）

| 報告項 | 原本 | 現在 |
|---|---|---|
| A1 | 用**到期**內在值公式配 exit_day 股價估平倉 → 當 4 條腳時間值係零 | `exit_day` 讀返該日真實鏈嘅 4 條腳 settle，`pnl = credit − exit_cost`；鏈缺失才用 BS 模型價並標記 `exit_px_source="model"` |
| A2 | `PROFIT_TARGET = 0.50` 全 repo 從未被使用，兩行 clamp 係 no-op → 實際係硬持到 exit_day | 逐個交易日重估，`pnl ≥ 50% × credit` 平倉止賺、`pnl ≤ −2 × credit` 止損，`exit_reason` 記錄邊個規則觸發 |
| A3 | `touched` 只睇收市價 | 有 high/low 就用日內高低（`touch_basis="intraday"`）；冇就標明 `"close"`，唔會假裝 |
| A4 | credit 直接由 settle 相加減，零成本零滑價 | 全部經 `costs.CostModel`：賣腳收 bid、買腳付 ask，開倉平倉各跨一次；`--sensitivity` 一次跑 0/1/3/5% |
| A5 | `sharpe` 其實只係單筆回報均值／標準差 | 改名 `signal_to_noise`；真 Sharpe / 最大回撤 / 最長水下期由 `portfolio.py` 用逐日組合權益曲線計 |
| A6 | `if after.empty: continue` 靜默丟樣本 | 全部跳過原因入 `skipped` 計數並印出 |

*重點*：回測用嘅係逐個行使價嘅真實結算價（由 HKEX raw 報告拆出），
但**結算價唔等於可成交價** —— 所以必須配合滑價模型睇。真實 bid/ask
要靠 `futu_option_chain.py` 由 Futu OpenD 收集，再用
`costs.from_measured_spreads()` 反推 `--slippage` 餵返呢個回測。

CLI:
    python3 condor_engine.py --stock 09626                    # 今日建構一張
    python3 condor_engine.py --backtest 09626                 # 單股回測
    python3 condor_engine.py --backtest 09626 --sensitivity   # 0/1/3/5% 滑價
    python3 condor_engine.py --backtest-all --slippage 0.03
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import bs
import chain_cache as cc
import costs
import portfolio as pf

BASE = Path(__file__).parent
OUT = BASE / "condor_backtest.json"

SHORT_DELTA = 0.20      # 短腳目標 delta（≈80% 價外概率）
MIN_DTE, MAX_DTE = 25, 55
MIN_LEG_OI = 100        # 每條腳最低未平倉
MIN_CREDIT_RATIO = 0.12 # 淨收入 / 翼寬，低過呢個唔值得做（口徑：扣滑價後）
PROFIT_TARGET = 0.50    # 賺到 credit 嘅 50% 就平（現在真正實現）
STOP_LOSS_MULT = 2.0    # 蝕到 2 × credit 就止損（現在真正實現）
FORCE_EXIT_DTE = 14     # 剩 14 日強制平（避免 gamma 爆）
MIN_TRADES = 5          # 少過呢個唔出統計

DEFAULT_SLIPPAGE = 0.03 # 每腳滑價（中價比例）；用 --slippage 覆蓋
TIME_BASIS = "calendar" # "calendar"（dte/365）或 "trading"（審查報告 C3）

# 港股公假（每行 YYYY-MM-DD）。刻意唔硬編喺程式裡面 —— 硬編一定會過時，
# 而過時嘅假期表比冇假期表更危險（你會以為已經處理咗）。
HOLIDAY_FILE = BASE / "options_data" / "hk_holidays.txt"


def _load_holidays() -> np.ndarray:
    if not HOLIDAY_FILE.exists():
        return np.array([], dtype="datetime64[D]")
    try:
        ds = [ln.strip() for ln in HOLIDAY_FILE.read_text().splitlines()
              if ln.strip() and not ln.startswith("#")]
        return np.array(ds, dtype="datetime64[D]")
    except (ValueError, OSError):
        return np.array([], dtype="datetime64[D]")


HK_HOLIDAYS = _load_holidays()


def _t(dte: int, basis: str | None = None,
       start: date | None = None) -> float:
    """年化時間。

    審查報告 C3：`dte/365` 用日曆日，但已實現波幅用 `sqrt(252)` 交易日，
    兩個口徑掉了鐘。農曆年／國慶前後 30 個日曆日只有 ~17 個交易日，
    用 30/365 會高估有幾多波幅可以發生 → 高估期權價／低估 theta 收入速度。

    basis="trading" 而且有 `start` 時，就真實數營業日（np.busday_count，
    扣除 `HK_HOLIDAYS`）再除 252。**冇 `start` 就退回日曆口徑**，因為
    `dte × 252/365 ÷ 252` 數學上等於 `dte/365`，寫咗亦係 no-op，
    唔應該假裝改咗。

    ⚠ 誠實講清楚幅度：只扣週末的話，30 個日曆日 = 21-22 個營業日，
    22/252 = 0.0873 vs 30/365 = 0.0822 —— trading 口徑其實**略大**。
    真正令 theta 偏樂觀嘅係**公假**（農曆年、清明、國慶連假），
    所以要見到差異，`HK_HOLIDAYS` 必須有內容。呢個檔案冇硬編港假日曆
    （會過時），要用就填 `options_data/hk_holidays.txt`（每行 YYYY-MM-DD）。
    """
    basis = basis or TIME_BASIS
    d = max(dte, 1)
    if basis == "trading" and start is not None:
        bd = int(np.busday_count(start, start + timedelta(days=d),
                                 holidays=HK_HOLIDAYS))
        return max(bd, 1) / 252.0
    return d / 365.0


def _delta(spot: float, strike: float, dte: int, iv_pct: float, cp: str,
           as_of: date | None = None) -> float | None:
    g = bs.greeks(spot, strike, _t(dte, start=as_of), (iv_pct or 0) / 100.0, cp)
    return g.get("delta") if g else None


def _pick_by_delta(legs: pd.DataFrame, spot: float, dte: int,
                   target: float, cp: str,
                   as_of: date | None = None) -> pd.Series | None:
    """喺一堆同類型合約中揀 |delta| 最接近 target 嘅一條。"""
    sub = legs[(legs.type == cp) & (legs.oi.fillna(0) >= MIN_LEG_OI)].copy()
    if sub.empty:
        return None
    sub["dlt"] = sub.apply(
        lambda r: abs(_delta(spot, r.strike, dte, r.iv, cp, as_of) or 9), axis=1)
    sub = sub[sub.dlt < 5]
    if sub.empty:
        return None
    return sub.iloc[(sub.dlt - target).abs().argsort().iloc[0]]


# ─────────────────────────────────────────────────────────────
# 建構
# ─────────────────────────────────────────────────────────────
def build(code: str, as_of: date | None = None,
          short_delta: float = SHORT_DELTA,
          chain_df=None,
          cost: costs.CostModel | None = None) -> dict | None:
    """今日（或指定日）建一張 Iron Condor。

    cost=None 時用 `costs.DEFAULT_COST`（3% per leg）。想睇零成本基準
    就傳 `costs.ZERO_COST` —— 但唔應該用零成本嘅數字做決策。
    """
    code = str(code).zfill(5)
    cost = cost or costs.DEFAULT_COST

    if chain_df is not None:
        ch = chain_df[chain_df.stock_code == code]
    else:
        ch = cc.day(as_of or cc.latest(), code)
    if ch is None or ch.empty:
        return None

    cand = ch[(ch.dte >= MIN_DTE) & (ch.dte <= MAX_DTE)]
    if cand.empty:
        return None
    # 揀成交／未平倉最好嘅到期月
    best_exp = (cand.groupby("expiry")
                .agg(v=("volume", "sum"), o=("oi", "sum"))
                .assign(s=lambda d: d.v + d.o * 0.1)
                .s.idxmax())
    legs = cand[cand.expiry == best_exp]
    spot = float(legs.close.iloc[0])
    dte = int(legs.dte.iloc[0])

    as_of_d = legs.date.iloc[0]
    sc = _pick_by_delta(legs, spot, dte, short_delta, "C", as_of_d)
    sp = _pick_by_delta(legs, spot, dte, short_delta, "P", as_of_d)
    if sc is None or sp is None:
        return None

    # 翼寬目標：現價 5%
    # 審查報告 C6：固定 5% 冇隨 IV 縮放（IV 60% 同 IV 20% 風險差天共地）。
    # 屬 Stage 1 範圍，未改，但已標記。
    wing_gap = max(spot * 0.05, 1.0)
    calls = legs[(legs.type == "C") & (legs.strike > sc.strike)].sort_values("strike")
    puts = legs[(legs.type == "P") & (legs.strike < sp.strike)].sort_values(
        "strike", ascending=False)
    if calls.empty or puts.empty:
        return None
    lc = calls.iloc[(calls.strike - (float(sc.strike) + wing_gap)).abs()
                    .argsort().iloc[0]]
    lp = puts.iloc[(puts.strike - (float(sp.strike) - wing_gap)).abs()
                   .argsort().iloc[0]]

    leg_objs = [
        costs.Leg(mid=float(sc.settle), side="short"),
        costs.Leg(mid=float(sp.settle), side="short"),
        costs.Leg(mid=float(lc.settle), side="long"),
        costs.Leg(mid=float(lp.settle), side="long"),
    ]
    mid_credit = costs.ZERO_COST.open_credit(leg_objs)   # 舊口徑（結算價中價）
    credit = cost.open_credit(leg_objs)                  # 實收（跨價差後）

    w_call = float(lc.strike) - float(sc.strike)
    w_put = float(sp.strike) - float(lp.strike)
    width = max(w_call, w_put)
    if credit <= 0 or width <= 0:
        return None

    max_loss = width - credit
    ratio = credit / width
    lo_be = float(sp.strike) - credit
    hi_be = float(sc.strike) + credit

    atm = legs.iloc[(legs.strike - spot).abs().argsort().iloc[0]]
    atm_iv = float(atm.iv or 0)
    pwin = None
    if atm_iv > 0:
        sd = atm_iv / 100 * np.sqrt(_t(dte, start=as_of_d))
        z_hi = np.log(hi_be / spot) / sd
        z_lo = np.log(lo_be / spot) / sd
        pwin = round(float(bs._cdf(z_hi) - bs._cdf(z_lo)) * 100, 1)

    return {
        "stock_code": code,
        "name": str(legs["name"].iloc[0]),
        "date": str(legs.date.iloc[0]),
        "spot": spot,
        "expiry": str(best_exp),
        "dte": dte,
        "atm_iv": atm_iv,
        "short_call": float(sc.strike), "sc_px": float(sc.settle),
        "long_call": float(lc.strike), "lc_px": float(lc.settle),
        "short_put": float(sp.strike), "sp_px": float(sp.settle),
        "long_put": float(lp.strike), "lp_px": float(lp.settle),
        "mid_credit": round(mid_credit, 3),
        "credit": round(credit, 3),
        "cost_drag": round(mid_credit - credit, 3),
        "width": width,
        "max_loss": round(max_loss, 3),
        "credit_ratio": round(ratio, 3),
        "mid_credit_ratio": round(mid_credit / width, 3),
        "be_low": round(lo_be, 2),
        "be_high": round(hi_be, 2),
        "range_pct": round((hi_be - lo_be) / spot * 100, 1),
        "p_win_model": pwin,
        "leg_oi_min": float(min(sc.oi or 0, sp.oi or 0, lc.oi or 0, lp.oi or 0)),
        "short_oi_min": float(min(sc.oi or 0, sp.oi or 0)),
        "slippage_per_leg": cost.slippage_per_leg,
        "ok": (ratio >= MIN_CREDIT_RATIO
               and min(sc.oi or 0, sp.oi or 0) >= MIN_LEG_OI),
    }


# ─────────────────────────────────────────────────────────────
# 估值（A1：真實鏈平倉價）
# ─────────────────────────────────────────────────────────────
def _payoff(spot_final: float, c: dict) -> float:
    """到期損益（每股，未計成本）。只用於真正持到到期嘅情況。"""
    call_loss = max(0.0, spot_final - c["short_call"]) - max(0.0, spot_final - c["long_call"])
    put_loss = max(0.0, c["short_put"] - spot_final) - max(0.0, c["long_put"] - spot_final)
    return c["credit"] - call_loss - put_loss


_LEG_SPEC = (("short_call", "C", "short"), ("short_put", "P", "short"),
             ("long_call", "C", "long"), ("long_put", "P", "long"))


def value_legs(d: date, code: str, c: dict, spot: float,
               iv_hint: float | None = None,
               basis: str | None = None) -> tuple[list[costs.Leg] | None, str]:
    """某日 4 條腳嘅中價。

    優先用該日真實鏈嘅 settle；某條腳當日冇報價（常見於深度價外）
    才用 BS 模型價補，並把來源標成 "model"。**唔會**用到期內在值。
    """
    exp = date.fromisoformat(c["expiry"])
    dte = max((exp - d).days, 0)
    iv = cc.atm_iv(d, code, exp, spot)
    if iv is None:
        iv = iv_hint
    legs: list[costs.Leg] = []
    src = "chain"
    for key, cp, side in _LEG_SPEC:
        strike = float(c[key])
        m = cc.leg_mid(d, code, exp, strike, cp)
        if m is None or m <= 0:
            if not iv or iv <= 0:
                return None, "none"
            m = bs.price(spot, strike, _t(dte, basis, d), iv / 100.0, cp)
            if m is None:
                return None, "none"
            src = "model"
        legs.append(costs.Leg(mid=float(m), side=side))
    return legs, src


# ─────────────────────────────────────────────────────────────
# 回測
# ─────────────────────────────────────────────────────────────
def _touched_on(d, close_s, hi_s, lo_s, c) -> tuple[bool, str]:
    """當日有冇觸及短腳。有 high/low 就用日內（審查報告 A3）。"""
    if hi_s is not None and lo_s is not None and d in hi_s.index and d in lo_s.index:
        return (bool(hi_s.loc[d] > c["short_call"] or lo_s.loc[d] < c["short_put"]),
                "intraday")
    v = close_s.loc[d]
    return bool(v > c["short_call"] or v < c["short_put"]), "close"


def backtest(code: str, px: pd.Series, dates: list[date],
             short_delta: float = SHORT_DELTA, step: int = 5,
             cost: costs.CostModel | None = None,
             hi: pd.Series | None = None, lo: pd.Series | None = None,
             profit_target: float = PROFIT_TARGET,
             stop_loss_mult: float = STOP_LOSS_MULT,
             basis: str | None = None,
             keep_mtm: bool = True,
             min_trades: int = MIN_TRADES) -> dict | None:
    """逐 step 日開一張 condor，逐個交易日重估，按規則平倉。"""
    code = str(code).zfill(5)
    cost = cost or costs.DEFAULT_COST
    trades: list[dict] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    px = px.dropna().sort_index()

    for i in range(0, len(dates), step):
        d = dates[i]
        c = build(code, d, short_delta, cost=cost)
        if not c:
            skip("建唔到 condor（冇合適到期月／流動性）")
            continue
        if not c["ok"]:
            skip("credit ratio 或未平倉未達門檻")
            continue

        exp = date.fromisoformat(c["expiry"])
        exit_day = exp - timedelta(days=FORCE_EXIT_DTE)
        after = px[px.index.date > d]
        if after.empty:
            skip("開倉日之後冇股價（樣本尾段）")
            continue
        window = after[after.index.date <= exit_day]
        if window.empty:
            skip("開倉日到強制平倉日之間冇交易日")
            continue

        credit = c["credit"]
        max_loss = c["max_loss"]
        touched = False
        touch_basis = "close"
        mtm: dict[str, float] = {}
        model_days = 0
        valued_days = 0
        exit_reason = "time"
        exit_date = window.index[-1]
        exit_spot = float(window.iloc[-1])
        pnl = None
        exit_src = "none"

        for ts in window.index:
            day_d = ts.date()
            spot = float(window.loc[ts])
            t_flag, t_basis = _touched_on(ts, window, hi, lo, c)
            touched = touched or t_flag
            if t_basis == "intraday":
                touch_basis = "intraday"

            legs, src = value_legs(day_d, code, c, spot, c["atm_iv"], basis)
            if legs is None:
                continue                     # 該日冇報告 → 唔估，唔假裝
            valued_days += 1
            if src == "model":
                model_days += 1
            pnl_now = credit - cost.close_cost(legs)
            mtm[str(day_d)] = round(pnl_now, 4)

            if pnl_now >= profit_target * credit:
                exit_reason, exit_date, exit_spot = "target", ts, spot
                pnl, exit_src = pnl_now, src
                break
            if pnl_now <= -stop_loss_mult * credit:
                exit_reason, exit_date, exit_spot = "stop", ts, spot
                pnl, exit_src = pnl_now, src
                break
            pnl, exit_src = pnl_now, src     # 滾動到最後一日 = 強制平倉價

        if pnl is None:
            skip("持倉期間完全冇期權報價，無法估值")
            continue

        # wings 封住上下限；超界代表估值出錯，記低而唔係靜靜夾住
        floor_, cap_ = -max_loss - 1e-9, credit + 1e-9
        if pnl < floor_ or pnl > cap_:
            skip(f"估值越界（{pnl:.3f} 唔在 [{-max_loss:.3f}, {credit:.3f}]）")
        pnl = float(np.clip(pnl, -max_loss, credit))

        trades.append({
            "open": str(d), "expiry": c["expiry"], "dte": c["dte"],
            "spot": c["spot"],
            "exit_date": str(exit_date.date()), "exit_spot": exit_spot,
            "exit_reason": exit_reason, "exit_px_source": exit_src,
            "days_held": int((exit_date.date() - d).days),
            "mid_credit": c["mid_credit"], "credit": credit,
            "cost_drag": c["cost_drag"], "max_loss": max_loss,
            "pnl": round(pnl, 3),
            "ret_on_risk": round(pnl / max_loss * 100, 1) if max_loss else None,
            "touched": touched, "touch_basis": touch_basis,
            "valued_days": valued_days, "model_priced_days": model_days,
            "range_pct": c["range_pct"], "p_win_model": c["p_win_model"],
            **({"mtm": mtm} if keep_mtm else {}),
        })

    # A6：樣本不足亦要講清楚為咗咩，唔可以靜靜返 None
    if len(trades) < min_trades:
        return {"stock_code": code, "n_trades": len(trades),
                "skipped": skipped, "insufficient": True}

    t = pd.DataFrame(trades)
    wins = t[t.pnl > 0]
    rr = t.ret_on_risk.dropna()

    eq = pf.equity_curve(trades)
    port = pf.metrics(eq)
    ci = pf.block_bootstrap_ci(t.pnl, block=max(1, int(t.days_held.median() or 21)))

    return {
        "stock_code": code,
        "n_trades": len(t),
        "win_rate": round(len(wins) / len(t) * 100, 1),
        "win_rate_ci": ci,
        "avg_pnl": round(float(t.pnl.mean()), 3),
        "avg_ret_on_risk": round(float(rr.mean()), 1) if len(rr) else None,
        "median_ret": round(float(rr.median()), 1) if len(rr) else None,
        "worst": round(float(t.pnl.min()), 3),
        "best": round(float(t.pnl.max()), 3),
        "touch_rate": round(float(t.touched.mean()) * 100, 1),
        "touch_basis": ("intraday" if (t.touch_basis == "intraday").all()
                        else "mixed" if (t.touch_basis == "intraday").any()
                        else "close"),
        "avg_range_pct": round(float(t.range_pct.mean()), 1),
        "avg_p_win_model": round(float(t.p_win_model.dropna().mean()), 1)
                            if t.p_win_model.notna().any() else None,
        # 審查報告 A5：呢個唔係 Sharpe，係單筆回報嘅信噪比。真 Sharpe 在 portfolio。
        "signal_to_noise": round(float(rr.mean() / rr.std()), 2)
                            if len(rr) > 2 and rr.std() else None,
        "avg_days_held": round(float(t.days_held.mean()), 1),
        "exit_reasons": t.exit_reason.value_counts().to_dict(),
        "model_priced_exit_pct": round(
            float((t.exit_px_source == "model").mean()) * 100, 1),
        "avg_cost_drag_pct": round(
            float((t.cost_drag / t.mid_credit.replace(0, np.nan)).mean()) * 100, 1),
        "cost": {"slippage_per_leg": cost.slippage_per_leg,
                 "commission_per_leg": cost.commission_per_leg},
        "portfolio": port,
        "skipped": skipped,
        "trades": trades,
    }


def sensitivity(code: str, px: pd.Series, dates: list[date],
                short_delta: float = SHORT_DELTA, step: int = 5,
                hi=None, lo=None, commission: float = 0.0) -> list[dict]:
    """同一隻股跑 0 / 1 / 3 / 5% 每腳滑價（審查報告 A4 嘅判斷標準）。"""
    out = []
    for name, cm in costs.grid(commission).items():
        r = backtest(code, px, dates, short_delta, step, cm, hi, lo,
                     keep_mtm=False)
        if r and not r.get("insufficient"):
            out.append({"slippage": name, **{k: r.get(k) for k in
                        ("n_trades", "win_rate", "avg_ret_on_risk",
                         "median_ret", "worst", "signal_to_noise",
                         "avg_cost_drag_pct")},
                        "sharpe": (r.get("portfolio") or {}).get("sharpe"),
                        "max_dd_pct": (r.get("portfolio") or {}).get("max_drawdown_pct")})
        else:
            out.append({"slippage": name, "n_trades": 0})
    return out


# ─────────────────────────────────────────────────────────────
# 顯示
# ─────────────────────────────────────────────────────────────
def _fmt_build(c: dict) -> str:
    L = [
        f"{c['stock_code']} {c['name']}    現價 {c['spot']}    ({c['date']})",
        "",
        f"── Iron Condor  到期 {c['expiry']}  DTE {c['dte']}  ATM IV {c['atm_iv']:.1f}% ──",
        f"  買 Put  {c['long_put']:>8.2f}   @ {c['lp_px']:>7.2f}",
        f"  賣 Put  {c['short_put']:>8.2f}   @ {c['sp_px']:>7.2f}",
        f"  賣 Call {c['short_call']:>8.2f}   @ {c['sc_px']:>7.2f}",
        f"  買 Call {c['long_call']:>8.2f}   @ {c['lc_px']:>7.2f}",
        "",
        f"  中價 credit       {c['mid_credit']:>8.2f}  /股  (結算價，唔可成交)",
        f"  滑價蒸發          {c['cost_drag']:>8.2f}  /股  "
        f"(每腳 {c['slippage_per_leg']*100:.1f}%)",
        f"  實收 credit       {c['credit']:>8.2f}  /股",
        f"  最大蝕            {c['max_loss']:>8.2f}  /股",
        f"  收入／翼寬        {c['credit_ratio']*100:>7.1f}%   "
        f"(中價口徑 {c['mid_credit_ratio']*100:.1f}%，門檻 {MIN_CREDIT_RATIO*100:.0f}%)",
        f"  盈虧平衡          {c['be_low']} — {c['be_high']}   (±{c['range_pct']/2:.1f}%)",
        f"  模型勝率          {c['p_win_model']}%" if c["p_win_model"] else "",
        f"  最低腳未平倉      {c['leg_oi_min']:>8.0f}",
        "",
        f"  ▶ {'✓ 可做' if c['ok'] else '✗ 扣滑價後收入太薄，唔值得做'}",
    ]
    return "\n".join(x for x in L if x)


def _fmt_bt(r: dict) -> str:
    if r.get("insufficient"):
        return (f"{r['stock_code']} 樣本不足（{r['n_trades']} 筆）\n\n"
                + _fmt_skipped(r.get("skipped")))
    ci = r.get("win_rate_ci") or {}
    L = [
        f"{r['stock_code']}  Iron Condor 回測"
        f"（滑價 {r['cost']['slippage_per_leg']*100:.1f}%/腳）",
        "",
        f"  交易筆數          {r['n_trades']:>8}",
        f"  勝率              {r['win_rate']:>7.1f}%"
        + (f"   95% CI [{ci['lo']:.0f}%, {ci['hi']:.0f}%]"
           f"  ≈{ci['n_independent']} 個獨立樣本" if ci else ""),
        f"  模型預測勝率      {r['avg_p_win_model']}%" if r.get("avg_p_win_model") else "",
        f"  平均風險回報      {r['avg_ret_on_risk']:>7.1f}%",
        f"  中位風險回報      {r['median_ret']:>7.1f}%",
        f"  最好 / 最壞       {r['best']:.2f} / {r['worst']:.2f}",
        f"  觸價率            {r['touch_rate']:>7.1f}%   （{r['touch_basis']} 口徑）",
        f"  信噪比（非Sharpe）{r['signal_to_noise'] or 0:>7.2f}",
        f"  平均持倉          {r['avg_days_held']:>7.1f} 日",
        f"  平倉原因          {r['exit_reasons']}",
        f"  滑價蒸發          {r['avg_cost_drag_pct']:>7.1f}% 中價 credit",
        f"  模型價平倉比例    {r['model_priced_exit_pct']:>7.1f}%"
        + ("   ⚠ 偏高，代表鏈報價缺失多"
           if (r["model_priced_exit_pct"] or 0) > 30 else ""),
        "",
        pf.fmt(r.get("portfolio") or {}),
    ]
    sk = _fmt_skipped(r.get("skipped"))
    if sk:
        L += ["", sk]
    return "\n".join(x for x in L if x)


def _fmt_skipped(skipped: dict | None) -> str:
    if not skipped:
        return ""
    L = ["── 跳過嘅開倉機會（審查報告 A6：唔再靜默）──"]
    for k, v in sorted(skipped.items(), key=lambda kv: -kv[1]):
        L.append(f"  {v:>5}  {k}")
    return "\n".join(L)


def _fmt_sens(rows: list[dict]) -> str:
    L = ["── 滑價敏感度（審查報告 A4）──", "",
         f"{'每腳':>6} {'筆數':>5} {'勝率':>7} {'平均回報':>9} {'中位':>7} "
         f"{'最壞':>8} {'Sharpe':>7} {'最大回撤':>9}"]
    for r in rows:
        if not r.get("n_trades"):
            L.append(f"{r['slippage']:>6} {'—':>5}   （樣本不足）")
            continue
        L.append(f"{r['slippage']:>6} {r['n_trades']:>5} {r['win_rate']:>6.1f}% "
                 f"{(r['avg_ret_on_risk'] or 0):>8.1f}% {(r['median_ret'] or 0):>6.1f}% "
                 f"{r['worst']:>8.2f} {(r['sharpe'] or 0):>7.2f} "
                 f"{(r['max_dd_pct'] or 0):>8.1f}%")
    L += ["", "判斷標準：3% 一行仍有正期望，先值得繼續。若 3% 已轉負，",
          "策略在真實成本下唔存在 —— 呢個唔係調參可以解決嘅問題。"]
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────
def oc_dates() -> list[date]:
    """全部有 raw 報告嘅交易日。"""
    files = sorted((BASE / "options_data" / "raw").glob("dqe*.txt.gz"))
    return [datetime.strptime(f.name[3:9], "%y%m%d").date() for f in files]


def main() -> None:
    global TIME_BASIS
    ap = argparse.ArgumentParser(description="Iron Condor 建構 + 回測")
    ap.add_argument("--stock", help="建構今日一張")
    ap.add_argument("--backtest", help="單股回測")
    ap.add_argument("--backtest-all", action="store_true", help="全市場回測")
    ap.add_argument("--sensitivity", action="store_true",
                    help="跑 0/1/3/5%% 每腳滑價")
    ap.add_argument("--delta", type=float, default=SHORT_DELTA)
    ap.add_argument("--step", type=int, default=5, help="每幾日開一張")
    ap.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE,
                    help="每腳滑價（中價比例），預設 0.03")
    ap.add_argument("--commission", type=float, default=0.0,
                    help="每腳每股佣金")
    ap.add_argument("--use-measured-slippage", action="store_true",
                    help="由 options_data/chain_live.parquet 實測價差反推滑價")
    ap.add_argument("--profit-target", type=float, default=PROFIT_TARGET)
    ap.add_argument("--stop-loss", type=float, default=STOP_LOSS_MULT)
    ap.add_argument("--time-basis", choices=("calendar", "trading"),
                    default=TIME_BASIS, help="年化時間口徑（審查報告 C3）")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    TIME_BASIS = a.time_basis

    cost = costs.CostModel(slippage_per_leg=a.slippage,
                           commission_per_leg=a.commission)
    if a.use_measured_slippage:
        m = costs.from_measured_spreads()
        if m is None:
            print("⚠ 冇實測價差數據，改用 --slippage。"
                  "先跑 python3 futu_option_chain.py --snapshot")
        else:
            cost = costs.CostModel(slippage_per_leg=m.slippage_per_leg,
                                   commission_per_leg=a.commission)
            print(f"用實測滑價 {cost.slippage_per_leg*100:.2f}%/腳")

    if a.stock:
        c = build(a.stock, short_delta=a.delta, cost=cost)
        if not c:
            print(f"{a.stock} 建唔到 condor（冇合適到期月／流動性不足）")
            return
        print(json.dumps(c, ensure_ascii=False, indent=2) if a.json else _fmt_build(c))
        return

    import vrp_engine as ve
    ohlc = ve.load_ohlc()
    px_all = ohlc["close"]
    hi_all, lo_all = ohlc.get("high"), ohlc.get("low")
    raw_dates = oc_dates()
    if not raw_dates:
        print("冇 options_data/raw/dqe*.txt.gz —— 先跑 options_scraper.py")
        return

    def series(df, code):
        return df[code].dropna() if (df is not None and code in df.columns) else None

    if a.backtest:
        code = str(a.backtest).zfill(5)
        if code not in px_all.columns:
            print(f"{code} 冇股價歷史")
            return
        px = px_all[code].dropna()
        hi, lo = series(hi_all, code), series(lo_all, code)
        if hi is None or lo is None:
            print("⚠ quotes.json 冇 high/low → 觸價率用收市價口徑（會低估）")
        if a.sensitivity:
            rows = sensitivity(code, px, raw_dates, a.delta, a.step, hi, lo,
                               a.commission)
            print(json.dumps(rows, ensure_ascii=False, indent=2) if a.json
                  else _fmt_sens(rows))
            return
        r = backtest(code, px, raw_dates, a.delta, a.step, cost, hi, lo,
                     a.profit_target, a.stop_loss)
        if not r:
            print(f"{code} 回測樣本不足")
            return
        print(json.dumps(r, ensure_ascii=False, indent=2) if a.json else _fmt_bt(r))
        return

    if a.backtest_all:
        iv = pd.read_parquet(BASE / "options_data" / "atm_iv_history.parquet",
                             columns=["stock_code"]).stock_code.unique()
        rows, thin = [], []
        cc.prime(raw_dates, verbose=True)
        for code in sorted(iv):
            if code not in px_all.columns:
                continue
            r = backtest(code, px_all[code].dropna(), raw_dates, a.delta,
                         a.step, cost, series(hi_all, code), series(lo_all, code),
                         a.profit_target, a.stop_loss)
            if not r:
                continue
            if r.get("insufficient"):
                thin.append(r)
            else:
                rows.append(r)
        rows.sort(key=lambda r: -(r["avg_ret_on_risk"] or -99))
        OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        print(f"\n=== Iron Condor 回測（delta {a.delta}，每 {a.step} 日開倉，"
              f"滑價 {cost.slippage_per_leg*100:.1f}%/腳）===\n")
        print(f"{'代號':>6} {'筆數':>5} {'勝率':>6} {'勝率95%CI':>14} "
              f"{'平均回報':>9} {'Sharpe':>7} {'最大回撤':>9} {'觸價':>6}")
        for r in rows[:a.limit]:
            ci = r.get("win_rate_ci") or {}
            p = r.get("portfolio") or {}
            ci_txt = f"[{ci['lo']:.0f},{ci['hi']:.0f}]" if ci else "—"
            print(f"{r['stock_code']:>6} {r['n_trades']:>5} {r['win_rate']:>5.0f}% "
                  f"{ci_txt:>14} "
                  f"{(r['avg_ret_on_risk'] or 0):>8.1f}% {(p.get('sharpe') or 0):>7.2f} "
                  f"{(p.get('max_drawdown_pct') or 0):>8.1f}% {r['touch_rate']:>5.0f}%")
        print(f"\n共 {len(rows)} 隻有足夠樣本，{len(thin)} 隻樣本不足。→ {OUT}")
        print(f"\n快取: {cc.stats()}")
        print("\n⚠ 呢個排行榜仍然係**全樣本**，唔可以直接用嚟揀今日落單標的。"
              "\n   要 out-of-sample 結果請跑：python3 walkforward.py")


if __name__ == "__main__":
    main()
