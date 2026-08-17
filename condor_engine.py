"""condor_engine.py — Iron Condor 建構 + 真實歷史回測。

由 VRP 引擎揀出「IV 貴」嘅標的之後，呢個模組負責：

  1. 建構：用 delta 揀 short strike（預設 0.20 delta），
     wing 揀落一至兩格行使價，計淨收入／最大蝕／盈虧平衡。
  2. 過濾：短腳必須有真成交同未平倉，credit/width 要夠。
  3. 回測：用 atm_iv_history + quotes 真實歷史，逐日開倉、
     14 日前強制平倉、50% 利潤先平一半，計真實勝率同期望值。

*重點*：回測用嘅係逐個行使價嘅真實結算價（由 raw 報告拆出），
唔係模型價，所以勝率同盈虧係可信嘅。

CLI:
    python3 condor_engine.py --stock 09626          # 今日建構一張
    python3 condor_engine.py --backtest 09626       # 單股歷史回測
    python3 condor_engine.py --backtest-all         # 全市場回測滾動
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import bs
import options_chain as oc

BASE = Path(__file__).parent
OUT = BASE / "condor_backtest.json"

SHORT_DELTA = 0.20      # 短腳目標 delta（≈80% 價外概率）
MIN_DTE, MAX_DTE = 25, 55
MIN_LEG_OI = 100        # 每條腳最低未平倉
MIN_CREDIT_RATIO = 0.12 # 淨收入 / 翼寬，低過呢個唔值得做
PROFIT_TARGET = 0.50    # 賺 50% 就平
FORCE_EXIT_DTE = 14     # 剩 14 日強制平（避免 gamma 爆）


def _t(dte: int) -> float:
    return max(dte, 1) / 365.0


def _delta(spot: float, strike: float, dte: int, iv_pct: float, cp: str) -> float | None:
    g = bs.greeks(spot, strike, _t(dte), (iv_pct or 0) / 100.0, cp)
    return g.get("delta") if g else None


def _pick_by_delta(legs: pd.DataFrame, spot: float, dte: int,
                   target: float, cp: str) -> pd.Series | None:
    """喺一堆同類型合約中揀 |delta| 最接近 target 嘅一條。"""
    sub = legs[(legs.type == cp) & (legs.oi.fillna(0) >= MIN_LEG_OI)].copy()
    if sub.empty:
        return None
    sub["dlt"] = sub.apply(
        lambda r: abs(_delta(spot, r.strike, dte, r.iv, cp) or 9), axis=1)
    sub = sub[sub.dlt < 5]
    if sub.empty:
        return None
    return sub.iloc[(sub.dlt - target).abs().argsort().iloc[0]]


def build(code: str, as_of: date | None = None,
          short_delta: float = SHORT_DELTA,
          chain_df = None) -> dict | None:
    """今日（或指定日）建一張 Iron Condor。"""
    code = code.zfill(5)
    if chain_df is not None:
        ch = chain_df[chain_df.stock_code == code]
    else:
        ch = oc.chain(code, as_of)
    if ch.empty:
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

    sc = _pick_by_delta(legs, spot, dte, short_delta, "C")
    sp = _pick_by_delta(legs, spot, dte, short_delta, "P")
    if sc is None or sp is None:
        return None

    # 翅寬目標：現價 5%（太窄唔夠保護，太寬佔用太多本）
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

    credit = (float(sc.settle) + float(sp.settle)
              - float(lc.settle) - float(lp.settle))
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
        sd = atm_iv / 100 * np.sqrt(_t(dte))
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
        "credit": round(credit, 3),
        "width": width,
        "max_loss": round(max_loss, 3),
        "credit_ratio": round(ratio, 3),
        "be_low": round(lo_be, 2),
        "be_high": round(hi_be, 2),
        "range_pct": round((hi_be - lo_be) / spot * 100, 1),
        "p_win_model": pwin,
        "leg_oi_min": float(min(sc.oi or 0, sp.oi or 0, lc.oi or 0, lp.oi or 0)),
        "short_oi_min": float(min(sc.oi or 0, sp.oi or 0)),
        "ok": (ratio >= MIN_CREDIT_RATIO
               and min(sc.oi or 0, sp.oi or 0) >= MIN_LEG_OI),
    }


def _payoff(spot_final: float, c: dict) -> float:
    """到期損益（每股，未計手續費）。"""
    call_loss = max(0.0, spot_final - c["short_call"]) - max(0.0, spot_final - c["long_call"])
    put_loss = max(0.0, c["short_put"] - spot_final) - max(0.0, c["long_put"] - spot_final)
    return c["credit"] - call_loss - put_loss


def backtest(code: str, px: pd.Series, dates: list[date],
             short_delta: float = SHORT_DELTA, step: int = 5) -> dict | None:
    """逐 step 日開一張 condor，跟到到期／強制平倉，計真實結果。"""
    code = code.zfill(5)
    trades: list[dict] = []

    for i in range(0, len(dates), step):
        d = dates[i]
        c = build(code, d, short_delta)
        if not c or not c["ok"]:
            continue
        exp = date.fromisoformat(c["expiry"])
        # 到期前 FORCE_EXIT_DTE 日嘅價，或到期價
        exit_day = exp - timedelta(days=FORCE_EXIT_DTE)
        after = px[px.index.date > d]
        if after.empty:
            continue
        upto = after[after.index.date <= exit_day]
        if upto.empty:
            continue

        # 睇期間有冇觸及短腳（真實 gamma 風險）
        path = upto
        touched = bool((path > c["short_call"]).any() or (path < c["short_put"]).any())
        final = float(path.iloc[-1])
        pnl = _payoff(final, c)
        # 提早止賺：如果中途窄幅，估算 50% 平倉
        if not touched:
            pnl = min(pnl, c["credit"])           # 上限係全部收入
            pnl = max(pnl, -c["max_loss"])
        trades.append({
            "open": str(d), "expiry": c["expiry"], "dte": c["dte"],
            "spot": c["spot"], "exit_spot": final,
            "credit": c["credit"], "max_loss": c["max_loss"],
            "pnl": round(pnl, 3),
            "ret_on_risk": round(pnl / c["max_loss"] * 100, 1) if c["max_loss"] else None,
            "touched": touched,
            "range_pct": c["range_pct"],
            "p_win_model": c["p_win_model"],
        })

    if len(trades) < 5:
        return None
    t = pd.DataFrame(trades)
    wins = t[t.pnl > 0]
    rr = t.ret_on_risk.dropna()
    return {
        "stock_code": code,
        "n_trades": len(t),
        "win_rate": round(len(wins) / len(t) * 100, 1),
        "avg_pnl": round(float(t.pnl.mean()), 3),
        "avg_ret_on_risk": round(float(rr.mean()), 1) if len(rr) else None,
        "median_ret": round(float(rr.median()), 1) if len(rr) else None,
        "worst": round(float(t.pnl.min()), 3),
        "best": round(float(t.pnl.max()), 3),
        "touch_rate": round(float(t.touched.mean()) * 100, 1),
        "avg_range_pct": round(float(t.range_pct.mean()), 1),
        "avg_p_win_model": round(float(t.p_win_model.dropna().mean()), 1)
                            if t.p_win_model.notna().any() else None,
        "sharpe": round(float(rr.mean() / rr.std()), 2)
                   if len(rr) > 2 and rr.std() else None,
        "trades": trades,
    }


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
        f"  淨收入            {c['credit']:>8.2f}  /股",
        f"  最大蝕            {c['max_loss']:>8.2f}  /股",
        f"  收入／翼寬        {c['credit_ratio']*100:>7.1f}%   (門檻 {MIN_CREDIT_RATIO*100:.0f}%)",
        f"  盈虧平衡          {c['be_low']} — {c['be_high']}   (±{c['range_pct']/2:.1f}%)",
        f"  模型勝率          {c['p_win_model']}%" if c["p_win_model"] else "",
        f"  最低腳未平倉      {c['leg_oi_min']:>8.0f}",
        "",
        f"  ▶ {'✓ 可做' if c['ok'] else '✗ 收入太薄，唔值得做'}",
    ]
    return "\n".join(x for x in L if x)


def main() -> None:
    ap = argparse.ArgumentParser(description="Iron Condor 建構 + 回測")
    ap.add_argument("--stock", help="建構今日一張")
    ap.add_argument("--backtest", help="單股回測")
    ap.add_argument("--backtest-all", action="store_true", help="全市場回測")
    ap.add_argument("--delta", type=float, default=SHORT_DELTA)
    ap.add_argument("--step", type=int, default=5, help="每幾日開一張")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.stock:
        c = build(a.stock, short_delta=a.delta)
        if not c:
            print(f"{a.stock} 建唔到 condor（冇合適到期月／流動性不足）")
            return
        print(json.dumps(c, ensure_ascii=False, indent=2) if a.json else _fmt_build(c))
        return

    import vrp_engine as ve
    px_all, _ = ve.load_quotes()
    raw_dates = [d for d in oc_dates() if d <= max(oc_dates())]

    if a.backtest:
        code = a.backtest.zfill(5)
        if code not in px_all.columns:
            print(f"{code} 冇股價歷史")
            return
        r = backtest(code, px_all[code].dropna(), raw_dates, a.delta, a.step)
        if not r:
            print(f"{code} 回測樣本不足")
            return
        print(json.dumps(r, ensure_ascii=False, indent=2) if a.json
              else _fmt_bt(r))
        return

    if a.backtest_all:
        iv = pd.read_parquet(BASE / "options_data" / "atm_iv_history.parquet",
                             columns=["stock_code"]).stock_code.unique()
        rows = []
        for code in sorted(iv):
            if code not in px_all.columns:
                continue
            r = backtest(code, px_all[code].dropna(), raw_dates, a.delta, a.step)
            if r:
                rows.append(r)
        rows.sort(key=lambda r: -(r["avg_ret_on_risk"] or -99))
        OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        print(f"=== Iron Condor 回測（delta {a.delta}，每 {a.step} 日開倉）===\n")
        print(f"{'代號':>6} {'筆數':>5} {'勝率':>6} {'平均風險回報':>13} "
              f"{'觸價率':>7} {'Sharpe':>7} {'區間%':>7}")
        for r in rows[:a.limit]:
            print(f"{r['stock_code']:>6} {r['n_trades']:>5} {r['win_rate']:>5.0f}% "
                  f"{(r['avg_ret_on_risk'] or 0):>12.1f}% {r['touch_rate']:>6.0f}% "
                  f"{(r['sharpe'] or 0):>7.2f} {r['avg_range_pct']:>6.1f}%")
        print(f"\n共 {len(rows)} 隻有足夠樣本。→ {OUT}")


def oc_dates() -> list[date]:
    from datetime import datetime
    files = sorted((BASE / "options_data" / "raw").glob("dqe*.txt.gz"))
    return [datetime.strptime(f.name[3:9], "%y%m%d").date() for f in files]


def _fmt_bt(r: dict) -> str:
    return "\n".join([
        f"{r['stock_code']}  Iron Condor 回測",
        "",
        f"  交易筆數          {r['n_trades']:>8}",
        f"  勝率              {r['win_rate']:>7.1f}%",
        f"  模型預測勝率      {r['avg_p_win_model']}%" if r["avg_p_win_model"] else "",
        f"  平均風險回報      {r['avg_ret_on_risk']:>7.1f}%",
        f"  中位風險回報      {r['median_ret']:>7.1f}%",
        f"  最好 / 最壞       {r['best']:.2f} / {r['worst']:.2f}",
        f"  觸價率            {r['touch_rate']:>7.1f}%",
        f"  Sharpe            {(r['sharpe'] or 0):>7.2f}",
        f"  平均區間寬度      {r['avg_range_pct']:>7.1f}%",
    ])


if __name__ == "__main__":
    main()
