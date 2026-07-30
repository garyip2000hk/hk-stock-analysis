#!/usr/bin/env python3
import json
import os
import sys
from datetime import datetime, timedelta

# Import existing functions from our project
sys.path.insert(0, '/home/workspace/stock-analysis')
from ccass_analyzer import fetch_stock, analyze, diff

CACHE_FILE = '/home/workspace/stock-analysis/leaderboard_cache.json'
STOCKS = ["00001", "00659", "08619", "08083", "08360", "01329", "08491", "08179", "01428", "06133"]

def build():
    print(f"[{datetime.now()}] Starting leaderboard build for {len(STOCKS)} stocks...")
    
    concentration_top = []
    movement_top = []
    
    date_now = datetime.now()
    while date_now.weekday() >= 5: date_now -= timedelta(days=1)
    
    # Approx 3 months ago for movement
    date_diff = date_now - timedelta(days=90)
    while date_diff.weekday() >= 5: date_diff -= timedelta(days=1)
    
    date_now_str = date_now.strftime('%Y%m%d')
    date_diff_str = date_diff.strftime('%Y%m%d')
    
    for stock in STOCKS:
        try:
            print(f"Fetching {stock}...")
            # Fetch latest for concentration
            data_now = fetch_stock(stock, date_now_str)
            if 'error' not in data_now:
                ana = analyze(data_now, top_n=5)
                if 'concentration' in ana:
                    concentration_top.append({
                        'stock_code': stock,
                        'top_5_pct': ana['concentration']['top_5'],
                        'top_10_pct': ana['concentration']['top_10']
                    })
            
            # Fetch past for movement
            data_past = fetch_stock(stock, date_diff_str)
            if 'error' not in data_now and 'error' not in data_past:
                d = diff(data_past, data_now, threshold=0.1)
                if 'changes' in d and len(d['changes']) > 0:
                    max_inc = max([ch['delta_percentage'] for ch in d['changes']])
                    movement_top.append({
                        'stock_code': stock,
                        'max_increase_pct': max_inc
                    })
        except Exception as e:
            print(f"Error on {stock}: {e}")

    # Sort
    concentration_top.sort(key=lambda x: x['top_5_pct'], reverse=True)
    movement_top.sort(key=lambda x: x['max_increase_pct'], reverse=True)
    
    result = {
        'last_updated': datetime.now().isoformat(),
        'concentration_top': concentration_top[:10],
        'movement_top': movement_top[:10]
    }
    
    with open(CACHE_FILE, 'w') as f:
        json.dump(result, f, indent=2)
        
    print(f"[{datetime.now()}] Leaderboard saved.")

if __name__ == '__main__':
    build()
