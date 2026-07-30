#!/usr/bin/env python3
"""
CCASS Multi-Stock Analyzer + Corporate Actions Detector
Usage:
  python3 ccass_analyzer.py stock 00001 --top 20
  python3 ccass_analyzer.py stock 00001 --diff 20260701
  python3 ccass_analyzer.py topchanges --stocks 00001,00659,08619
  python3 ccass_analyzer.py corpactions 00001 --months 12
"""
import requests
from bs4 import BeautifulSoup
import json
import re
import argparse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import plotly.graph_objects as go
import plotly.io as pio

SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
})

CCASS_URL = "https://www3.hkexnews.hk/sdw/search/searchsdw.aspx"
HKEXNEWS_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"

# Known financial engineering event types from HKEX headline categories
CORP_ACTION_KEYWORDS = {
    'rights_issue': ['供股', 'RIGHTS ISSUE', 'RIGHT ISSUE', '公開發售'],
    'placing': ['配售', 'PLACING', 'PLACEMENT', '先舊後新', 'TOP-UP'],
    'convertible_bond': ['可換股債券', 'CONVERTIBLE BOND', 'CB', '可換股票據'],
    'general_offer': ['全面收購', 'GENERAL OFFER', 'MANDATORY OFFER', '強制性收購'],
    'privatization': ['私有化', 'PRIVATISATION', 'PRIVATIZATION'],
    'share_consolidation': ['合併股份', '股份合併', 'SHARE CONSOLIDATION', 'CONSOLIDATION'],
    'share_split': ['拆細', '股份拆細', 'SHARE SPLIT', 'SUBDIVISION'],
    'bonus_issue': ['紅股', 'BONUS ISSUE', '紅利', '送紅股'],
    'buyback': ['回購', 'SHARE REPURCHASE', 'BUY-BACK', 'REPURCHASE'],
    'transfer_board': ['轉主板', 'GEM TRANSFER', 'TRANSFER OF LISTING'],
}

def _get_ccass_form(html):
    soup = BeautifulSoup(html, 'html.parser')
    fields = {}
    for name in ['__VIEWSTATE', '__VIEWSTATEGENERATOR', '__EVENTVALIDATION', 'today']:
        el = soup.find('input', {'name': name})
        if el: fields[name] = el['value']
    return fields

def _cell_val(cell):
    body = cell.find('div', class_='mobile-list-body')
    return body.get_text(strip=True) if body else cell.get_text(strip=True)

def fetch_stock(stock_code, date_str=None, max_retries=5):
    start_d = None
    if date_str is None:
        start_d = datetime.now()
    else:
        clean = date_str.replace('-', '').replace('/', '')
        if len(clean) == 8:
            try:
                start_d = datetime.strptime(clean, '%Y%m%d')
            except:
                start_d = datetime.now()
        else:
            start_d = datetime.now()

    for attempt in range(max_retries):
        d = start_d - timedelta(days=attempt)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
            start_d = d  # keep sync
        
        current_date_str = d.strftime('%Y/%m/%d')
        
        try:
            resp = SESSION.get(CCASS_URL, timeout=30)
            fields = _get_ccass_form(resp.text)
            form = {**fields, 'txtShareholdingDate': current_date_str, 'txtStockCode': stock_code, 'btnSearch': '搜尋'}
            
            resp = SESSION.post(CCASS_URL, data=form, timeout=30)
            soup = BeautifulSoup(resp.text, 'html.parser')

            err = soup.find(id='lblErrorMsg')
            if err and err.text.strip():
                if attempt == max_retries - 1:
                    return {'error': err.text.strip(), 'stock_code': stock_code, 'date': current_date_str}
                continue # try previous day

            table = soup.find('table', class_='table-mobile-list')
            if not table:
                if attempt == max_retries - 1:
                    return {'error': 'No data', 'stock_code': stock_code, 'date': current_date_str}
                continue # try previous day

            participants = []
            for row in table.find_all('tr')[1:]:
                cells = row.find_all('td')
                if len(cells) < 4: continue
                
                id_val = _cell_val(cells[0])
                name = _cell_val(cells[1])
                addr = _cell_val(cells[2])
                shares_str = _cell_val(cells[3]).replace(',', '').strip()
                percent_str = _cell_val(cells[4]).replace('%', '').strip()
                
                try:
                    shares = int(shares_str)
                    percent = float(percent_str)
                except ValueError:
                    continue
                    
                participants.append({
                    'id': id_val,
                    'name': name,
                    'shares': shares,
                    'percentage': percent
                })
                
            if not participants:
                if attempt == max_retries - 1:
                    return {'error': 'No participant data', 'stock_code': stock_code, 'date': current_date_str}
                continue

            summary = soup.find('div', class_='ccass-search-summary')
            total_shares = 0
            if summary:
                nums = re.findall(r'[\d,]+', summary.text)
                if nums:
                    total_shares = int(nums[-1].replace(',', ''))
                    
            return {
                'date': current_date_str,
                'stock_code': stock_code,
                'total_participants': len(participants),
                'total_shares': total_shares,
                'participants': participants
            }
        except Exception as e:
            if attempt == max_retries - 1:
                return {'error': str(e), 'stock_code': stock_code, 'date': current_date_str}

