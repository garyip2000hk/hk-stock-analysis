#!/usr/bin/env python3
"""
CCASS Shareholding Scraper for HKEX
Fetches CCASS participant shareholding data, concentration analysis, and date diff.
Usage:
  python3 ccass_scraper.py 00001 --analyze --top 10
  python3 ccass_scraper.py 00001 --diff 20260701 --analyze --top 10
"""
import requests
from bs4 import BeautifulSoup
import json
import re
import argparse
from datetime import datetime, timedelta

SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
})

BASE_URL = "https://www3.hkexnews.hk/sdw/search/searchsdw.aspx"


def _get_form_fields(html):
    soup = BeautifulSoup(html, 'html.parser')
    fields = {}
    for el in soup.select('input[type="hidden"][name]'):
        fields[el['name']] = el.get('value', '')
    return fields


def _clean_prefix(text, prefix_pattern):
    return re.sub(r'^' + prefix_pattern + r'[:,]?\s*', '', text)


def fetch_ccass(stock_code, date_str=None):
    """Fetch CCASS data for a stock on a given date."""
    if date_str is None:
        d = datetime.now()
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        date_str = d.strftime('%Y/%m/%d')
    elif len(date_str) == 8 and '/' not in date_str:
        date_str = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"

    # Step 1: GET the page
    resp = SESSION.get(BASE_URL, timeout=30)
    fields = _get_form_fields(resp.text)
    if resp.url != BASE_URL:
        base_url = resp.url
    else:
        base_url = BASE_URL

    # Step 2: POST with search params
    form_data = {
        **fields,
        'txtShareholdingDate': date_str,
        'txtStockCode': str(stock_code).zfill(5),
        'txtStockName': '',
        'txtParticipantID': '',
        'txtParticipantName': '',
        'txtSelPartID': '',
        '__EVENTTARGET': 'btnSearch',
        '__EVENTARGUMENT': '',
    }
    resp = SESSION.post(base_url, data=form_data, headers={'Referer': base_url}, timeout=30)

    return _parse_ccass(resp.text, stock_code, date_str)


def _cell_value(cell):
    """Extract the actual value from a mobile-list-body div inside a td."""
    body = cell.find('div', class_='mobile-list-body')
    if body:
        return body.get_text(strip=True)
    return cell.get_text(strip=True)


def _parse_ccass(html, stock_code, date_str):
    soup = BeautifulSoup(html, 'html.parser')

    alert = soup.find(id='alertMsg')
    if alert and alert.get('value', '').strip():
        return {'error': alert.get('value').strip(), 'stock_code': stock_code, 'date': date_str}

    table = soup.find('table', class_='table-mobile-list')
    if not table:
        return {'error': 'No data found', 'stock_code': stock_code, 'date': date_str}

    rows = table.find_all('tr')
    participants = []

    def labelled_value(cell):
        text = cell.get_text(' ', strip=True)
        if ':' in text:
            text = text.split(':', 1)[1]
        return text.strip()

    for row in rows[1:]:
        cells = row.find_all('td')
        if len(cells) < 5:
            continue

        pid = labelled_value(cells[0])
        name_raw = labelled_value(cells[1])
        name = name_raw.replace('*', '').strip()
        consent = '*' in cells[1].get_text(' ', strip=True)
        address = labelled_value(cells[2])
        shares_str = labelled_value(cells[3]).replace(',', '')
        pct_str = labelled_value(cells[4]).replace('%', '').strip()
        try:
            shares = int(shares_str)
        except (ValueError, TypeError):
            shares = 0
        try:
            pct = float(pct_str)
        except (ValueError, TypeError):
            pct = 0.0

        participants.append({
            'participant_id': pid or 'N/A',
            'name': name or 'N/A',
            'consent': consent,
            'address': address or '',
            'shares': shares,
            'percentage': pct,
        })

    return {
        'stock_code': stock_code,
        'date': date_str,
        'participants': participants,
        'total_participants': len(participants),
    }


