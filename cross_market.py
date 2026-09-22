#!/usr/local/bin/python3
"""cross_market.py — 隔夜美股／期貨跨市場訊號（恒指開市方向參考）.

數據源：`Desktop/db/IB/Kline/kline_ib_day.parquet`（IB Gateway 日K）。

邏輯：
  恒指交易日 d 嘅「隔夜」＝ d 之前最近一個美股交易日 u 嘅全日表現。
  - SPX/NDX/ES/NQ 日變化 %（u 收市 vs u 前一交易日收市）
  - VIX 水平同變化
  - 方向訊號：升跌閘 ±DIR_TH（0.3%），SPX 同 NDX 要同向先算方向性
  - 強度：強 = |SPX| ≥ 0.8% 或 |NDX| ≥ 1.2%；VIX 單日 +2 以上 = 風險規避加註

驗證：逐日對返恒指實際表現——
  - `hit_dir`：訊號方向 對 恒指當日升跌（收市 vs 前收市）
  - `hit_gap`：訊號方向 對 恒指開市裂口（開市 vs 前收市）
  - 分桶統計（按 |SPX 變化| 大細）

輸出 `options_data/cross_market.json`：
  latest（下一交易日參考）＋ series（逐日對照）＋ stats

CLI:
    python3 cross_market.py            # rebuild + 打印最新訊號
    python3 cross_market.py --json     # 淨出 JSON
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

BASE = Path(__file__).parent
IB_KLINE = Path("/home/workspace/Desktop/db/IB/Kline/kline_ib_day.parquet")
OUT = BASE / "options_data" / "cross_market.json"

DIR_TH = 0.3       # 方向性門檻 %
STRONG_SPX = 0.8   # 強訊號門檻 %
STRONG_NDX = 1.2
VIX_JUMP = 2.0     # VIX 單日升幅警示（點）

# VIX regime 門檻（任務 4 共用）
VIX_TOO_LOW = 15.0
VIX_CAUTION = 22.0
VIX_STRESS = 30.0


def _now_hkt() -> str:
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(timespec="seconds")


def _py(o):
    import numpy as np
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    return o


def load_ib() -> dict[str, pd.DataFrame]:
    df = pd.read_parquet(IB_KLINE)
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return {s: g.sort_values("date").reset_index(drop=True)
            for s, g in df.groupby("symbol")}


def chg_series(df: pd.DataFrame) -> pd.Series:
    return df.set_index("date")["close"].pct_change() * 100


def direction(spx: float | None, ndx: float | None) -> str:
    if spx is None or ndx is None:
        return "unknown"
    up = spx >= DIR_TH and ndx >= DIR_TH
    dn = spx <= -DIR_TH and ndx <= -DIR_TH
    if up:
        return "up"
    if dn:
        return "down"
    return "flat"


def vix_regime(level: float | None) -> dict:
    """VIX 環境分級（鐵鷹／strangle 波動率過濾用）。"""
    if level is None:
        return {"level": None, "regime": "unknown",
                "note": "冇 VIX 數據（IB 數據未同步）"}
    if level < VIX_TOO_LOW:
        return {"level": round(level, 2), "regime": "too_low",
                "note": f"VIX {level:.1f} < {VIX_TOO_LOW:.0f}：市場太靜，權金太薄，賣方冇肉食"}
    if level <= VIX_CAUTION:
        return {"level": round(level, 2), "regime": "normal",
                "note": f"VIX {level:.1f}：正常賣方環境"}
    if level <= VIX_STRESS:
        return {"level": round(level, 2), "regime": "caution",
                "note": f"VIX {level:.1f}：權金厚但尾部風險升，建議減注／收窄翼寬"}
    return {"level": round(level, 2), "regime": "stress",
            "note": f"VIX {level:.1f} > {VIX_STRESS:.0f}：壓力市，唔好做賣方"}


def build() -> dict:
    ib = load_ib()
    spx, ndx, vix = ib.get("SPX"), ib.get("NDX"), ib.get("VIX")
    es, nq = ib.get("ES"), ib.get("NQ")
    hsi = ib.get("HSI")
    if spx is None or hsi is None:
        raise RuntimeError("IB parquet 冇 SPX/HSI，先跑 ib_data_importer")

    spx_c = spx.set_index("date")["close"]
    ndx_c = ndx.set_index("date")["close"]
    vix_c = vix.set_index("date")["close"] if vix is not None else pd.Series(dtype=float)
    es_c = es.set_index("date")["close"] if es is not None else pd.Series(dtype=float)
    nq_c = nq.set_index("date")["close"] if nq is not None else pd.Series(dtype=float)

    spx_chg, ndx_chg = chg_series(spx), chg_series(ndx)
    es_chg = chg_series(es) if es is not None else pd.Series(dtype=float)
    nq_chg = chg_series(nq) if nq is not None else pd.Series(dtype=float)
    vix_chg = chg_series(vix) if vix is not None else pd.Series(dtype=float)

    hsi_df = hsi.set_index("date")
    hsi_prev_close = hsi_df["close"].shift(1)
    hsi_dir = (hsi_df["close"] > hsi_prev_close).where(
        hsi_df["close"] != hsi_prev_close)

    us_dates = spx_c.index.tolist()
    series: list[dict] = []
    for d, row in hsi_df.iterrows():
        prior = [u for u in us_dates if u < d]
        if len(prior) < 2:
            continue
        u, u0 = prior[-1], prior[-2]
        sc = spx_chg.get(u)
        nc = ndx_chg.get(u)
        if pd.isna(sc) or pd.isna(nc):
            continue
        dir_ = direction(float(sc), float(nc))
        strong = abs(sc) >= STRONG_SPX or abs(nc) >= STRONG_NDX
        vx = vix_c.get(u)
        vjump = vix_chg.get(u)
        act_dir = hsi_dir.get(d)
        act_dir = None if act_dir is None or pd.isna(act_dir) else bool(act_dir)
        gap = None
        prev_c = hsi_prev_close.get(d)
        if pd.notna(row["open"]) and prev_c and prev_c > 0:
            gap = (row["open"] / prev_c - 1) * 100

        def hit(sig_dir: str, actual_up: bool | None) -> bool | None:
            if sig_dir not in ("up", "down") or actual_up is None:
                return None
            return (sig_dir == "up") == actual_up

        series.append({
            "d": d, "us_date": u,
            "spx": round(float(sc), 2), "ndx": round(float(nc), 2),
            "es": round(float(es_chg.get(u)), 2) if pd.notna(es_chg.get(u)) else None,
            "nq": round(float(nq_chg.get(u)), 2) if pd.notna(nq_chg.get(u)) else None,
            "vix": round(float(vx), 2) if vx is not None and pd.notna(vx) else None,
            "vix_chg": round(float(vjump), 2) if vjump is not None and pd.notna(vjump) else None,
            "dir": dir_, "strong": bool(strong),
            "hsi_chg": round(float(row["close"] / prev_c - 1) * 100, 2) if prev_c and prev_c > 0 else None,
            "hsi_up": act_dir,
            "hsi_gap": round(float(gap), 2) if gap is not None else None,
            "hit_dir": hit(dir_, act_dir),
            "hit_gap": hit(dir_, None if gap is None else gap > 0),
        })

    # ── 統計（只計有方向嘅訊號日）──
    def rate(key: str) -> dict:
        rows = [s for s in series if s[key] is not None]
        n = len(rows)
        h = sum(1 for s in rows if s[key])
        return {"n": n, "hit": h, "pct": round(100 * h / n, 1) if n else None}

    def bucket(lo: float, hi: float, key: str = "spx") -> dict:
        rows = [s for s in series
                if s["dir"] in ("up", "down") and lo <= abs(s[key] or 0) < hi]
        rows = [s for s in rows if s["hit_dir"] is not None]
        n = len(rows)
        h = sum(1 for s in rows if s["hit_dir"])
        return {"n": n, "pct": round(100 * h / n, 1) if n else None}

    strong_rows = [s for s in series if s["strong"] and s["hit_dir"] is not None]
    stats = {
        "dir": rate("hit_dir"),
        "gap": rate("hit_gap"),
        "strong": {"n": len(strong_rows),
                   "pct": round(100 * sum(1 for s in strong_rows if s["hit_dir"]) / len(strong_rows), 1)
                   if strong_rows else None},
        "buckets": [
            {"label": "|SPX| 0.3-0.5%", **bucket(0.3, 0.5)},
            {"label": "|SPX| 0.5-1.0%", **bucket(0.5, 1.0)},
            {"label": "|SPX| ≥ 1.0%", **bucket(1.0, 99)},
        ],
    }

    latest = series[-1] if series else None
    vx_last = float(vix_c.iloc[-1]) if len(vix_c) else None
    payload = {
        "updated": _now_hkt(),
        "params": {"dir_th": DIR_TH, "strong_spx": STRONG_SPX,
                   "strong_ndx": STRONG_NDX, "vix_jump": VIX_JUMP},
        "latest": latest,
        "vix_regime": vix_regime(vx_last),
        "stats": stats,
        "series": series,
    }
    return payload


def summary() -> dict:
    """輕量版（俾 hsi_strangle_service /overnight 用）：最新訊號＋VIX regime。"""
    try:
        p = build()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    lt = p["latest"] or {}
    spx, ndx = lt.get("spx"), lt.get("ndx")
    es_night = lt.get("es")
    warn = "none"
    mv = max(abs(x) for x in (spx, ndx, es_night) if x is not None) if any(
        x is not None for x in (spx, ndx, es_night)) else 0
    if mv >= 1.5:
        warn = "alert"
    elif mv >= 0.8:
        warn = "watch"
    side_risk = None
    if lt.get("dir") == "down":
        side_risk = "Put 邊風險高（美股跌，恒指跟跌壓力大，Put 短腿可能被試）"
    elif lt.get("dir") == "up":
        side_risk = "Call 邊風險高（美股升，恒指跟升，Call 短腿可能被試）"
    return {
        "ok": True,
        "us_date": lt.get("us_date"),
        "spx_pct": spx, "ndx_pct": ndx, "es_pct": es_night, "nq_pct": lt.get("nq"),
        "vix": lt.get("vix"), "vix_chg": lt.get("vix_chg"),
        "direction": lt.get("dir"), "strong": lt.get("strong"),
        "warn_level": warn,
        "side_risk": side_risk,
        "vix_regime": p["vix_regime"],
        "stats": p["stats"],
        "for_hsi_date": None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    p = build()
    if not args.json:
        lt = p["latest"] or {}
        vr = p["vix_regime"]
        print(f"🌏 跨市場訊號（美股 {lt.get('us_date')} → 恒指 {lt.get('d')} 實戰對照）")
        print(f"   SPX {lt.get('spx'):+.2f}%  NDX {lt.get('ndx'):+.2f}%  "
              f"ES {lt.get('es'):+.2f}%  VIX {lt.get('vix')}")
        print(f"   方向 {lt.get('dir')}{'（強）' if lt.get('strong') else ''} | "
              f"恒指實際 {('%+.2f%%' % lt['hsi_chg']) if lt.get('hsi_chg') is not None else '—'} "
              f"{'✅' if lt.get('hit_dir') else '❌' if lt.get('hit_dir') is False else ''}")
        print(f"   VIX regime: {vr['regime']} — {vr['note']}")
        st = p["stats"]
        print(f"   歷史：方向訊號 {st['dir']['n']} 日 命中 {st['dir']['pct']}% | "
              f"強訊號 {st['strong']['n']} 日 {st['strong']['pct']}%")
        for b in st["buckets"]:
            print(f"     {b['label']}: n={b['n']} 命中 {b['pct']}%")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(p, ensure_ascii=False, separators=(",", ":"), default=_py))
    if args.json:
        print(json.dumps({"latest": p["latest"], "vix_regime": p["vix_regime"],
                          "stats": p["stats"]}, ensure_ascii=False, indent=1, default=_py))
    print(f"OK wrote {OUT}")


OVN_CACHE = {"t": 0.0, "v": None}


def overnight_live(ttl: float = 60.0) -> dict:
    """IB 實時夜盤：ES/NQ 最新 1 小時 bar vs 上個美股日市收 → 隔夜變幅 + 預警級別。
    VIX 無 live 訂閱，用日 K 最後收市。IB 死咗就 ok=False（面板 fallback 收市版）。"""
    import time as _t
    now = _t.time()
    if OVN_CACHE["v"] is not None and now - OVN_CACHE["t"] < ttl:
        return OVN_CACHE["v"]
    out = {"ok": False, "error": None, "legs": [], "warn": "normal",
           "warn_zh": "", "vix": None, "asof": _now_hkt()}
    try:
        import asyncio
        from ib_async import IB
        import ib_data_importer as ibi

        async def _run():
            ib = IB()
            await ib.connectAsync("127.0.0.1", 4001, clientId=93, timeout=10)
            try:
                px = {}
                for sym in ("ES", "NQ"):
                    c = await ibi.resolve_front_month(ib, sym, "CME", "USD")
                    bars = await ib.reqHistoricalDataAsync(
                        c, endDateTime="", durationStr="2 D",
                        barSizeSetting="1 hour", whatToShow="TRADES",
                        useRTH=False, formatDate=1)
                    if bars:
                        px[sym] = {"last": float(bars[-1].close),
                                   "asof": str(bars[-1].date)}
                return px
            finally:
                ib.disconnect()

        px = asyncio.run(_run())
        if not px:
            out["error"] = "IB 無夜盤報價"
            OVN_CACHE.update(t=now, v=out)
            return out
        ibd = load_ib()
        base = {}
        for sym in ("ES", "NQ"):
            d = ibd.get(sym)
            if d is not None and len(d):
                base[sym] = float(d.iloc[-1]["close"])
        vix = None
        dv = ibd.get("VIX")
        if dv is not None and len(dv):
            vix = float(dv.iloc[-1]["close"])
        legs = []
        for sym in ("ES", "NQ"):
            if sym in px and sym in base and base[sym] > 0:
                pct = (px[sym]["last"] - base[sym]) / base[sym] * 100
                legs.append({"sym": sym, "last": round(px[sym]["last"], 2),
                             "base": round(base[sym], 2),
                             "pct": round(pct, 2), "asof": px[sym]["asof"]})
        warn, warn_zh = "normal", ""
        if legs:
            m = min(l["pct"] for l in legs)
            if m <= -2.0:
                warn, warn_zh = "red", "隔夜美股期貨急跌，恒指開市偏淡，Put 邊壓力大"
            elif m <= -1.0:
                warn, warn_zh = "amber", "隔夜美股期貨跌逾 1%，恒指開市承壓"
            elif m >= 1.0:
                warn, warn_zh = "green", "隔夜美股期貨升逾 1%，恒指開市偏強"
        if vix is not None:
            if vix >= 25:
                warn, warn_zh = "red", f"VIX {vix:.1f} 高波幅，權金厚但跳空風險大"
            elif vix >= 20 and warn == "normal":
                warn, warn_zh = "amber", f"VIX {vix:.1f} 偏強，留意波幅"
        out.update(ok=True, legs=legs, warn=warn, warn_zh=warn_zh, vix=vix)
    except Exception as e:
        out["error"] = str(e)[:120]
    OVN_CACHE.update(t=now, v=out)
    return out
