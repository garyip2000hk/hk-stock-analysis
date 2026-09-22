"""regime_edge.py — 波幅體制 + 真實歷史期望值（direction_advisor 用）。

解決一個實際問題：`strategy_engine.evaluate()` 嘅期望值係用**對數常態
零漂移**假設積分出嚟。真實市場有肥尾、有漂移、而且「賣方賺唔賺錢」高度
取決於當下波幅係喺歷史高位定低位。同一個 Bull Put Spread：

  · 對數常態公式        → EV −$3（睇落中性，好似值得做）
  · 真實 1 年路徑重採樣  → EV −$243
  · 真實 20 年路徑重採樣 → EV −$610

所以本模組提供兩樣嘢：

1. `regime()` — 標的當下 HV20 喺自己歷史嘅百分位（低位 / 中性 / 高位），
   同 IV/HV 比率。低波幅位賣方冇溢價（實測負期望），高波幅位買方要付貴價。

2. `empirical_ev()` — 用**真實歷史 H 個交易日回報**重採樣，對策略嘅到期
   損益直接取平均，出 1年 / 5年 / 全樣本 / 同體制樣本四個口徑嘅期望值、
   勝率、最壞單筆同 5% 尾部。冇任何分佈假設。

歷史價由 Yahoo Finance 落嚟，cache 喺 `options_data/regime_history/`，
每日只拉一次。攞唔到就 fallback 去本地 parquet（HSI / 港股日K）。

CLI:
    python3 regime_edge.py HSI --market hk_index
    python3 regime_edge.py 00700
    python3 regime_edge.py NVDA --market us_stock
"""

from __future__ import annotations

import argparse
import json
import math
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "options_data" / "regime_history"
HSI_KLINE = ROOT.parent / "Desktop/db/Futu/Kline/kline_index.parquet"
HK_KLINE = ROOT.parent / "Desktop/db/Futu/Kline/kline_day.parquet"

TRADING_DAYS = 252
LOW_PCT = 20.0          # HV20 百分位 ≤ 20 → 波幅低位
HIGH_PCT = 80.0         # ≥ 80 → 波幅高位
MIN_WINDOWS = 60        # 樣本少過呢個唔出結論


# ---------------------------------------------------------------- 歷史價

def _yahoo_symbol(market: str, code: str) -> str:
    if market == "hk_index":
        return "^HSI"
    if market == "hk_stock":
        return f"{int(code):04d}.HK"
    return code.upper()


def _fetch_yahoo(symbol: str, rng: str = "20y") -> list[tuple[str, float]]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range={rng}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as fh:
        d = json.load(fh)
    r = d["chart"]["result"][0]
    ts = r["timestamp"]
    closes = r["indicators"]["quote"][0]["close"]
    out = []
    for t, c in zip(ts, closes):
        if c:
            out.append((date.fromtimestamp(t).isoformat(), float(c)))
    return out


def _local_history(market: str, code: str) -> list[tuple[str, float]]:
    try:
        import pandas as pd
        if market == "hk_index":
            df = pd.read_parquet(HSI_KLINE)
            df = df[df["code"] == "HK.800000"].sort_values("time_key")
            return [(str(r.time_key)[:10], float(r.close))
                    for r in df.itertuples()]
        if market == "hk_stock":
            df = pd.read_parquet(HK_KLINE)
            df = df[df["code"] == f"HK.{code}"].sort_values("time_key")
            return [(str(r.time_key)[:10], float(r.close))
                    for r in df.itertuples()]
    except Exception:
        pass
    return []


