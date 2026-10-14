"""walkforward.py — 走前式（walk-forward）樣本外驗證。

## 為咩要有呢個檔案

審查報告 A6 指出：`vol_system.py` 用 `condor_backtest.json` 嘅
`MIN_BT_TRADES=20` / `MIN_BT_WIN=70` / `MIN_BT_RET=15` 去揀今日落單標的，
但 `condor_backtest.json` 係**全樣本**回測結果 —— 即係「用 2026 年 8 月
已經知道嘅結果，去決定 2024 年 3 月應該賣邊隻股嘅期權」。

呢個係典型 data snooping：喺 60 隻股度揀「歷史勝率 ≥ 70%」嘅，
就算全部股票真實勝率都係 50%，純靠運氣都會有一批「達標」。
用呢個排行榜報出嘅勝率，必然遠高於實際落單會拿到嘅勝率。

## 呢度點做

利用一個關鍵事實：**每張 condor 嘅損益係獨立於「揀唔揀佢」嘅決策**。
所以唔需要重複跑回測 —— 跑一次拿齊全部交易，再按時間切：

    對每個月初 t：
        train = 所有 **喺 t 之前已經平倉** 嘅交易
        selected(t) = train 度達標嘅股票（同 vol_system 一樣嘅門檻）
        oos(t) = selected(t) 呢批股票喺 [t, t+1 個月) **開倉** 嘅交易

    OOS 總結 = Σ oos(t)

嚴格用「已平倉」而唔係「已開倉」做 train 邊界：決策日只可以見到
已經有結果嘅交易。仲未平倉嘅倉位對當時嘅你係未知結果。

輸出 `walkforward.json`，包括：
  - `oos`：樣本外整體勝率／回報／組合 Sharpe（**呢個才係可信嘅數字**）
  - `full_sample`：全樣本結果，同 OOS 並排睇差幾多（過擬合幅度）
  - `windows`：逐月揀咗邊幾隻、當月 OOS 表現
  - `latest`：最新一期揀出嘅股票 → `vol_system.py` 應該讀呢個

判斷標準（審查報告 F）：**OOS 勝率 ≥ 60% 且 3% 滑價下仍正期望**。

CLI:
    python3 walkforward.py                      # 3% 滑價，全市場
    python3 walkforward.py --slippage 0.05
    python3 walkforward.py --codes 00700 09988
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import chain_cache as cc
import condor_engine as ce
import costs
import portfolio as pf

BASE = Path(__file__).parent
OUT = BASE / "walkforward.json"

# 同 vol_system.py 一樣嘅選股門檻（唯一分別：只餵之前嘅資料）
MIN_BT_TRADES = 20
MIN_BT_WIN = 70.0
MIN_BT_RET = 15.0

MIN_TRAIN_MONTHS = 12   # 至少要幾個月訓練期先開始評 OOS
REBALANCE = "MS"        # 每月初重新揀


def _month_starts(lo: pd.Timestamp, hi: pd.Timestamp) -> list[pd.Timestamp]:
    return list(pd.date_range(lo.normalize().replace(day=1), hi, freq=REBALANCE))


def collect_trades(codes: list[str], cost: costs.CostModel,
                   step: int = 5, verbose: bool = True) -> pd.DataFrame:
    """跑一次全樣本回測，攞齊每隻股每張倉。"""
    import vrp_engine as ve

    ohlc = ve.load_ohlc()
    px_all, hi_all, lo_all = ohlc["close"], ohlc.get("high"), ohlc.get("low")
    dates = ce.oc_dates()
    if not dates or px_all.empty:
        return pd.DataFrame()

    cc.prime(dates, verbose=verbose)
    rows = []
    for i, code in enumerate(codes, 1):
        if code not in px_all.columns:
            continue

        def ser(df):
            return df[code].dropna() if (df is not None
                                         and code in df.columns) else None

        r = ce.backtest(code, px_all[code].dropna(), dates, cost=cost, step=step,
                        hi=ser(hi_all), lo=ser(lo_all))
        if verbose and i % 10 == 0:
            print(f"  回測 {i}/{len(codes)} …", flush=True)
        if not r or r.get("insufficient"):
            continue
        for t in r["trades"]:
            rows.append({"stock_code": code, **t})

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["open_dt"] = pd.to_datetime(df["open"])
    df["exit_dt"] = pd.to_datetime(df["exit_date"])
    return df.sort_values("open_dt").reset_index(drop=True)


def _select(train: pd.DataFrame) -> list[str]:
    """訓練期內達標嘅股票（同 vol_system 門檻一致）。"""
    if train.empty:
        return []
    g = train.groupby("stock_code").agg(
        n=("pnl", "size"),
        win=("pnl", lambda s: (s > 0).mean() * 100),
        ret=("ret_on_risk", "mean"))
    ok = g[(g.n >= MIN_BT_TRADES) & (g.win >= MIN_BT_WIN) & (g.ret >= MIN_BT_RET)]
    return sorted(ok.index.tolist())


def _summary(t: pd.DataFrame, label: str) -> dict:
    if t.empty:
        return {"label": label, "n_trades": 0}
    rr = t.ret_on_risk.dropna()
    eq = pf.equity_curve(t.to_dict("records"))
    ci = pf.block_bootstrap_ci(t.pnl, block=max(1, int(t.days_held.median() or 21)))
    return {
        "label": label,
        "n_trades": int(len(t)),
        "n_codes": int(t.stock_code.nunique()),
        "win_rate": round(float((t.pnl > 0).mean() * 100), 1),
        "win_rate_ci": ci,
        "avg_ret_on_risk": round(float(rr.mean()), 1) if len(rr) else None,
        "median_ret": round(float(rr.median()), 1) if len(rr) else None,
        "worst": round(float(t.pnl.min()), 3),
        "total_pnl_units": round(float((t.pnl / t.max_loss).sum()), 2),
        "exit_reasons": t.exit_reason.value_counts().to_dict(),
        "portfolio": pf.metrics(eq),
    }


def run(codes: list[str] | None = None, slippage: float = 0.03,
        commission: float = 0.0, step: int = 5,
        verbose: bool = True) -> dict | None:
    cost = costs.CostModel(slippage_per_leg=slippage,
                           commission_per_leg=commission)

    if codes is None:
        p = BASE / "options_data" / "atm_iv_history.parquet"
        if not p.exists():
            print("冇 options_data/atm_iv_history.parquet —— 先跑 atm_history.py")
            return None
        codes = sorted(pd.read_parquet(p, columns=["stock_code"])
                       .stock_code.dropna().unique())
    codes = [str(c).zfill(5) for c in codes]

    df = collect_trades(codes, cost, step, verbose)
    if df.empty:
        print("冇任何交易，檢查 options_data/raw 同 imported/quotes.json")
        return None

    starts = _month_starts(df.open_dt.min(), df.open_dt.max())
    if len(starts) <= MIN_TRAIN_MONTHS:
        print(f"樣本只有 {len(starts)} 個月，唔夠做 walk-forward"
              f"（要 > {MIN_TRAIN_MONTHS}）")
        return None

    windows, oos_parts = [], []
    for k in range(MIN_TRAIN_MONTHS, len(starts)):
        t0 = starts[k]
        t1 = starts[k + 1] if k + 1 < len(starts) else df.open_dt.max() + pd.Timedelta(days=1)

        train = df[df.exit_dt < t0]                # 只用已平倉嘅
        sel = _select(train)
        oos = df[(df.open_dt >= t0) & (df.open_dt < t1) & df.stock_code.isin(sel)]
        if len(oos):
            oos_parts.append(oos)

        windows.append({
            "decision_date": str(t0.date()),
            "train_trades": int(len(train)),
            "train_codes": int(train.stock_code.nunique()) if len(train) else 0,
            "selected": sel,
            "n_selected": len(sel),
            "oos_trades": int(len(oos)),
            "oos_win_rate": round(float((oos.pnl > 0).mean() * 100), 1)
                            if len(oos) else None,
            "oos_avg_ret": round(float(oos.ret_on_risk.mean()), 1)
                            if len(oos) else None,
        })

    oos_all = (pd.concat(oos_parts).drop_duplicates(subset=["stock_code", "open"])
               if oos_parts else pd.DataFrame())

    # 全樣本「達標股票」= vol_system 現時實際會揀嘅（用埋未來資料）
    full_sel = _select(df)
    full = df[df.stock_code.isin(full_sel)]

    res = {
        "generated": str(date.today()),
        "cost": {"slippage_per_leg": slippage, "commission_per_leg": commission},
        "filters": {"min_trades": MIN_BT_TRADES, "min_win": MIN_BT_WIN,
                    "min_ret": MIN_BT_RET, "min_train_months": MIN_TRAIN_MONTHS},
        "universe_size": len(codes),
        "oos": _summary(oos_all, "樣本外（walk-forward）"),
        "full_sample": _summary(full, "全樣本（有前視偏差，只作對比）"),
        "all_trades": _summary(df, "全部股票全部交易（無選股）"),
        "windows": windows,
        "latest": {"decision_date": windows[-1]["decision_date"],
                   "selected": windows[-1]["selected"]},
    }

    oos_w = res["oos"].get("win_rate")
    res["verdict"] = {
        "oos_win_ge_60": (oos_w is not None and oos_w >= 60),
        "oos_positive_expectancy": (res["oos"].get("avg_ret_on_risk") or -1) > 0,
        "overfit_gap_pct": (round(res["full_sample"]["win_rate"] - oos_w, 1)
                            if oos_w is not None
                            and res["full_sample"].get("win_rate") else None),
    }
    res["verdict"]["pass"] = (res["verdict"]["oos_win_ge_60"]
                              and res["verdict"]["oos_positive_expectancy"])
    return res


def fmt(r: dict) -> str:
    L = [f"=== Walk-forward 樣本外驗證"
         f"（滑價 {r['cost']['slippage_per_leg']*100:.1f}%/腳）===", ""]

    for key in ("all_trades", "full_sample", "oos"):
        s = r[key]
        if not s.get("n_trades"):
            L += [f"── {s['label']} ──", "  冇交易", ""]
            continue
        p = s.get("portfolio") or {}
        ci = s.get("win_rate_ci") or {}
        L += [
            f"── {s['label']} ──",
            f"  交易筆數 / 股票數   {s['n_trades']:>6} / {s['n_codes']}",
            f"  勝率                {s['win_rate']:>6.1f}%"
            + (f"   95% CI [{ci['lo']:.0f}%, {ci['hi']:.0f}%]" if ci else ""),
            f"  平均 / 中位風險回報 {(s['avg_ret_on_risk'] or 0):>6.1f}% / "
            f"{(s['median_ret'] or 0):.1f}%",
            f"  最壞單筆            {s['worst']:>6.2f}",
            f"  Sharpe（真・年化）  {(p.get('sharpe') or 0):>6.2f}",
            f"  最大回撤            {(p.get('max_drawdown_pct') or 0):>6.2f}%",
            f"  平倉原因            {s['exit_reasons']}",
            "",
        ]

    v = r["verdict"]
    gap = v.get("overfit_gap_pct")
    L += ["── 判斷 ──",
          f"  OOS 勝率 ≥ 60%           {'✓' if v['oos_win_ge_60'] else '✗'}",
          f"  OOS 正期望               {'✓' if v['oos_positive_expectancy'] else '✗'}"]
    if gap is not None:
        L.append(f"  過擬合幅度               全樣本勝率高過 OOS {gap:+.1f} 個百分點")
        if gap > 10:
            L.append("     ⚠ 差距 > 10 點 → 全樣本排行榜嘅勝率唔可以信")
    L += ["",
          f"  ▶ {'✓ 通過樣本外檢驗，可以入 Stage 1' if v['pass'] else '✗ 未通過樣本外檢驗 —— 唔應該實盤落單'}",
          "",
          f"最新一期（{r['latest']['decision_date']}）揀出："
          + ("、".join(r["latest"]["selected"]) or "冇股票達標"),
          "",
          "── 逐月窗口（尾 12 期）──",
          f"{'決策日':>12} {'訓練筆數':>9} {'揀中':>5} {'OOS筆數':>8} {'OOS勝率':>8} {'OOS回報':>8}"]
    for w in r["windows"][-12:]:
        wr = w["oos_win_rate"]
        ar = w["oos_avg_ret"]
        wr_s = f"{wr:.0f}%" if wr is not None else "—"
        ar_s = f"{ar:.1f}%" if ar is not None else "—"
        L.append(f"{w['decision_date']:>12} {w['train_trades']:>9} "
                 f"{w['n_selected']:>5} {w['oos_trades']:>8} "
                 f"{wr_s:>8} {ar_s:>8}")
    return "\n".join(L)


def load() -> dict | None:
    """其他模組讀最新一期選股。"""
    if not OUT.exists():
        return None
    try:
        return json.loads(OUT.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward 樣本外驗證")
    ap.add_argument("--codes", nargs="*", help="只跑指定股票（預設全市場）")
    ap.add_argument("--slippage", type=float, default=ce.DEFAULT_SLIPPAGE)
    ap.add_argument("--commission", type=float, default=0.0)
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    r = run(a.codes or None, a.slippage, a.commission, a.step, not a.quiet)
    if not r:
        return
    OUT.write_text(json.dumps(r, ensure_ascii=False, indent=2))
    print(json.dumps(r, ensure_ascii=False, indent=2) if a.json else fmt(r))
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
