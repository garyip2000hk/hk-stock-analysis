"""vol_surface.py — 波幅曲面：期限結構（term structure）+ 偏斜（skew）。

由 `options_chain` 拆出嘅逐個行使價 IV，重建每隻股票嘅波幅曲面：

  期限結構  近月 IV vs 遠月 IV
      Contango       遠月 IV > 近月 IV（正常）→ 賣遠月／買近月
      Backwardation  近月 IV > 遠月 IV（近期有事）→ 賣近月／買遠月（日曆價差）
  前向波幅  由兩個月嘅 IV 反推出「中間段」市場定價嘅波幅
  前向係數  forward vol ÷ 近月 IV，< 0.85 = 遠月被過度定價（日曆價差機會）
  偏斜      價外 put IV − 價外 call IV，正數 = 跌方保險貴（正常）

前後月配對用「流動性最好嘅可交易月」，唔係盲揀第一個月 —— 因為即期
到期（DTE < 10）嘅合約成交極少，IV 會失真。

CLI:
    python3 vol_surface.py 00700              # 單一股票完整曲面
    python3 vol_surface.py --scan             # 全市場期限結構排行
    python3 vol_surface.py --scan --json
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path

import pandas as pd

import options_chain as oc

BASE = Path(__file__).parent

MIN_DTE_FRONT = 12
MAX_DTE_FRONT = 45
MIN_DTE_BACK = 46
MAX_DTE_BACK = 150
MIN_OI = 5


def _atm_metrics(g: pd.DataFrame, close: float) -> dict | None:
    live = g[(g.iv.notna()) & (g.iv > 0) & (g.iv < 300)]
    if live.empty:
        return None
    traded = live[(live.oi.fillna(0) >= MIN_OI) | (live.volume.fillna(0) > 0)]
    if not traded.empty:
        live = traded

    live = live.assign(dist=(live.strike - close).abs())
    calls = live[live.type == "C"].nsmallest(1, "dist")
    puts = live[live.type == "P"].nsmallest(1, "dist")
    ivs = [float(x.iv.iloc[0]) for x in (calls, puts) if not x.empty]
    if not ivs:
        return None

    otm_p = live[(live.type == "P") & (live.moneyness.between(0.85, 0.95))]
    otm_c = live[(live.type == "C") & (live.moneyness.between(1.05, 1.15))]
    skew = None
    if not otm_p.empty and not otm_c.empty:
        skew = round(float(otm_p.iv.mean() - otm_c.iv.mean()), 2)

    return {
        "atm_iv": round(sum(ivs) / len(ivs), 2),
        "skew": skew,
        "volume": float(g.volume.fillna(0).sum()),
        "oi": float(g.oi.fillna(0).sum()),
    }


def _forward_vol(iv1: float, t1: float, iv2: float, t2: float) -> float | None:
    """由兩個到期嘅 IV 反推中間段前向波幅（variance additivity）。"""
    if t2 <= t1 or iv1 is None or iv2 is None:
        return None
    v = (iv2**2 * t2 - iv1**2 * t1) / (t2 - t1)
    return round(math.sqrt(v), 2) if v > 0 else None


def _label(slope: float | None) -> str:
    if slope is None:
        return "單月"
    if slope > 8:
        return "陡 Contango"
    if slope > 2:
        return "Contango"
    if slope < -8:
        return "陡 Backwardation"
    if slope < -2:
        return "Backwardation"
    return "平坦"


def surface(stock_code: str, as_of: date | None = None) -> dict | None:
    """單一股票完整波幅曲面。"""
    df = oc.chain(stock_code.zfill(5), as_of)
    if df.empty:
        return None
    close = float(df.close.iloc[0])
    if not close or close <= 0:
        return None

    terms: list[dict] = []
    for (exp, dte), g in df.groupby(["expiry", "dte"]):
        if dte < 1:
            continue
        m = _atm_metrics(g, close)
        if not m:
            continue
        terms.append({"expiry": str(exp), "dte": int(dte), **m})
    if not terms:
        return None
    terms.sort(key=lambda t: t["dte"])

    res: dict = {
        "stock_code": stock_code.zfill(5),
        "name": df.name.iloc[0],
        "close": close,
        "date": str(df.date.iloc[0]),
        "terms": terms,
    }

    def _pick(lo: int, hi: int) -> dict | None:
        cand = [t for t in terms if lo <= t["dte"] <= hi]
        return max(cand, key=lambda t: t["oi"]) if cand else None

    front = _pick(MIN_DTE_FRONT, MAX_DTE_FRONT) or (terms[0] if terms else None)
    back = _pick(MIN_DTE_BACK, MAX_DTE_BACK)
    if back is None:
        cand = [t for t in terms if front and t["dte"] > front["dte"]]
        back = max(cand, key=lambda t: t["oi"]) if cand else None

    if front:
        res["front"] = front
        res["front_skew"] = front["skew"]
    if front and back:
        slope = round((back["atm_iv"] - front["atm_iv"]) / front["atm_iv"] * 100, 1)
        fwd = _forward_vol(front["atm_iv"], front["dte"] / 365,
                           back["atm_iv"], back["dte"] / 365)
        res.update({
            "back": back,
            "slope_pct": slope,
            "forward_vol": fwd,
            "forward_factor": round(fwd / front["atm_iv"], 2) if fwd else None,
            "structure": _label(slope),
        })
    else:
        res["structure"] = "單月"
    return res


def scan(as_of: date | None = None, min_oi: float = 2000) -> list[dict]:
    """全市場期限結構掃描。"""
    df = oc.parse_chains(as_of)
    if df.empty:
        return []
    df = df[df.stock_code.notna() & df.close.notna() & (df.close > 0)]

    out: list[dict] = []
    for code, sub in df.groupby("stock_code"):
        if sub.oi.fillna(0).sum() < min_oi:
            continue
        close = float(sub.close.iloc[0])
        terms: list[dict] = []
        for (exp, dte), g in sub.groupby(["expiry", "dte"]):
            if dte < 1:
                continue
            m = _atm_metrics(g, close)
            if m:
                terms.append({"expiry": str(exp), "dte": int(dte), **m})
        if len(terms) < 2:
            continue
        terms.sort(key=lambda t: t["dte"])

        def _pick(lo: int, hi: int) -> dict | None:
            cand = [t for t in terms if lo <= t["dte"] <= hi]
            return max(cand, key=lambda t: t["oi"]) if cand else None

        front = _pick(MIN_DTE_FRONT, MAX_DTE_FRONT) or terms[0]
        back = _pick(MIN_DTE_BACK, MAX_DTE_BACK)
        if back is None:
            cand = [t for t in terms if t["dte"] > front["dte"]]
            if not cand:
                continue
            back = max(cand, key=lambda t: t["oi"])

        slope = round((back["atm_iv"] - front["atm_iv"]) / front["atm_iv"] * 100, 1)
        fwd = _forward_vol(front["atm_iv"], front["dte"] / 365,
                           back["atm_iv"], back["dte"] / 365)
        out.append({
            "stock_code": code,
            "name": sub.name.iloc[0],
            "close": close,
            "front_dte": front["dte"],
            "front_iv": front["atm_iv"],
            "back_dte": back["dte"],
            "back_iv": back["atm_iv"],
            "slope_pct": slope,
            "forward_vol": fwd,
            "forward_factor": round(fwd / front["atm_iv"], 2) if fwd else None,
            "front_skew": front["skew"],
            "oi": front["oi"] + back["oi"],
            "structure": _label(slope),
        })
    return sorted(out, key=lambda r: r["slope_pct"])


def _print_one(s: dict) -> None:
    print(f"{s['stock_code']} {s['name']}  收市 {s['close']}  報告日 {s['date']}")
    line = f"結構: {s.get('structure', '—')}"
    if s.get("slope_pct") is not None:
        line += f"   斜率 {s['slope_pct']:+.1f}%"
    if s.get("forward_vol"):
        line += f"   前向波幅 {s['forward_vol']}"
    if s.get("forward_factor"):
        line += f"   前向係數 {s['forward_factor']}"
    if s.get("front_skew") is not None:
        line += f"   Skew {s['front_skew']:+.1f}"
    print(line)

    print(f"\n{'到期':>12} {'DTE':>5} {'ATM IV':>7} {'Skew':>7} {'未平倉':>11}")
    for t in s["terms"]:
        sk = f"{t['skew']:>7.1f}" if t["skew"] is not None else "      —"
        print(f"{t['expiry']:>12} {t['dte']:>5} {t['atm_iv']:>7.1f} {sk} {int(t['oi']):>11,}")

    ff = s.get("forward_factor")
    if ff is not None:
        print()
        if ff < 0.85:
            print(f"  ▶ 前向係數 {ff} < 0.85：遠月被過度定價 → 賣遠月／買近月（日曆價差）")
        elif ff > 1.15:
            print(f"  ▶ 前向係數 {ff} > 1.15：近月被過度定價 → 賣近月／買遠月（反向日曆）")
        else:
            print(f"  ▶ 前向係數 {ff}：期限結構合理，冇明顯日曆機會")


def main() -> None:
    ap = argparse.ArgumentParser(description="波幅曲面：期限結構 + 偏斜")
    ap.add_argument("stock", nargs="?", help="股票代號")
    ap.add_argument("--scan", action="store_true", help="全市場掃描")
    ap.add_argument("--date", help="報告日 YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    as_of = date.fromisoformat(a.date) if a.date else None

    if a.scan or not a.stock:
        rows = scan(as_of)
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        print(f"{'代號':>6} {'名稱':<22} {'近IV':>6} {'遠IV':>6} {'斜率':>8} "
              f"{'前向':>6} {'FF':>5} {'Skew':>6}  結構")
        for r in rows[:a.limit]:
            ff = f"{r['forward_factor']:>5.2f}" if r["forward_factor"] else "    —"
            fv = f"{r['forward_vol']:>6.1f}" if r["forward_vol"] else "     —"
            sk = f"{r['front_skew']:>6.1f}" if r["front_skew"] is not None else "     —"
            print(f" {r['stock_code']:>5} {r['name'][:22]:<22} {r['front_iv']:>6.1f} "
                  f"{r['back_iv']:>6.1f} {r['slope_pct']:>7.1f}% {fv} {ff} {sk}  {r['structure']}")
        print(f"\n共 {len(rows)} 隻。斜率最負（backwardation）＝近月被搶貴，"
              f"通常有事件；斜率最正＝遠月貴。")
        return

    s = surface(a.stock, as_of)
    if not s:
        print(f"{a.stock} 冇期權數據")
        return
    if a.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return
    _print_one(s)


if __name__ == "__main__":
    main()