def history(market: str, code: str, max_age_h: int = 20) -> list[tuple[str, float]]:
    """收市價歷史（新→舊排好），日 cache。"""
    sym = _yahoo_symbol(market, code)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = CACHE_DIR / f"{sym.replace('^', 'IDX_').replace('.', '_')}.json"
    if fp.exists():
        age_h = (datetime.now().timestamp() - fp.stat().st_mtime) / 3600
        if age_h < max_age_h:
            try:
                return [(a, float(b)) for a, b in json.loads(fp.read_text())]
            except Exception:
                pass
    rows: list[tuple[str, float]] = []
    try:
        rows = _fetch_yahoo(sym)
    except Exception:
        rows = []
    if len(rows) < 300:
        loc = _local_history(market, code)
        if len(loc) > len(rows):
            rows = loc
    if rows:
        try:
            fp.write_text(json.dumps(rows))
        except Exception:
            pass
    elif fp.exists():
        try:
            return [(a, float(b)) for a, b in json.loads(fp.read_text())]
        except Exception:
            pass
    return rows


# ---------------------------------------------------------------- 波幅體制

def _log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def _std(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _hv_series(lr: list[float], win: int = 20) -> list[float]:
    return [_std(lr[i - win:i]) * math.sqrt(TRADING_DAYS)
            for i in range(win, len(lr) + 1)]


def regime(market: str, code: str, iv: float | None = None,
           rows: list[tuple[str, float]] | None = None) -> dict:
    """當下波幅體制：HV20 喺 2 年 / 5 年 / 全歷史嘅百分位。"""
    rows = rows if rows is not None else history(market, code)
    if len(rows) < 120:
        return {"ok": False, "error": "歷史價唔夠計波幅體制"}
    closes = [c for _, c in rows]
    lr = _log_returns(closes)
    hv_now = _std(lr[-20:]) * math.sqrt(TRADING_DAYS)
    hvs = _hv_series(lr)

    def pct(window: int | None) -> float | None:
        pool = hvs if window is None else hvs[-window:]
        if len(pool) < MIN_WINDOWS:
            return None
        return round(sum(1 for h in pool if h < hv_now) / len(pool) * 100, 1)

    p2 = pct(TRADING_DAYS * 2)
    p5 = pct(TRADING_DAYS * 5)
    pall = pct(None)
    ref = p5 if p5 is not None else (p2 if p2 is not None else pall)

    if ref is None:
        label, side = "unknown", None
    elif ref <= LOW_PCT:
        label, side = "low", "buy"
    elif ref >= HIGH_PCT:
        label, side = "high", "sell"
    else:
        label, side = "mid", None

    out = {
        "ok": True,
        "hv20": round(hv_now * 100, 1),
        "pct_2y": p2, "pct_5y": p5, "pct_all": pall, "pct_ref": ref,
        "label": label,
        "label_zh": {"low": "波幅低位", "mid": "波幅中性",
                     "high": "波幅高位", "unknown": "未知"}[label],
        "favours": side,
        "n_days": len(rows),
        "first_date": rows[0][0], "last_date": rows[-1][0],
    }
    if iv:
        out["iv_hv"] = round(iv / (hv_now * 100), 2) if hv_now else None
    out["note"] = _regime_note(out)
    return out


def _regime_note(r: dict) -> str:
    ref = r.get("pct_ref")
    hv = r.get("hv20")
    ih = r.get("iv_hv")
    bits = [f"HV20 {hv}%"]
    if ref is not None:
        bits.append(f"5 年百分位 {ref:.0f}%")
    if ih:
        bits.append(f"IV/HV {ih}")
    head = "　".join(bits)
    if r["label"] == "low":
        return (f"{head} → 波幅喺歷史低位。實測呢種日子賣方（收權金）"
                f"期望值轉負：權金收得少，但一次跳空就蝕足闊度。買方較有利。")
    if r["label"] == "high":
        return (f"{head} → 波幅喺歷史高位。買方要付貴價，實測期望值轉負；"
                f"賣方有溢價。")
    return f"{head} → 波幅中性，兩邊都冇明顯體制優勢，睇個別組合期望值。"


# ---------------------------------------------------------------- 真實期望值

def _payoff(legs: list[dict], s: float) -> float:
    tot = 0.0
    for lg in legs:
        intrinsic = (max(s - lg["strike"], 0.0) if lg["cp"] == "C"
                     else max(lg["strike"] - s, 0.0))
        tot += lg["qty"] * intrinsic
    return tot


def _stats(pnls: list[float]) -> dict:
    n = len(pnls)
    if not n:
        return {}
    srt = sorted(pnls)
    return {
        "n": n,
        "ev": round(sum(pnls) / n, 0),
        "win_rate": round(sum(1 for p in pnls if p > 0) / n * 100, 1),
        "worst": round(srt[0], 0),
        "p5": round(srt[max(0, int(n * 0.05) - 1)], 0),
        "median": round(srt[n // 2], 0),
    }


def empirical_ev(legs: list[dict], spot: float, size: int, dte: int,
                 market: str, code: str, fee_per_leg: float = 30.0,
                 rows: list[tuple[str, float]] | None = None,
                 reg: dict | None = None) -> dict:
    """用真實歷史回報重採樣計到期期望值（無分佈假設）。

    legs 嘅 price 用分析時嘅權金；淨成本 = Σ qty×price（>0 = 付出）。
    H = dte 轉交易日；喺歷史所有 H 日窗口滾動，每個窗口當成一次「今日開倉、
    到期結算」，出 P&L 分佈。
    """
    rows = rows if rows is not None else history(market, code)
    if len(rows) < 300:
        return {"ok": False, "error": "歷史價唔夠做真實回測"}
    closes = [c for _, c in rows]
    lr = _log_returns(closes)
    H = max(1, round(dte * TRADING_DAYS / 365))
    if len(lr) <= H + 40:
        return {"ok": False, "error": "歷史價唔夠覆蓋呢個到期日"}

    net = sum(lg["qty"] * lg["price"] for lg in legs)
    fees = fee_per_leg * len(legs) * 2
    hvs = _hv_series(lr)          # hvs[i] 對應 lr[:20+i] → 即 closes[20+i]
    hv_now = _std(lr[-20:]) * math.sqrt(TRADING_DAYS)
    label = (reg or {}).get("label")

    all_p: list[float] = []
    y1: list[float] = []
    y5: list[float] = []
    same: list[float] = []
    n_lr = len(lr)
    for i in range(n_lr - H):
        ret = math.exp(sum(lr[i:i + H])) - 1.0
        s_end = spot * (1.0 + ret)
        pnl = (_payoff(legs, s_end) - net) * size - fees
        all_p.append(pnl)
        back = n_lr - H - i        # 距今幾多個窗口
        if back <= TRADING_DAYS:
            y1.append(pnl)
        if back <= TRADING_DAYS * 5:
            y5.append(pnl)
        # 開倉日嘅 HV20：用窗口起點前 20 日回報
        if label in ("low", "high") and 0 <= i - 20 < len(hvs):
            hv_then = hvs[i - 20]
            if label == "low" and hv_then <= hv_now * 1.15:
                same.append(pnl)
            elif label == "high" and hv_then >= hv_now * 0.85:
                same.append(pnl)

    out = {
        "ok": True,
        "horizon_trading_days": H,
        "net_cost_hkd": round(net * size, 0),
        "fees_hkd": round(fees, 0),
        "all": _stats(all_p),
        "y1": _stats(y1),
        "y5": _stats(y5),
        "same_regime": _stats(same) if len(same) >= MIN_WINDOWS else None,
    }
    evs = [out[k]["ev"] for k in ("y1", "y5", "all")
           if out.get(k) and out[k].get("ev") is not None]
    if out.get("same_regime"):
        evs.append(out["same_regime"]["ev"])
    if evs:
        out["ev_worst_case"] = min(evs)
        out["ev_mean"] = round(sum(evs) / len(evs), 0)
        out["all_positive"] = all(e > 0 for e in evs)
    return out


# ---------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--market", default="hk_stock",
                    choices=["hk_stock", "hk_index", "us_stock"])
    ap.add_argument("--iv", type=float)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = regime(a.market, a.code, a.iv)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return
    if not r.get("ok"):
        print(r.get("error"))
        return
    print(f"{a.code}  {r['label_zh']}")
    print(f"  歷史 {r['first_date']} → {r['last_date']}（{r['n_days']} 日）")
    print(f"  {r['note']}")


if __name__ == "__main__":
    main()
