#!/usr/bin/env python3
import json
import time
import os
import yfinance as yf
from datetime import datetime, timedelta
import subprocess

STOCKS_FILE = '/home/workspace/stock-analysis/dt_halfnew_stocks.txt'
RESULTS_FILE = '/home/workspace/stock-analysis/halfnew_results.json'

def test_stock(ticker):
    try:
        hist = yf.Ticker(f"{ticker}.HK").history(period="5y")
        if hist.empty or len(hist) < 200: return False
        
        max_high = hist['High'].max()
        three_months_ago = datetime.now() - timedelta(days=90)
        recent_hist = hist[hist.index >= three_months_ago.strftime('%Y-%m-%d')]
        
        if recent_hist.empty: return False
        
        recent_max = recent_hist['High'].max()
        recent_min = recent_hist['Low'].min()
        recent_avg = recent_hist['Close'].mean()
        
        drop_pct = (max_high - recent_avg) / max_high
        if drop_pct <= 0.70: return False
        
        volatility = (recent_max - recent_min) / recent_min
        if volatility > 0.50: return False
        
        return True
    except Exception as e:
        print(f"Error testing {ticker}: {e}")
        return False

def main():
    if not os.path.exists(STOCKS_FILE):
        print(f"File not found: {STOCKS_FILE}")
        return
        
    with open(STOCKS_FILE, 'r') as f:
        stocks = [line.strip() for line in f if line.strip()]
        
    results = []
    print(f"Starting scan of {len(stocks)} half-new stocks...")
    
    for i, stock in enumerate(stocks):
        if i > 0 and i % 10 == 0:
            print(f"Progress: {i}/{len(stocks)}")
            
        print(f"Testing {stock}...")
        if test_stock(stock):
            print(f"!!! MATCH FOUND: {stock} !!!")
            results.append({
                "stock_code": stock,
                "timestamp": time.time()
            })
            
            # Save incrementally
            with open(RESULTS_FILE, 'w') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
                
        time.sleep(1) # Rate limit
        
    print(f"Scan complete. Found {len(results)} matches.")

if __name__ == '__main__':
    main()
