#!/usr/bin/env python3
import json
import time
from datetime import datetime, timedelta, date
import duckdb
import pandas as pd

import ccass_snapshot as cs

CACHE_FILE = '/home/workspace/stock-analysis/leaderboard_cache.json'
QUOTES_FILE = '/home/workspace/stock-analysis/imported/quotes.json'

def build():
    start = time.time()
    print(f"[{datetime.now()}] Starting leaderboard build...")
    
    con = duckdb.connect()
    
    # 1. Get issued shares for all stocks
    issued_df = con.execute("SELECT LTRIM(stock_code, '0') as sc, MAX(issued_shares) as shares FROM read_parquet(?) GROUP BY 1", [str(cs.ISSUED)]).df()
    issued_map = {row['sc']: int(row['shares']) for _, row in issued_df.iterrows() if row['shares']}
    
    # 2. Get latest CCASS data for all stocks (c5, c10)
    # Get latest date
    latest_date_res = con.execute("SELECT MAX(at_date) FROM read_parquet(?)", [cs._existing(cs.DAILYLOG_SOURCES)]).fetchone()
    if not latest_date_res or not latest_date_res[0]:
        print("No ccass dailylog found.")
        return
    latest_date = latest_date_res[0]
    
    # Approx 30 days ago for movement
    past_date = latest_date - timedelta(days=30)
    
    df_latest = con.execute("""
        SELECT issue_id, c5, c10 
        FROM read_parquet(?) 
        WHERE at_date = ?
    """, [cs._existing(cs.DAILYLOG_SOURCES), latest_date]).df()
    
    # We need to map issue_id to stock_code
    sn_df = con.execute("SELECT issue_id, LTRIM(stock_code, '0') as sc, short_name FROM read_parquet(?)", [str(cs.SHORTNAMES)]).df()
    issue_to_sc = {int(row['issue_id']): row['sc'] for _, row in sn_df.iterrows() if str(row['issue_id']).isdigit()}
    issue_to_name = {int(row['issue_id']): row['short_name'] for _, row in sn_df.iterrows() if str(row['issue_id']).isdigit()}
    
    # 3. Compute top 50 concentration
    concentration_top = []
    
    # Add market cap from quotes.json if available
    quotes = {}
    try:
        with open(QUOTES_FILE, 'r') as f:
            q_data = json.load(f)
            last_q_date = sorted(q_data['quotes'].keys())[-1]
            quotes = q_data['quotes'][last_q_date]
    except Exception as e:
        print(f"Failed to load quotes: {e}")
        
    for _, row in df_latest.iterrows():
        iid = row['issue_id']
        sc = issue_to_sc.get(iid)
        if not sc or sc not in issued_map:
            continue
            
        issued = issued_map[sc]
        c5 = row['c5']
        
        c5_pct = (c5 / issued * 100) if issued > 0 else 0
        
        # Format stock code
        full_sc = sc.zfill(5)
        
        name = issue_to_name.get(iid, "")
        
        # Calculate market cap
        market_cap_str = ""
        mc = 0
        q = quotes.get(full_sc)
        if q and q.get('close'):
            mc = q['close'] * issued
            if mc >= 1e12:
                market_cap_str = f"{mc/1e12:.1f}兆"
            elif mc >= 1e11:
                market_cap_str = f"{mc/1e11:.1f}千億"
            elif mc >= 1e10:
                market_cap_str = f"{mc/1e10:.1f}百億"
            elif mc >= 1e8:
                market_cap_str = f"{mc/1e8:.1f}億"
        else:
            # Fake a high market cap if no quotes, or 0
            pass
            
        concentration_top.append({
            "stock_code": full_sc,
            "name": name,
            "top_5_pct": round(c5_pct, 2),
            "market_cap": market_cap_str,
            "mc_val": mc
        })
        
    # Sort concentration
    concentration_top.sort(key=lambda x: x['top_5_pct'], reverse=True)
    
    # 4. Compute movement
    df_past = con.execute("""
        SELECT issue_id, MAX(at_date) as at_date
        FROM read_parquet(?) 
        WHERE at_date <= ?
        GROUP BY 1
    """, [cs._existing(cs.DAILYLOG_SOURCES), past_date]).df()
    
    # Get actual values for those past dates
    past_vals = {}
    for _, row in df_past.iterrows():
        v = con.execute("""
            SELECT c5 FROM read_parquet(?) WHERE issue_id = ? AND at_date = ?
        """, [cs._existing(cs.DAILYLOG_SOURCES), row['issue_id'], row['at_date']]).fetchone()
        if v:
            past_vals[row['issue_id']] = v[0]
            
    movement_top = []
    for _, row in df_latest.iterrows():
        iid = row['issue_id']
        sc = issue_to_sc.get(iid)
        if not sc or sc not in issued_map:
            continue
            
        issued = issued_map[sc]
        c5_now = row['c5']
        c5_past = past_vals.get(iid)
        
        if c5_past is not None and issued > 0:
            pct_now = c5_now / issued * 100
            pct_past = c5_past / issued * 100
            delta_pct = pct_now - pct_past
            
            # exclude abnormal > 100% changes
            if delta_pct > 0 and delta_pct < 100:
                full_sc = sc.zfill(5)
                name = issue_to_name.get(iid, "")
                mc = 0
                market_cap_str = ""
                q = quotes.get(full_sc)
                if q and q.get('close'):
                    mc = q['close'] * issued
                    if mc >= 1e12:
                        market_cap_str = f"{mc/1e12:.1f}兆"
                    elif mc >= 1e11:
                        market_cap_str = f"{mc/1e11:.1f}千億"
                    elif mc >= 1e10:
                        market_cap_str = f"{mc/1e10:.1f}百億"
                    elif mc >= 1e8:
                        market_cap_str = f"{mc/1e8:.1f}億"
                        
                movement_top.append({
                    "stock_code": full_sc,
                    "name": name,
                    "delta_pct": round(delta_pct, 2),
                    "market_cap": market_cap_str,
                    "mc_val": mc
                })
                
    movement_top.sort(key=lambda x: x['delta_pct'], reverse=True)
    
    # 5. Build final result
    # Front-end filters by market_cap_yi < 50
    # To keep it compatible, we provide what they need
    result = {
        'last_updated': datetime.now().isoformat(),
        'source': 'Local CCASS (Auto)',
        'concentration': {
            'updated_at': datetime.now().isoformat(),
            'rows': concentration_top
        },
        'big_investor_movement': {
            'updated_at': datetime.now().isoformat(),
            'rows': movement_top
        }
    }
    
    with open(CACHE_FILE, 'w') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
        
    print(f"[{datetime.now()}] Leaderboard saved in {round(time.time() - start, 2)}s.")

if __name__ == '__main__':
    build()
