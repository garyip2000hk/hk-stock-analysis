#!/usr/bin/env python3
"""
Daily Corporate Actions Scanner
Uses web sources to find recent HK corporate actions and updates cache.
"""
import json
import os
import sys
import re
from datetime import datetime, timedelta
from pathlib import Path

CACHE_DIR = Path(__file__).parent
CACHE_FILE = CACHE_DIR / "corp_actions_cache.json"

EVENT_KEYWORDS = {
    "rights_issue": ["供股", "RIGHT ISSUE", "RIGHTS ISSUE", "公開發售", "OPEN OFFER"],
    "placing": ["配售", "配股", "PLACING", "先舊後新", "TOP-UP PLACING"],
    "general_offer": ["要約", "全購", "強制性現金要約", "GENERAL OFFER", "MANDATORY OFFER", "收購要約"],
    "cb": ["可換股債券", "可轉換債券", "可轉債", "CONVERTIBLE BOND", "CB"],
    "buyback": ["回購", "BUYBACK", "SHARE REPURCHASE"],
    "consolidation": ["合股", "SHARE CONSOLIDATION", "STOCK CONSOLIDATION"],
    "split": ["拆股", "SHARE SPLIT", "STOCK SPLIT"],
    "bonus_issue": ["送紅股", "紅股", "BONUS ISSUE", "BONUS SHARE"],
    "privatization": ["私有化", "PRIVATIZATION"],
    "dividend": ["派息", "股息", "DIVIDEND"],
}

def scan_hkex_announcements_today():
    """Try to get today's HKEX announcements via HTTP."""
    import requests
    
    events = []
    try:
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Referer': 'https://www1.hkexnews.hk/',
        })
        
        url = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
        params = {
            'sortDir': '0', 'sortByOptions': 'DateTime',
            'category': '0', 'market': 'SEHK',
            'stockId': '', 'documentType': '-1',
            'fromDate': '', 'toDate': '', 'title': '',
            'searchType': '0', 't1code': '-2',
            't2Gcode': '-2', 't2code': '-2',
            'rowRange': '100', 'lang': 'ZH',
        }
        resp = session.get(url, params=params, timeout=30)
        data = resp.json()
        
        results_str = data.get('result', '[]')
        if isinstance(results_str, str):
            results = json.loads(results_str)
        else:
            results = results_str
            
        for row in results[:200]:
            title = row.get('TITLE', '')
            date_str = row.get('DATE', '')
            stock_code = row.get('STOCKCODE', '') or row.get('stockCode', '')
            
            action_types = []
            for action, keywords in EVENT_KEYWORDS.items():
                for kw in keywords:
                    if kw.lower() in title.lower():
                        action_types.append(action)
                        break
            
            if action_types:
                events.append({
                    'date': date_str,
                    'type': action_types[0],
                    'title': title.strip(),
                    'stock_code': stock_code,
                })
    except Exception as e:
        print(f"HKEX scan failed: {e}", file=sys.stderr)
    
    return events


def load_cache():
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_cache(data):
    with open(CACHE_FILE, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_cache(new_events, source="hkex_scan"):
    cache = load_cache()
    now = datetime.now().isoformat()
    added = 0
    
    # Group events by stock code
    for ev in new_events:
        stock = ev.get('stock_code', '').zfill(5)
        if not stock or stock == '00000':
            continue
        
        if stock not in cache:
            cache[stock] = []
        
        # Check for duplicates
        existing_titles = [e.get('title', '') for e in cache[stock]]
        if ev.get('title') in existing_titles:
            continue
        
        cache[stock].append({
            'date': ev.get('date', ''),
            'type': ev.get('type', 'general'),
            'title': ev.get('title', ''),
            'source': source,
            'collected_at': now,
        })
        added += 1
    
    if added > 0:
        save_cache(cache)
        print(f"Added {added} new events to cache")
    else:
        print("No new events to add")
    
    return added


def main():
    print(f"=== Corporate Actions Scanner ===")
    print(f"Time: {datetime.now().isoformat()}")
    
    events = scan_hkex_announcements_today()
    
    if events:
        added = update_cache(events)
        print(f"Scanned {len(events)} events, added {added}")
    else:
        print("No events found from HKEX scan")
    
    # Also print cache stats
    cache = load_cache()
    total_events = sum(len(v) for v in cache.values())
    print(f"Cache stats: {len(cache)} stocks, {total_events} total events")


if __name__ == '__main__':
    main()
