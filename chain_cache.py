"""chain_cache.py — 期權鏈解析快取。

審查報告 E：原本嘅回測每個日期、每隻股都重新 gunzip + regex 整份 HKEX
日報（`options_chain.parse_chains(d, code)`）→ O(日 × 股) 重複解析。
加入逐日 MTM（Stage 0 要求）之後，讀取次數再乘以持倉日數，慢到無法迭代。

呢個模組做兩件事：

  1. **每份日報只解析一次**，全部股票一齊 parse，之後按 stock_code 切。
  2. LRU 記憶體上限（預設 60 個交易日）＋ 可選 parquet 落地快取，
     令重複回測唔需要再 regex。

資料來源優先次序（快 → 慢）：

  記憶體 LRU → `chain_history.parquet`（如果 repo 有 `chain_history.py`
  而且快取已建立）→ 逐日 `chain_cache/chain_YYYYMMDD.parquet`
  → gunzip + regex 解 raw 日報

`chain_history` 係 `hk-stock-analysis` 生產 repo 已經每日 `--update` 嘅
全歷史快取（只留有流動性嘅合約）。有佢就直接用，唔好再造第二份快取 ——
所以 import 係 **optional**，兩個 repo 可以共用同一份 chain_cache.py。

用法：

    import chain_cache as cc
    df = cc.day(date(2026, 8, 6))                  # 全部股票
    df = cc.day(date(2026, 8, 6), "00700")         # 單股
    legs = cc.legs(date(2026, 8, 6), "00700", expiry, [650.0, 700.0])

    cc.prime(dates)          # 預熱（順序讀，disk friendly）
    cc.stats()               # 命中率
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date
from pathlib import Path

import pandas as pd

import options_chain as oc

try:                                   # 生產 repo 才有；研究 repo 冇亦要跑得
    import chain_history as ch
except ImportError:
    ch = None

BASE = Path(__file__).parent
DISK_DIR = BASE / "options_data" / "chain_cache"

MAX_DAYS_IN_MEM = 60

_mem: "OrderedDict[date, pd.DataFrame]" = OrderedDict()
_hits = {"mem": 0, "history": 0, "disk": 0, "parse": 0, "miss": 0}

# chain_history 快取存在唔存在，只探一次（避免每日都 stat 一次大檔）
_history_ok: bool | None = None


def _history_available() -> bool:
    global _history_ok
    if _history_ok is None:
        _history_ok = bool(ch is not None and ch.CACHE.exists())
    return _history_ok


def _from_history(d: date) -> pd.DataFrame | None:
    """由 chain_history.parquet 抽單日。搵唔到／出錯就 None（交回上層 fallback）。"""
    if not _history_available():
        return None
    try:
        df = ch.load(start=d, end=d)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if df is None or df.empty:
        return None
    # chain_history 用 datetime64，下游按 date 比較 expiry，統一轉返 date
    out = df.copy()
    for c in ("date", "expiry"):
        if c in out.columns:
            out[c] = pd.to_datetime(out[c]).dt.date
    return out


def latest() -> date | None:
    """最近一個有 raw 報告嘅交易日。"""
    return oc.latest_raw()


def _disk_path(d: date) -> Path:
    return DISK_DIR / f"chain_{d:%Y%m%d}.parquet"


def _touch(d: date, df: pd.DataFrame) -> pd.DataFrame:
    _mem[d] = df
    _mem.move_to_end(d)
    while len(_mem) > MAX_DAYS_IN_MEM:
        _mem.popitem(last=False)
    return df


def day(d: date, stock_code: str | None = None,
        use_disk: bool = True) -> pd.DataFrame:
    """該日全部（或單股）期權鏈。冇報告就回傳空 DataFrame。"""
    if d is None:
        d = latest()
    if d is None:
        return pd.DataFrame()
    if d in _mem:
        _hits["mem"] += 1
        df = _mem[d]
    else:
        df = _from_history(d)
        if df is not None:
            _hits["history"] += 1
        p = _disk_path(d)
        if df is None and use_disk and p.exists():
            try:
                df = pd.read_parquet(p)
                _hits["disk"] += 1
            except (OSError, ValueError):
                df = None
        if df is None:
            df = oc.parse_chains(d)          # 一次 parse 全部股票
            _hits["parse"] += 1
            if df.empty:
                _hits["miss"] += 1
            elif use_disk:
                try:
                    DISK_DIR.mkdir(parents=True, exist_ok=True)
                    df.to_parquet(p, index=False)
                except (OSError, ValueError, ImportError):
                    pass                      # 落地失敗唔應該中斷回測
        _touch(d, df)

    if stock_code is None or df.empty:
        return df
    return df[df.stock_code == str(stock_code).zfill(5)]


def prime(dates: list[date], use_disk: bool = True, verbose: bool = False) -> int:
    """順序預熱一批日期，回傳成功載入嘅日數。

    有 `chain_history` 快取時**一次讀晒整個日期範圍**再切日，
    比逐日 filter parquet 快好多（逐日 filter 每次都要掃 metadata）。
    """
    if not dates:
        return 0
    if _history_available():
        try:
            big = ch.load(start=min(dates), end=max(dates))
        except (FileNotFoundError, OSError, ValueError):
            big = None
        if big is not None and not big.empty:
            big = big.copy()
            for c in ("date", "expiry"):
                if c in big.columns:
                    big[c] = pd.to_datetime(big[c]).dt.date
            want = set(dates)
            n = 0
            for d, grp in big.groupby("date", sort=True):
                if d in want:
                    _touch(d, grp.reset_index(drop=True))
                    _hits["history"] += 1
                    n += 1
            if verbose:
                print(f"  由 chain_history 預熱 {n}/{len(dates)} 日")
            if n:
                return n
    n = 0
    for i, d in enumerate(dates, 1):
        if not day(d, use_disk=use_disk).empty:
            n += 1
        if verbose and i % 25 == 0:
            print(f"  預熱 {i}/{len(dates)} …", flush=True)
    return n


def legs(d: date, stock_code: str, expiry: date,
         strikes: list[float] | None = None) -> pd.DataFrame:
    """該日、該股、該到期月（可指定行使價）嘅腳。"""
    df = day(d, stock_code)
    if df.empty:
        return df
    out = df[df.expiry == expiry]
    if strikes:
        out = out[out.strike.isin(strikes)]
    return out


def leg_mid(d: date, stock_code: str, expiry: date, strike: float,
            cp: str) -> float | None:
    """單腳中價（HKEX 結算價）。搵唔到就 None，由呼叫方決定 fallback。"""
    df = legs(d, stock_code, expiry, [strike])
    if df.empty:
        return None
    sub = df[df.type == cp]
    if sub.empty:
        return None
    v = sub.settle.iloc[0]
    return None if pd.isna(v) else float(v)


def atm_iv(d: date, stock_code: str, expiry: date,
           spot: float) -> float | None:
    """該日該到期月最接近現價嘅 IV%，用嚟做模型價 fallback。"""
    df = legs(d, stock_code, expiry)
    if df.empty:
        return None
    df = df.dropna(subset=["iv"])
    df = df[(df.iv > 0) & (df.iv < 200)]
    if df.empty:
        return None
    row = df.iloc[(df.strike - spot).abs().argsort().iloc[0]]
    return float(row.iv)


def clear() -> None:
    global _history_ok
    _history_ok = None
    _mem.clear()
    for k in _hits:
        _hits[k] = 0


def stats() -> dict:
    total = sum(_hits.values())
    fast = _hits["mem"] + _hits["history"] + _hits["disk"]
    return {**_hits,
            "days_in_mem": len(_mem),
            "source": "chain_history" if _history_available() else "raw",
            "hit_rate": round(fast / total * 100, 1) if total else None}
