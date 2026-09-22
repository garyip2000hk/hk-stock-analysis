"""ib_futures_backfill.py — 期貨補歷史：串多個合約月份還原 front-month 連續日K.

點解要串：ib_data_importer 每日淨攞「當時最新」嗰個月合約，合約未上市前
冇數據，所以首次抓取歷史會短咗（ES 249 日／YM 179 日／MHI 136 日）。
呢個腳本逐個月合約攞返過期歷史（includeExpired + endDateTime 帶時間）。

串接方法（唔靠 lastTradeDate——IB 唔一定回）：
  1. reqContractDetails(includeExpired=True) 攞晒所有月份合約，用
     contractMonth (YYYYMM) 排序
  2. 每個合約攞 window 內佢自己全部日K（endDateTime = 該月月尾，
     過期後自然冇 bar，唔會撈埋下一隻）
  3. 每個日期揀「最舊仍交易緊」嗰隻合約（min contractMonth）＝ front month

用法：
  python3 ib_futures_backfill.py [--days 730] [--symbols ES NQ YM HSI MHI]
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

KLINE_PATH = Path("/home/workspace/Desktop/db/IB/Kline/kline_ib_day.parquet")
LOG_PATH = Path("/home/workspace/stock-analysis/options_data/ib_futures_backfill_log.json")

HOST, PORT = "127.0.0.1", 4001

SYMBOLS = {
    "ES": ("CME", "USD"),
    "NQ": ("CME", "USD"),
    "YM": ("CBOT", "USD"),
    "HSI": ("HKFE", "HKD"),
    "MHI": ("HKFE", "HKD"),
}

PACING_SLEEP = 5.0   # 每個歷史請求之間休眠秒數（IB pacing 限制）


def log(msg: str):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


async def months_chain(ib, symbol: str, exchange: str, currency: str, window_start: str):
    """回 [(contractMonth, contract)] 由舊到新；跳過太遠嘅未來月份."""
    from ib_async import Future

    c = Future(symbol, exchange=exchange, currency=currency)
    c.includeExpired = True
    details = await ib.reqContractDetailsAsync(c)
    futs = [d.contract for d in details if d.contract.secType == "FUT"]
    rows = []
    for ct in futs:
        cm = (getattr(ct, "lastTradeDateOrContractMonth", "") or "")[:6]
        if len(cm) == 6 and cm.isdigit():
            rows.append((cm, ct))
    rows.sort(key=lambda r: r[0])
    keep = [r for r in rows if r[0] >= window_start.replace("-", "")[:6]]
    # 未來太遠嘅月份（未上市）冇歷史，留返依家 + 下一季就夠
    import calendar
    now = datetime.now()
    y, m = now.year, (now.month - 1 + 5) % 12 + 1  # current + 4 months
    if m < now.month:
        y += 1
    cutoff = f"{y}{m:02d}"
    keep = [r for r in keep if r[0] <= cutoff]
    return keep


async def fetch_month(ib, contract, end: str, dur_days: int, retries: int = 3):
    """攞一個合約以 end 為終點往回 dur_days 日嘅日K（過期合約 end=到期日）."""
    dur = max(min(dur_days, 364), 10)
    for attempt in range(retries):
        try:
            bars = await ib.reqHistoricalDataAsync(
                contract, endDateTime=end, barSizeSetting="1 day",
                durationStr=f"{dur} D", useRTH=True, whatToShow="TRADES",
                formatDate=1, timeout=90)
            return bars or []
        except Exception as e:
            log(f"    ⚠ {getattr(contract, 'localSymbol', '?')} attempt {attempt+1} failed: {str(e)[:90]}")
            if attempt < retries - 1:
                await asyncio.sleep(15)
    return []


async def backfill_symbol(ib, symbol: str, exchange: str, currency: str,
                          window_start: str) -> pd.DataFrame:
    chain = await months_chain(ib, symbol, exchange, currency, window_start)
    if not chain:
        log(f"  ✗ {symbol}: 冇合約詳情")
        return pd.DataFrame()
    log(f"  {symbol}: {len(chain)} 個月合約（{chain[0][1].localSymbol} → {chain[-1][1].localSymbol}）")

    today = datetime.now().strftime("%Y-%m-%d")
    by_date = {}  # date -> (contractMonth, bar)
    for cm, ct in chain:
        ltd = (getattr(ct, "lastTradeDate", "") or "")[:8]
        expired = ltd and ltd[:4].isdigit() and datetime.strptime(ltd, "%Y%m%d") <= datetime.now()
        if expired:
            end = f"{ltd} 23:59:59"        # 過期合約：終點=最後交易日（必須帶時間）
            dur = (datetime.strptime(ltd, "%Y%m%d") - datetime.strptime(window_start, "%Y-%m-%d")).days + 15
        else:
            end = f"{today.replace('-', '')} 23:59:59"  # 未到期：終點=今日（必須帶時間）
            dur = (datetime.now() - datetime.strptime(window_start, "%Y-%m-%d")).days + 5
        bars = await fetch_month(ib, ct, end, dur)
        n = 0
        for b in bars:
            d = str(b.date)[:10]
            if d < window_start or d > today:
                continue
            # 同一日多過一個合約有價 → 揀最舊嗰個（front month 慣例）
            if d not in by_date or cm < by_date[d][0]:
                by_date[d] = (cm, b)
            n += 1
        log(f"    ✓ {ct.localSymbol:<12s} ({cm}): {n} 行")
        await asyncio.sleep(PACING_SLEEP)

    rows = []
    for d in sorted(by_date):
        cm, b = by_date[d]
        rows.append({
            "date": d, "symbol": symbol, "sec_type": "future",
            "open": float(b.open), "high": float(b.high),
            "low": float(b.low), "close": float(b.close),
            "volume": int(b.volume or 0),
            "front_month": symbol + cm[2:],
        })
    return pd.DataFrame(rows)


async def run(days: int, symbols: list[str]):
    from ib_async import IB

    window_start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    ib = IB()
    try:
        await asyncio.wait_for(ib.connectAsync(HOST, PORT, clientId=93, timeout=15), timeout=25)
    except Exception as e:
        return {"status": "error", "error": f"連接失敗（gateway 未登入？）: {e}"}
    log(f"已連接 IB Gateway，補歷史窗口 {window_start} → 今日")

    frames = []
    for sym in symbols:
        ex, cur = SYMBOLS[sym]
        df = await backfill_symbol(ib, sym, ex, cur, window_start)
        if len(df):
            frames.append(df)
        await asyncio.sleep(PACING_SLEEP)
    ib.disconnect()

    if not frames:
        return {"status": "error", "error": "冇攞到任何期貨數據"}
    new = pd.concat(frames, ignore_index=True)
    new = new.drop_duplicates(subset=["date", "symbol"], keep="last")

    old = pd.read_parquet(KLINE_PATH) if KLINE_PATH.exists() else pd.DataFrame()
    stats = {}
    if len(old):
        syms = new["symbol"].unique().tolist()
        kept = old[(old["symbol"].isin(syms)) & (old["date"] >= window_start)]
        stats = {s: {"old_rows": int((kept["symbol"] == s).sum())} for s in syms}
        old = old[~((old["symbol"].isin(syms)) & (old["date"] >= window_start))]
    df = pd.concat([old, new], ignore_index=True).sort_values(
        ["symbol", "date"]).reset_index(drop=True)
    df.to_parquet(KLINE_PATH, index=False)

    for s in new["symbol"].unique():
        g = new[new["symbol"] == s]
        stats.setdefault(s, {})["new_rows"] = len(g)
        stats[s]["range"] = f"{g['date'].min()} → {g['date'].max()}"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(
        {"run": datetime.now().isoformat(timespec="seconds"), "window_start": window_start,
         "stats": stats}, ensure_ascii=False, indent=1))
    log(f"完成：新增 {len(new)} 行，總計 {len(df)} 行 → {KLINE_PATH}")
    return {"status": "ok", "new_rows": len(new), "total_rows": len(df), "stats": stats}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    a = ap.parse_args()
    out = asyncio.run(run(a.days, a.symbols))
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