def analyze(data, top_n=20):
    if 'error' in data: return data
    p = data.get('participants', [])
    if not p: return {'error': 'No data'}
    total = sum(x['shares'] for x in p)
    return {
        'date': data['date'], 'stock_code': data['stock_code'],
        'total_participants': len(p), 'total_shares': total,
        'concentration': {
            'top_5': round(sum(x['percentage'] for x in p[:5]), 2),
            'top_10': round(sum(x['percentage'] for x in p[:10]), 2),
            'top_20': round(sum(x['percentage'] for x in p[:20]), 2),
        },
        'top_holders': [{'rank': i+1, 'id': x['id'], 'name': x['name'],
                          'shares': x['shares'], 'percentage': x['percentage']}
                         for i, x in enumerate(p[:top_n])],
    }

def diff(old_data, new_data, threshold=0.1):
    if 'error' in old_data or 'error' in new_data:
        return {'error': 'Data error', 'changes': [], 'significant_changes': 0}
    prev = {x['id']: x for x in old_data.get('participants', [])}
    curr = {x['id']: x for x in new_data.get('participants', [])}
    changes = []
    for pid in set(list(prev.keys()) + list(curr.keys())):
        po = prev.get(pid, {'shares': 0, 'percentage': 0.0, 'name': 'N/A'})
        pn = curr.get(pid, {'shares': 0, 'percentage': 0.0, 'name': po.get('name', 'N/A')})
        ds = pn['shares'] - po['shares']
        dp = round(pn['percentage'] - po['percentage'], 3)
        if abs(dp) >= threshold or (po['shares'] == 0 and pn['shares'] > 0):
            changes.append({
                'id': pid, 'name': pn.get('name') or po.get('name'),
                'shares_before': po['shares'], 'shares_after': pn['shares'],
                'delta_shares': ds, 'pct_before': po['percentage'],
                'pct_after': pn['percentage'], 'delta_pct': dp,
            })
    changes.sort(key=lambda x: abs(x['delta_pct']), reverse=True)
    return {'date_before': old_data['date'], 'date_after': new_data['date'],
            'changes': changes, 'significant_changes': len(changes)}

def top_changes(stocks, days_back=7):
    """Rank biggest CCASS changes across multiple stocks."""
    all_changes = []
    d2 = datetime.now()
    while d2.weekday() >= 5: d2 -= timedelta(days=1)
    d1 = d2 - timedelta(days=days_back)
    
    for stock in stocks:
        try:
            data_old = fetch_stock(stock, d1.strftime('%Y%m%d'))
            data_new = fetch_stock(stock, d2.strftime('%Y%m%d'))
            result = diff(data_old, data_new, threshold=0.2)
            for ch in result.get('changes', []):
                ch['stock_code'] = stock
                ch['date_range'] = f"{d1.strftime('%m/%d')}-{d2.strftime('%m/%d')}"
                all_changes.append(ch)
        except Exception as e:
            all_changes.append({'stock_code': stock, 'error': str(e)})

    # Sort by absolute percentage change
    all_changes.sort(key=lambda x: abs(x.get('delta_pct', 0)), reverse=True)
    return all_changes[:30]

