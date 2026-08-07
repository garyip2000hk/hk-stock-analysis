"""IV 貴定平分析器 — 用 HKEX 官方 ATM IV + 我哋自己嘅收市價歷史。

四條數（由粗到精）：

1. HV20  = 過去 20 個交易日對數回報標準差 × √252 × 100
           呢個係「股票實際波到幾多」。
2. IV/HV = 期權市場收嘅價 ÷ 股票實際波幅。
           > 1.3 = 賣方有肉食（貴）; < 0.9 = 買方便宜。
3. IV Rank / Percentile — 同自己一年歷史比：
           Rank = (IV - IV_min) / (IV_max - IV_min) × 100
           Pct  = 過去一年有幾多 % 嘅日子 IV 低過今日
4. Z-score = (IV - mean) / std   （> +2 極貴、< -2 極平）

CLI:
    python3 iv_analyzer.py                 # 全市場排行（貴 → 平）
    python3 iv_analyzer.py --stock 00700   # 單隻詳細
    python3 iv_analyzer.py --json          # 出 JSON 供 API 用
    python3 iv_analyzer.py --cheap         # 只睇最平嘅（買方機會）
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
HISTORY = HERE / "options_data" / "iv_history.parquet"
QUOTES = HERE / "imported" / "quotes.json"
OUT_JSON = HERE / "options_data" / "iv_analysis.json"

TRADING_DAYS = 252
HV_WINDOW = 20
LOOKBACK = 252  # IV rank / percentile 用一年


# ---------------------------------------------------------------- 數據載入

def load_iv() -> pd.DataFrame:
    if not HISTORY.exists():
        raise SystemExit(f"未見 {HISTORY}，先跑 options_scraper.py")
    df = pd.read_parquet(HISTORY)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["stock_code", "date"])


def load_closes() -> dict[str, pd.Series]:
    """由 imported/quotes.json 砌每隻股票嘅收市價序列。"""
    raw = json.loads(QUOTES.read_text())
    quotes = raw.get("quotes", raw)
    rows: dict[str, dict] = {}
    for d, per_stock in quotes.items():
        for code, rec in per_stock.items():
            c = rec.get("close") if isinstance(rec, dict) else rec
            if c:
                rows.setdefault(code, {})[d] = float(c)
    out = {}
    for code, series in rows.items():
        s = pd.Series(series)
        s.index = pd.to_datetime(s.index)
        out[code] = s.sort_index()
    return out


# ---------------------------------------------------------------- 計算

def realized_vol(closes: pd.Series, window: int = HV_WINDOW) -> float | None:
    """年化歷史波幅 (%)。用對數回報，標準做法。"""
    if closes is None or len(closes) < window + 1:
        return None
    px = closes.iloc[-(window + 1):]
    rets = np.log(px / px.shift(1)).dropna()
    if len(rets) < window // 2:
        return None
    return float(rets.std(ddof=1) * math.sqrt(TRADING_DAYS) * 100)


def iv_stats(iv_series: pd.Series) -> dict:
    """IV rank / percentile / z-score，用最近 LOOKBACK 個觀測。"""
    s = iv_series.dropna()
    if len(s) < 2:
        return {}
    s = s.iloc[-LOOKBACK:]
    cur = float(s.iloc[-1])
    lo, hi = float(s.min()), float(s.max())
    rank = (cur - lo) / (hi - lo) * 100 if hi > lo else 50.0
    pct = float((s < cur).mean() * 100)
    mean, std = float(s.mean()), float(s.std(ddof=1) or 0)
    z = (cur - mean) / std if std else 0.0
    return {
        "iv": cur,
        "iv_rank": round(rank, 1),
        "iv_pct": round(pct, 1),
        "iv_mean": round(mean, 1),
        "iv_min": round(lo, 1),
        "iv_max": round(hi, 1),
        "iv_z": round(z, 2),
        "obs": len(s),
    }


def verdict(ratio: float | None, rank: float | None, z: float | None) -> tuple[str, int]:
    """綜合評分：正分＝貴（利賣方），負分＝平（利買方）。"""
    score = 0
    if ratio is not None:
        if ratio >= 1.5:
            score += 3
        elif ratio >= 1.25:
            score += 2
        elif ratio >= 1.05:
            score += 1
        elif ratio <= 0.75:
            score -= 3
        elif ratio <= 0.9:
            score -= 2
        elif ratio < 1.0:
            score -= 1
    if rank is not None:
        if rank >= 80:
            score += 2
        elif rank >= 60:
            score += 1
        elif rank <= 20:
            score -= 2
        elif rank <= 40:
            score -= 1
    if z is not None:
        if z >= 2:
            score += 2
        elif z >= 1:
            score += 1
        elif z <= -2:
            score -= 2
        elif z <= -1:
            score -= 1

    if score >= 5:
        return "極貴・偏賣方", score
    if score >= 3:
        return "貴", score
    if score >= 1:
        return "略貴", score
    if score <= -5:
        return "極平・偏買方", score
    if score <= -3:
        return "平", score
    if score <= -1:
        return "略平", score
    return "合理", score


def analyse(as_of: str | None = None) -> list[dict]:
    iv = load_iv()
    closes = load_closes()

    latest = pd.Timestamp(as_of) if as_of else iv["date"].max()
    today = iv[iv["date"] == latest]

    results = []
    for _, row in today.iterrows():
        code = row["stock_code"]
        hist = iv[(iv["stock_code"] == code) & (iv["date"] <= latest)]
        st = iv_stats(hist.set_index("date")["iv"])
        if not st:
            continue

        ser = hist.set_index("date")["iv"].dropna()
        chg1 = round(float(ser.iloc[-1] - ser.iloc[-2]), 1) if len(ser) >= 2 else None
        chg5 = round(float(ser.iloc[-1] - ser.iloc[-6]), 1) if len(ser) >= 6 else None

        px = closes.get(code)
        if px is not None:
            px = px[px.index <= latest]
        hv20 = realized_vol(px, 20)
        hv60 = realized_vol(px, 60)
        ratio = round(st["iv"] / hv20, 2) if hv20 and hv20 > 0 else None
        v, score = verdict(ratio, st.get("iv_rank"), st.get("iv_z"))

        results.append(
            {
                "date": str(latest.date()),
                "stock_code": code,
                "hkats": row["hkats"],
                "name": row["name"],
                "close": row["close"],
                **st,
                "iv_chg_1d": chg1,
                "iv_chg_5d": chg5,
                "hv20": round(hv20, 1) if hv20 else None,
                "hv60": round(hv60, 1) if hv60 else None,
                "iv_hv": ratio,
                "premium_pts": round(st["iv"] - hv20, 1) if hv20 else None,
                "volume": int(row["volume"] or 0),
                "oi": int(row["oi"] or 0),
                "pcr_vol": None if pd.isna(row["pcr_vol"]) else float(row["pcr_vol"]),
                "pcr_oi": None if pd.isna(row["pcr_oi"]) else float(row["pcr_oi"]),
                "verdict": v,
                "score": score,
            }
        )

    results.sort(key=lambda r: (-r["score"], -(r["iv_hv"] or 0)))
    return results


# ---------------------------------------------------------------- 輸出

def fmt_table(rows: list[dict], limit: int = 25) -> str:
    head = (
        f"{'代號':>6} {'名稱':<22} {'IV':>5} {'HV20':>6} {'IV/HV':>6} "
        f"{'Rank':>5} {'Z':>6} {'成交':>9}  評價"
    )
    lines = [head, "-" * len(head)]
    for r in rows[:limit]:
        lines.append(
            f"{r['stock_code']:>6} {r['name'][:22]:<22} {r['iv']:>5.0f} "
            f"{(r['hv20'] or 0):>6.1f} {(r['iv_hv'] or 0):>6.2f} "
            f"{r['iv_rank']:>5.0f} {r['iv_z']:>6.2f} {r['volume']:>9,}  {r['verdict']}"
        )
    return "\n".join(lines)


def detail(rows: list[dict], code: str) -> str:
    r = next((x for x in rows if x["stock_code"] == code or x["hkats"] == code.upper()), None)
    if not r:
        return f"{code} 唔喺期權標的名單（HKEX 只有約 140 隻有股票期權）。"
    exp_daily = r["iv"] / math.sqrt(TRADING_DAYS)
    exp_month = r["iv"] / math.sqrt(12)
    return "\n".join(
        [
            f"{r['stock_code']} {r['name']}    收市 {r['close']}    ({r['date']})",
            "",
            f"  ATM IV          {r['iv']:.0f}%",
            f"  HV20 / HV60     {r['hv20']}% / {r['hv60']}%",
            f"  IV / HV20       {r['iv_hv']}      （溢價 {r['premium_pts']} 點）",
            f"  IV Rank         {r['iv_rank']:.0f}   （一年區間 {r['iv_min']:.0f}%–{r['iv_max']:.0f}%）",
            f"  IV Percentile   {r['iv_pct']:.0f}",
            f"  Z-score         {r['iv_z']:+.2f}   （均值 {r['iv_mean']:.0f}%, {r['obs']} 個觀測）",
            f"  IV 變動         1 日 {r['iv_chg_1d']:+} 點    5 日 {r['iv_chg_5d']:+} 點",
            f"  Put/Call 成交   {r['pcr_vol']}    未平倉 {r['pcr_oi']}",
            f"  成交 / 未平倉   {r['volume']:,} / {r['oi']:,}",
            "",
            f"  市場預期波動    日 ±{exp_daily:.2f}%   月 ±{exp_month:.1f}%",
            "",
            f"  ▶ 評價：{r['verdict']}  (score {r['score']:+d})",
        ]
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock")
    ap.add_argument("--as-of")
    ap.add_argument("--cheap", action="store_true", help="只出最平嘅（買方機會）")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = analyse(args.as_of)

    if args.json:
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        print(json.dumps(rows, ensure_ascii=False, default=str))
        return

    if args.stock:
        print(detail(rows, args.stock))
        return

    if args.cheap:
        cheap = sorted(rows, key=lambda r: (r["score"], r["iv_hv"] or 9))
        print(f"=== IV 最平 {args.limit} 隻（{rows[0]['date']}）===\n")
        print(fmt_table(cheap, args.limit))
        return

    print(f"=== IV 貴 → 平 排行（{rows[0]['date']}，{len(rows)} 隻）===\n")
    print(fmt_table(rows, args.limit))
    print()
    cheap = sorted(rows, key=lambda r: (r["score"], r["iv_hv"] or 9))
    print("=== 最平 10 隻（買方機會）===\n")
    print(fmt_table(cheap, 10))


if __name__ == "__main__":
    main()
