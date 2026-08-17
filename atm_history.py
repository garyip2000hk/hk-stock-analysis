"""atm_history.py — 由 raw 報告重建乾淨嘅歷史 ATM IV（按到期月）。

為咩要有呢個模組
----------------
`options_data/iv_history.parquet`（options_scraper 出）係 HKEX 報告嘅
class summary 一行 IV。實測發現部分股票嘅 summary IV 會爆到 100%+
（例如 09618 顯示 116%、00981 顯示 121%），但由逐個行使價拆出嘅真實
ATM IV 只係 35% / 57%。原因係 summary 嗰行會被極度價外、幾乎冇成交嘅
合約污染。用嚟做波幅溢價判斷會完全錯。

呢個模組讀返 raw 報告（options_chain.parse_chains），對每隻股票、每個
到期月，用「最接近平價嘅 call + put IV 平均」重建 ATM IV，再存做
`options_data/atm_iv_history.parquet`。

輸出每行 = 一日 × 一隻股票 × 一個到期月：
    date, stock_code, name, close, expiry, dte, atm_iv, atm_strike,
    call_iv, put_iv, skew_25d, volume, oi

CLI:
    python3 atm_history.py --build            # 全量重建（~200 日，約 5 分鐘）
    python3 atm_history.py --build --since 2026-06-01
    python3 atm_history.py --stock 00700      # 睇單一股票歷史
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import options_chain as oc

BASE = Path(__file__).parent
RAW_DIR = BASE / "options_data" / "raw"
OUT = BASE / "options_data" / "atm_iv_history.parquet"

MIN_OI = 5


def _raw_dates() -> list[date]:
    out = []
    for f in sorted(RAW_DIR.glob("dqe*.txt.gz")):
        try:
            out.append(datetime.strptime(f.name[3:9], "%y%m%d").date())
        except ValueError:
            continue
    return out


def _atm_iv(g: pd.DataFrame, close: float) -> dict | None:
    """由一個到期月嘅鏈計 ATM IV：最近平價嘅 C/P IV 平均。"""
    live = g[(g.iv.notna()) & (g.iv > 0) & (g.iv < 300)]
    if live.empty:
        return None

    traded = live[(live.oi.fillna(0) >= MIN_OI) | (live.volume.fillna(0) > 0)]
    if not traded.empty:
        live = traded

    live = live.assign(dist=(live.strike - close).abs())
    calls = live[live.type == "C"].nsmallest(1, "dist")
    puts = live[live.type == "P"].nsmallest(1, "dist")

    c_iv = float(calls.iv.iloc[0]) if not calls.empty else None
    p_iv = float(puts.iv.iloc[0]) if not puts.empty else None
    ivs = [v for v in (c_iv, p_iv) if v is not None]
    if not ivs:
        return None

    strike = None
    for cand in (calls, puts):
        if not cand.empty:
            strike = float(cand.strike.iloc[0])
            break

    otm_p = live[(live.type == "P") & (live.moneyness.between(0.85, 0.95))]
    otm_c = live[(live.type == "C") & (live.moneyness.between(1.05, 1.15))]
    skew = None
    if not otm_p.empty and not otm_c.empty:
        skew = round(float(otm_p.iv.mean() - otm_c.iv.mean()), 2)

    return {
        "atm_iv": round(sum(ivs) / len(ivs), 2),
        "atm_strike": strike,
        "call_iv": c_iv,
        "put_iv": p_iv,
        "skew_25d": skew,
        "volume": float(g.volume.fillna(0).sum()),
        "oi": float(g.oi.fillna(0).sum()),
    }


def build_day(d: date) -> pd.DataFrame:
    df = oc.parse_chains(d)
    if df.empty:
        return pd.DataFrame()
    df = df[df.stock_code.notna() & df.close.notna() & (df.close > 0)]
    if df.empty:
        return pd.DataFrame()

    rows: list[dict] = []
    for (code, name, close, exp, dte), g in df.groupby(
        ["stock_code", "name", "close", "expiry", "dte"], sort=False
    ):
        if dte < 1:
            continue
        m = _atm_iv(g, float(close))
        if not m:
            continue
        rows.append(
            {
                "date": d,
                "stock_code": code,
                "name": name,
                "close": float(close),
                "expiry": exp,
                "dte": int(dte),
                **m,
            }
        )
    return pd.DataFrame(rows)


def build(since: date | None = None, verbose: bool = True) -> pd.DataFrame:
    dates = [d for d in _raw_dates() if since is None or d >= since]
    frames: list[pd.DataFrame] = []
    for i, d in enumerate(dates, 1):
        try:
            part = build_day(d)
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"  {d} 失敗: {exc}")
            continue
        if not part.empty:
            frames.append(part)
        if verbose and (i % 20 == 0 or i == len(dates)):
            print(f"  {i}/{len(dates)}  {d}  累計 {sum(len(f) for f in frames):,} 行", flush=True)

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)

    if OUT.exists() and since is not None:
        old = pd.read_parquet(OUT)
        old = old[~old.date.isin(set(out.date))]
        out = pd.concat([old, out], ignore_index=True)

    out = out.sort_values(["date", "stock_code", "expiry"]).reset_index(drop=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT, index=False)
    return out


def load() -> pd.DataFrame:
    if not OUT.exists():
        return pd.DataFrame()
    return pd.read_parquet(OUT)


def front_iv(min_dte: int = 15, max_dte: int = 75) -> pd.DataFrame:
    """每日每股一行：揀 DTE 落範圍內、未平倉最多嘅月做代表 IV。"""
    df = load()
    if df.empty:
        return df
    cand = df[df.dte.between(min_dte, max_dte)]
    fallback = df[df.dte >= 7]
    cand = pd.concat([cand, fallback[~fallback.set_index(["date", "stock_code"]).index.isin(
        cand.set_index(["date", "stock_code"]).index)]], ignore_index=True)
    return (cand.sort_values(["date", "stock_code", "oi"])
            .groupby(["date", "stock_code"], as_index=False).last())


def main() -> None:
    ap = argparse.ArgumentParser(description="重建乾淨歷史 ATM IV")
    ap.add_argument("--build", action="store_true", help="重建 parquet")
    ap.add_argument("--since", help="只重建由此日起 YYYY-MM-DD")
    ap.add_argument("--stock", help="睇單一股票歷史")
    a = ap.parse_args()

    if a.build:
        since = date.fromisoformat(a.since) if a.since else None
        print(f"重建 ATM IV 歷史{'（增量 ' + a.since + ' 起）' if since else '（全量）'}…")
        out = build(since)
        if out.empty:
            print("冇數據")
            return
        print(f"\n✓ {OUT}")
        print(f"  {len(out):,} 行  {out.date.nunique()} 日  {out.stock_code.nunique()} 隻")
        print(f"  {out.date.min()} → {out.date.max()}")
        return

    df = load()
    if df.empty:
        print("未建歷史，先跑：python3 atm_history.py --build")
        return

    if a.stock:
        code = a.stock.zfill(5)
        sub = front_iv()
        sub = sub[sub.stock_code == code].sort_values("date")
        if sub.empty:
            print(f"{code} 冇數據")
            return
        print(f"{code} {sub.name.iloc[-1]}  {len(sub)} 日\n")
        print(f"{'日期':>12} {'收市':>9} {'DTE':>4} {'ATM IV':>7} {'Skew':>6}")
        for r in sub.tail(30).to_dict("records"):
            sk = f"{r['skew_25d']:>6.1f}" if r["skew_25d"] is not None and not pd.isna(r["skew_25d"]) else "     —"
            print(f"{str(r['date']):>12} {r['close']:>9.2f} {r['dte']:>4} {r['atm_iv']:>7.1f} {sk}")
        return

    print(f"{OUT}\n  {len(df):,} 行  {df.date.nunique()} 日  {df.stock_code.nunique()} 隻")
    print(f"  {df.date.min()} → {df.date.max()}")


if __name__ == "__main__":
    main()
