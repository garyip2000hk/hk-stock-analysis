#!/usr/bin/env python3
import json
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
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
    sn_df = con.execute("SELECT issue_id, LTRIM(stock_code, '0') as sc, short_name, use_date FROM read_parquet(?)", [str(cs.SHORTNAMES)]).df()
    sn_df = sn_df[sn_df['issue_id'].astype(str).str.isdigit()]
    sn_df['issue_id'] = sn_df['issue_id'].astype(int)
    # Stock codes get recycled: per code, the issue with the latest use_date
    # is the currently listed company. Older issues on the same code belong to
    # delisted predecessors whose CCASS rows linger in the dailylog.
    sn_df = sn_df.sort_values('use_date', na_position='first')
    issue_to_sc = {int(row['issue_id']): row['sc'] for _, row in sn_df.iterrows()}
    issue_to_name = sn_df.groupby('issue_id')['short_name'].last().to_dict()
    active_issue_by_sc = {row['sc']: int(row['issue_id']) for _, row in sn_df.groupby('sc').tail(1).iterrows()}
    
    # 3. Compute top 50 concentration
    concentration_top = []
    skipped_cap = 0
    skipped_etp = 0
    
    # Market cap 來源：quotes.json 全歷史「逐隻行返轉頭」搵最近一次有價嗰日。
    # 唔可以淨係讀最後一日——最後一日有機會係 stub（得幾隻股），
    # 而且停牌股喺停牌期間根本唔會出現喺每日報價度。
    quote_names = {}
    latest_close = {}   # code -> (date, close)
    try:
        with open(QUOTES_FILE, 'r') as f:
            q_data = json.load(f)
            quote_names = q_data.get('names', {})
            for d in sorted(q_data.get('quotes', {}).keys(), reverse=True):
                for code, rec in q_data['quotes'][d].items():
                    c = rec.get('close')
                    if c and code not in latest_close:
                        latest_close[code] = (d, c)
    except Exception as e:
        print(f"Failed to load quotes: {e}")

    # ETF／L&I（ETP）排除：HKEXequity 快照有 product_type，只留 EQTY + REIT。
    # 搵最新有嘢嗰個日期 folder；ETP 名單好少變，過咗同步日都夠用。
    etp_codes = set()
    eq_root = Path('/home/workspace/Desktop/db/HKEXequity/equity')
    try:
        for ddir in sorted(eq_root.iterdir(), reverse=True):
            files = list(ddir.glob('*.json'))
            if not files:
                continue
            for f in files:
                try:
                    raw = f.read_text(encoding='utf-8').strip()
                    if raw.startswith('('):
                        raw = raw[1:raw.rfind(')')]
                    pt = json.loads(raw)['data']['quote'].get('product_type')
                    if pt == 'ETP':
                        etp_codes.add(f"{int(f.stem):05d}")
                except Exception:
                    continue
            break
    except Exception as e:
        print(f"Failed to load ETP list: {e}")
    print(f"ETP codes loaded: {len(etp_codes)}")

    def mcap_fields(full_sc, issued, ccass_name):
        # 名：HKEX 每日報價 short name 優先（乾淨），CCASS 後備（要剷走合股/供股後綴）
        name = quote_names.get(full_sc) or re.sub(r'-(NEW|[A-Z]?\d+[KM]?)$', '', ccass_name or '')
        q = latest_close.get(full_sc)
        mc = 0
        market_cap_str = ""
        if q:
            mc = q[1] * issued
            if mc >= 1e12:
                market_cap_str = f"{mc/1e12:.1f}兆"
            elif mc >= 1e11:
                market_cap_str = f"{mc/1e11:.1f}千億"
            elif mc >= 1e10:
                market_cap_str = f"{mc/1e10:.1f}百億"
            elif mc >= 1e8:
                market_cap_str = f"{mc/1e8:.1f}億"
        return name, market_cap_str, mc
        
    for _, row in df_latest.iterrows():
        iid = row['issue_id']
        sc = issue_to_sc.get(iid)
        if not sc or sc not in issued_map:
            continue
        # skip rows from old companies sitting on a recycled stock code
        if active_issue_by_sc.get(sc) != int(iid):
            continue
        # skip ETF / leveraged & inverse products
        if sc.zfill(5) in etp_codes:
            skipped_etp += 1
            continue
            
        issued = issued_map[sc]
        c5 = row['c5']
        
        # fallback cap: CCASS c5 can never exceed issued shares; if it does,
        # issued_shares is stale/wrong (e.g. ETF units created after the snapshot)
        if issued <= 0 or c5 > issued:
            skipped_cap += 1
            continue
        
        c5_pct = (c5 / issued * 100)
        
        # Format stock code
        full_sc = sc.zfill(5)
        
        name, market_cap_str, mc = mcap_fields(full_sc, issued, issue_to_name.get(iid, ""))
            
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
    skipped_movement = 0
    for _, row in df_latest.iterrows():
        iid = row['issue_id']
        sc = issue_to_sc.get(iid)
        if not sc or sc not in issued_map:
            continue
        if active_issue_by_sc.get(sc) != int(iid):
            continue
        # skip ETF / leveraged & inverse products
        if sc.zfill(5) in etp_codes:
            skipped_etp += 1
            continue
            
        issued = issued_map[sc]
        c5_now = row['c5']
        c5_past = past_vals.get(iid)
        
        if c5_past is not None and issued > 0:
            # same integrity guard as concentration
            if c5_now > issued or c5_past > issued:
                skipped_movement += 1
                continue
            pct_now = c5_now / issued * 100
            pct_past = c5_past / issued * 100
            delta_pct = pct_now - pct_past
            
            # exclude abnormal > 100% changes
            if delta_pct > 0 and delta_pct < 100:
                full_sc = sc.zfill(5)
                name, market_cap_str, mc = mcap_fields(full_sc, issued, issue_to_name.get(iid, ""))
                        
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
        'last_updated': datetime.now(ZoneInfo('Asia/Hong_Kong')).isoformat(),
        'source': 'Local CCASS (Auto)',
        'concentration': {
            'updated_at': datetime.now(ZoneInfo('Asia/Hong_Kong')).isoformat(),
            'rows': concentration_top
        },
        'big_investor_movement': {
            'updated_at': datetime.now(ZoneInfo('Asia/Hong_Kong')).isoformat(),
            'rows': movement_top
        }
    }
    
    with open(CACHE_FILE, 'w') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
        
    if skipped_cap or skipped_movement:
        print(f"[{datetime.now()}] Data integrity: skipped {skipped_cap} concentration row(s), {skipped_movement} movement row(s) where CCASS c5 exceeded issued shares; {skipped_etp} ETP row(s).")
    print(f"[{datetime.now()}] Leaderboard saved in {round(time.time() - start, 2)}s.")

if __name__ == '__main__':
    build()