def analyze(data, top_n=20):
    """Analyze concentration from CCASS data."""
    if 'error' in data:
        return {'error': data['error']}

    participants = data.get('participants', [])
    if not participants:
        return {'error': 'No participant data'}

    total_shares = sum(p['shares'] for p in participants)

    top_5_pct = sum(p['percentage'] for p in participants[:5])
    top_10_pct = sum(p['percentage'] for p in participants[:10])
    top_20_pct = sum(p['percentage'] for p in participants[:20])

    return {
        'date': data['date'],
        'stock_code': data['stock_code'],
        'total_participants': len(participants),
        'total_shares_in_ccass': total_shares,
        'concentration': {
            'top_5': round(top_5_pct, 2),
            'top_10': round(top_10_pct, 2),
            'top_20': round(top_20_pct, 2),
        },
        'top_holders': [
            {
                'rank': i + 1,
                'id': p['participant_id'],
                'name': p['name'],
                'shares': p['shares'],
                'percentage': p['percentage'],
            }
            for i, p in enumerate(participants[:top_n])
        ],
    }


def diff(data_old, data_new, threshold_pct=0.1):
    """Compare two dates, find significant changes in participant holdings."""
    if 'error' in data_old:
        return {'error': 'Previous date error: ' + data_old['error']}
    if 'error' in data_new:
        return {'error': 'Current date error: ' + data_new['error']}

    prev = {p['participant_id']: p for p in data_old.get('participants', [])}
    curr = {p['participant_id']: p for p in data_new.get('participants', [])}

    changes = []
    all_ids = set(list(prev.keys()) + list(curr.keys()))

    for pid in all_ids:
        p_old = prev.get(pid, {'shares': 0, 'percentage': 0.0, 'name': 'N/A'})
        p_new = curr.get(pid, {'shares': 0, 'percentage': 0.0,
                                'name': p_old.get('name', 'N/A')})

        delta_shares = p_new['shares'] - p_old['shares']
        delta_pct = round(p_new['percentage'] - p_old['percentage'], 3)

        if abs(delta_pct) >= threshold_pct or (
                p_old['shares'] == 0 and p_new['shares'] > 0):
            changes.append({
                'participant_id': pid,
                'name': p_new.get('name') or p_old.get('name', 'N/A'),
                'shares_before': p_old['shares'],
                'shares_after': p_new['shares'],
                'delta_shares': delta_shares,
                'percentage_before': p_old['percentage'],
                'percentage_after': p_new['percentage'],
                'delta_percentage': delta_pct,
            })

    changes.sort(key=lambda x: abs(x['delta_percentage']), reverse=True)

    return {
        'date_before': data_old['date'],
        'date_after': data_new['date'],
        'stock_code': data_new['stock_code'],
        'changes': changes,
        'significant_changes': len(changes),
    }


def main():
    parser = argparse.ArgumentParser(description='CCASS Shareholding Scraper & Analyzer')
    parser.add_argument('stock_code', help='HKEX stock code (e.g. 00001)')
    parser.add_argument('--date', help='Date in YYYYMMDD format')
    parser.add_argument('--diff', help='Compare date (YYYYMMDD) vs --date')
    parser.add_argument('--analyze', action='store_true', help='Show concentration analysis')
    parser.add_argument('--top', type=int, default=20, help='Top N holders')
    parser.add_argument('--threshold', type=float, default=0.1,
                        help='Min pct change threshold for diff')
    args = parser.parse_args()

    data_a = fetch_ccass(args.stock_code, args.date)

    if args.diff:
        data_b = fetch_ccass(args.stock_code, args.diff)
        result = diff(data_b, data_a, threshold_pct=args.threshold)
        if args.analyze:
            result['analysis'] = analyze(data_b)
            result['analysis_current'] = analyze(data_a)
    elif args.analyze:
        result = analyze(data_a, top_n=args.top)
    else:
        result = data_a

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