def corp_actions(stock_code, months=12):
    """Read corporate actions from local cache file."""
    
    # Normalize stock code to 5 digits
    code = str(stock_code).zfill(5)
    
    # Find cache file relative to this script
    import os as _os
    _cache_dir = _os.path.dirname(_os.path.abspath(__file__))
    _cache_path = _os.path.join(_cache_dir, 'corp_actions_cache.json')
    
    if not _os.path.exists(_cache_path):
        return []
    
    try:
        with open(_cache_path, 'r', encoding='utf-8') as f:
            cache = json.load(f)
    except Exception:
        return []
    
    events = cache.get(code, [])
    
    # Filter by months if needed
    if months and months < 120:
        cutoff = datetime.now() - timedelta(days=months * 30)
        events = [e for e in events if e.get('date', '') and e['date'] >= cutoff.strftime('%Y-%m-%d')]
    
    return events

def generate_chart(data, stock_code, output_path):
    """Generate a Plotly chart for CCASS concentration."""
    holders = data.get('top_holders', [])[:15]
    if not holders: return None

    names = [f"{h['name'][:25]}" for h in holders]
    pcts = [h['percentage'] for h in holders]

    fig = go.Figure(data=[
        go.Bar(x=pcts[::-1], y=names[::-1], orientation='h',
               marker=dict(color=pcts[::-1], colorscale='Blues', showscale=False),
               text=[f"{p}%" for p in pcts[::-1]], textposition='outside',
               textfont=dict(color='#94a3b8', size=11))
    ])
    fig.update_layout(
        title=dict(text=f'CCASS Top {len(holders)} 券商持股 — {stock_code}', 
                    font=dict(color='#e2e8f0', size=16)),
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        xaxis=dict(title='', showgrid=True, gridcolor='rgba(255,255,255,0.05)',
                    color='#64748b'),
        yaxis=dict(color='#94a3b8'),
        margin=dict(l=10, r=80, t=50, b=10),
        height=500,
    )
    pio.write_image(fig, output_path, format='png', scale=2)
    return output_path


# CLI
def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd')

    sp = sub.add_parser('stock')
    sp.add_argument('stock_code')
    sp.add_argument('--date')
    sp.add_argument('--diff')
    sp.add_argument('--after')
    sp.add_argument('--top', type=int, default=20)

    tc = sub.add_parser('topchanges')
    tc.add_argument('--stocks', default='')
    tc.add_argument('--days', type=int, default=7)

    ca = sub.add_parser('corpactions')
    ca.add_argument('stock_code')
    ca.add_argument('--months', type=int, default=12)

    args = parser.parse_args()

    if args.cmd == 'stock':
        stock_code = args.stock_code.zfill(5)
        
        if args.diff:
            after_date = getattr(args, 'after', None)
            date_a = args.diff
            date_b = after_date if after_date else datetime.now().strftime('%Y/%m/%d')
            
            # Fetch both dates in parallel to speed up
            with ThreadPoolExecutor(max_workers=2) as executor:
                future_a = executor.submit(fetch_stock, stock_code, date_a)
                future_b = executor.submit(fetch_stock, stock_code, date_b)
                
                data_a = future_a.result(timeout=30)
                data_b = future_b.result(timeout=30)
            
            result = diff(data_a, data_b)
            result['analysis_before'] = analyze(data_a, top_n=args.top)
            result['analysis_after'] = analyze(data_b, top_n=args.top)
        else:
            data = fetch_stock(stock_code, args.date)
            result = analyze(data, top_n=args.top)

        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.cmd == 'topchanges':
        stocks = [s.strip() for s in args.stocks.split(',') if s.strip()]
        if not stocks:
            print(json.dumps({'error': 'No stocks provided'}, ensure_ascii=False))
            return
        result = top_changes(stocks, args.days)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.cmd == 'corpactions':
        events = corp_actions(args.stock_code, args.months)
        print(json.dumps({'stock_code': args.stock_code, 'events': events,
                           'total': len(events)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
