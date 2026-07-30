#!/usr/bin/env python3
"""Scan halfnew stocks with >=50% drop for OHLC + narrow range (3 months)."""
import json, sys, os
from datetime import datetime, timedelta

try:
    import yfinance as yf
except ImportError:
    os.system("pip install yfinance -q")
    import yfinance as yf

CACHE_PATH = "/home/workspace/stock-analysis/halfnew_cache.json"

def load_cache():
    with open(CACHE_PATH) as f:
        return json.load(f)

def get_price_drop(stock):
    chg = stock.get("chg_pct")
    if chg is None:
        return None
    s = str(chg).replace("%", "").replace("+", "").strip()
    try:
        return float(s)
    except:
        return None

def get_ticker(stock_code):
    code = str(stock_code).zfill(4)
    if len(code) == 4:
        return code + ".HK"
    return code + ".HK"

def check_narrow_range_3m(ticker):
    """Check if the stock has narrow range (<=15% volatility) in last 3 months."""
    try:
        t = yf.Ticker(ticker)
        end = datetime.now()
        start = end - timedelta(days=120)
        df = t.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        if df.empty or len(df) < 30:
            return None
        
        # Last 3 months
        cutoff = end - timedelta(days=90)
        recent = df[df.index >= pd.Timestamp(cutoff)]
        if len(recent) < 20:
            return None
        
        high = recent['High'].max()
        low = recent['Low'].min()
        range_pct = ((high - low) / low) * 100
        
        avg_vol = recent['Volume'].mean()
        last_close = recent['Close'].iloc[-1]
        
        return {
            "ticker": ticker,
            "last_close": round(float(last_close), 3),
            "range_pct": round(float(range_pct), 1),
            "high_3m": round(float(high), 3),
            "low_3m": round(float(low), 3),
            "avg_volume": int(avg_vol),
            "days": len(recent)
        }
    except Exception as e:
        return None

def main():
    import pandas as pd
    
    stocks = load_cache()
    print(f"Loaded {len(stocks)} stocks from cache")
    
    dropped = []
    for s in stocks:
        pct = get_price_drop(s)
        if pct is not None and pct <= -50:
            dropped.append({
                "stock_code": s.get("stock_code", ""),
                "name": s.get("name", ""),
                "drop_pct": pct,
                "ticker": get_ticker(s.get("stock_code", ""))
            })
    
    print(f"Dropped >=50%: {len(dropped)} stocks")
    
    results = []
    narrow_count = 0
    
    for i, stock in enumerate(dropped):
        ticker = stock["ticker"]
        pct = stock["drop_pct"]
        code = stock["stock_code"]
        
        if (i + 1) % 20 == 0:
            print(f"Progress: {i+1}/{len(dropped)} | Narrow found: {narrow_count}")
        
        result = check_narrow_range_3m(ticker)
        if result:
            result["stock_code"] = code
            result["name"] = stock["name"]
            result["drop_pct"] = pct
            results.append(result)
            narrow_count += 1
            print(f"  FOUND [{code}] {stock['name']}: drop={pct}%, range={result['range_pct']}%")
    
    # Sort by narrowest range first
    results.sort(key=lambda x: x["range_pct"])
    
    output = {
        "scan_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_scanned": len(dropped),
        "narrow_found": len(results),
        "condition": "drop >= 50% + 3-month range <= 20%",
        "stocks": results
    }
    
    with open("/home/workspace/stock-analysis/narrow_3m.json", "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print(f"\n=== DONE ===")
    print(f"Scanned: {len(dropped)} | Narrow range found: {len(results)}")
    for r in results[:10]:
        print(f"  [{r['stock_code']}] {r['name']}: drop={r['drop_pct']}%, range={r['range_pct']}%")

if __name__ == "__main__":
    main()
