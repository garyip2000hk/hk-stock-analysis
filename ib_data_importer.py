"""
IB Gateway 數據匯入 — 美股／指數／期貨日K
  經 IBC + IB Gateway（port 4001，服務名 `ib-gateway`）
  期貨自動揀最近到期月份（front month）

輸出：
  /home/workspace/Desktop/db/IB/Kline/kline_ib_day.parquet  (append-only, 去重)

用法：
  python3 ib_data_importer.py [--days 365]
"""
import argparse
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

OUT_DIR = Path("/home/workspace/Desktop/db/IB/Kline")
KLINE_PATH = OUT_DIR / "kline_ib_day.parquet"

HOST = "127.0.0.1"
PORT = 4001

HKT = timezone(timedelta(hours=8))

# (名稱, 類別, symbol, exchange, currency)
UNIVERSE = [
    # 美股龍頭
    ("AAPL", "stock", "AAPL", "SMART", "USD"),
    ("NVDA", "stock", "NVDA", "SMART", "USD"),
    ("MSFT", "stock", "MSFT", "SMART", "USD"),
    ("GOOGL", "stock", "GOOGL", "SMART", "USD"),
    ("AMZN", "stock", "AMZN", "SMART", "USD"),
    ("META", "stock", "META", "SMART", "USD"),
    ("TSLA", "stock", "TSLA", "SMART", "USD"),
    # 指數
    ("SPY", "stock", "SPY", "SMART", "USD"),
    ("QQQ", "stock", "QQQ", "SMART", "USD"),
    # 港股對照
    ("700.HK", "stock", "700", "SEHK", "HKD"),
    ("9988.HK", "stock", "9988", "SEHK", "HKD"),
    ("2800.HK", "stock", "2800", "SEHK", "HKD"),
    # 指數現貨
    ("SPX", "index", "SPX", "CBOE", "USD"),
    ("NDX", "index", "NDX", "NASDAQ", "USD"),
    ("INDU", "index", "INDU", "CME", "USD"),
    ("VIX", "index", "VIX", "CBOE", "USD"),
    # 期貨（front month）
    ("ES", "future", "ES", "CME", "USD"),
    ("NQ", "future", "NQ", "CME", "USD"),
    ("YM", "future", "YM", "CBOT", "USD"),
    ("HSI", "future", "HSI", "HKFE", "HKD"),
    ("MHI", "future", "MHI", "HKFE", "HKD"),
]


def now_hkt():
    return datetime.now(HKT)


def log(msg):
    print(f"[{now_hkt():%H:%M:%S}] {msg}", flush=True)


def make_contract(ib, sec_type, symbol, exchange, currency):
    from ib_async import Stock, Index, Future

    if sec_type == "stock":
        return Stock(symbol, exchange, currency)
    if sec_type == "index":
        return Index(symbol, exchange, currency)
    if sec_type == "future":
        return Future(symbol, exchange=exchange, currency=currency)
    raise ValueError(f"未知類別: {sec_type}")


async def resolve_front_month(ib, name, exchange, currency):
    """期貨揀最近到期月份合約。"""
    from ib_async import Future

    details = await ib.reqContractDetailsAsync(Future(name, exchange=exchange, currency=currency))
    if not details:
        return None

    def yymm(c):
        ls = getattr(c, "localSymbol", "") or ""
        m = re.search(rf"^{name}(\d{{4,6}})", ls)
        if not m:
            return "9999"
        code = m.group(1)
        return code[-4:] if len(code) == 6 else code

    def ltd(d):
        c = getattr(d, "contract", d)
        v = getattr(c, "lastTradeDate", "") or ""
        return v[:8] if re.match(r"^\d{8}", v) else ""

    today_str = datetime.now(HKT).date().strftime("%Y%m%d")
    active = [d for d in details if ltd(d) >= today_str] or list(details)
    active.sort(key=ltd)
    return getattr(active[0], "contract", active[0])


async def fetch_one(ib, name, sec_type, symbol, exchange, currency, days, sem):
    async with sem:
        try:
            contract = make_contract(ib, sec_type, symbol, exchange, currency)
            front = None
            if sec_type == "future":
                front = await resolve_front_month(ib, symbol, exchange, currency)
                if front is None:
                    return name, None, "no front month"
                contract = front
            bars = await ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                barSizeSetting="1 day",
                durationStr=f"{days} D",
                useRTH=True,
                whatToShow="TRADES",
                formatDate=1,
            )
            if not bars:
                return name, None, "no bars"
            rows = []
            for b in bars:
                rows.append({
                    "date": str(b.date)[:10],
                    "symbol": name,
                    "sec_type": sec_type,
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": int(b.volume or 0),
                    "front_month": getattr(contract, "localSymbol", "") if sec_type == "future" else "",
                })
            return name, pd.DataFrame(rows), None
        except Exception as e:
            return name, None, str(e)


async def fetch_all(days):
    from ib_async import IB

    ib = IB()
    try:
        await asyncio.wait_for(ib.connectAsync(HOST, PORT, clientId=91, timeout=15), timeout=20)
    except Exception as e:
        return None, f"連接失敗: {e}"

    sem = asyncio.Semaphore(1)  # 歷史數據要求逐個走，避免 pacing
    tasks = [fetch_one(ib, n, t, s, e, c, days, sem) for (n, t, s, e, c) in UNIVERSE]
    results = await asyncio.gather(*tasks)
    ib.disconnect()
    return results, None


def merge_parquet(new_rows):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df_new = pd.concat(new_rows, ignore_index=True)
    if KLINE_PATH.exists():
        old = pd.read_parquet(KLINE_PATH)
        df = pd.concat([old, df_new], ignore_index=True)
    else:
        df = df_new
    df = df.drop_duplicates(subset=["date", "symbol"], keep="last")
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df.to_parquet(KLINE_PATH, index=False)
    return len(df_new), len(df)


def run_import(days=365):
    log(f"IB 數據匯入開始（{days} 日，{len(UNIVERSE)} 個標的）")
    results, err = asyncio.run(fetch_all(days))
    if err:
        log(f"✗ {err}")
        return {"status": "error", "error": err}

    ok, failed, frames = [], [], []
    for name, df, e in results:
        if df is None:
            failed.append(f"{name}:{e}")
            log(f"  ✗ {name:8s} {e}")
        else:
            ok.append(name)
            frames.append(df)
            log(f"  ✓ {name:8s} {len(df)} 行")

    if not frames:
        return {"status": "error", "error": "全部標的失敗", "failed": failed}

    new_rows, total = merge_parquet(frames)
    status = "ok" if not failed else "degraded"
    log(f"完成：{len(ok)} 成功 / {len(failed)} 失敗，新增 {new_rows} 行，總計 {total} 行")
    return {
        "status": status,
        "ok": ok,
        "failed": failed,
        "new_rows": new_rows,
        "total_rows": total,
        "parquet": str(KLINE_PATH),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=365)
    a = p.parse_args()
    out = run_import(days=a.days)
    print(out)
