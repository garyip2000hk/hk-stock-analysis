import yfinance as yf
import numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import json, sys

def calc_range_pct(hist):
    if len(hist) < 20: return float('inf')
    recent = hist.iloc[-63:]  # ~3 months
    if len(recent) < 10: return float('inf')
    high = recent.High.max()
    low = recent.Low.min()
    return (high - low) / ((high + low) / 2) * 100

def check_consolidation(hist, threshold=15):
    rp = calc_range_pct(hist)
    return rp, rp <= threshold

def calc_ma_trend(hist):
    if len(hist) < 20: return "noise"
    recent = hist.iloc[-20:]
    sma5 = recent.Close.rolling(5).mean().iloc[-1]
    sma10 = recent.Close.rolling(10).mean().iloc[-1]
    if sma5 > sma10: return "up"
    elif sma5 < sma10: return "down"
    return "flat"

THRESHOLD = 15.0  # %
period = "1y"

data = json.load(open('/home/workspace/stock-analysis/dt_halfnew_raw.json'))
results = []
total = len(data)
print(f"Scanning {total} stocks...\n")

processed = 0
for row in data:
    processed += 1
    code = row.get('stock_code', '')
    pct = row.get('change_pct', 0)
    if pct is None or pct > -70: continue
    
    ticker = f"{code}.HK"
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=period)
        if len(hist) < 20: continue
        
        range_3m, is_narrow = check_consolidation(hist, THRESHOLD)
        trend = calc_ma_trend(hist)
        avg_vol = hist.Volume.mean()
        last_vol = hist.Volume.iloc[-1] if len(hist) > 0 else 0
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 0
        
        results.append({
            'stock_code': code,
            'name': row.get('name', ''),
            'ipo_price': row.get('ipo_price', ''),
            'change_pct': pct,
            'range_3m_pct': round(range_3m, 1),
            'is_narrow_3m': is_narrow,
            'trend_20d': trend,
            'vol_ratio': round(vol_ratio, 2),
            'data_points': len(hist)
        })
        print(f"[{processed}/{total}] {code} {pct:+.1f}% | 3m range: {range_3m:.1f}% | narrow: {is_narrow}")
    except:
        continue

# sort: narrow range first, then by biggest drop
results.sort(key=lambda x: (not x['is_narrow_3m'], x['range_3m_pct']))

print(f"\n=== RESULTS: {len(results)} stocks with OHLC data ===")
narrow = [r for r in results if r['is_narrow_3m']]
print(f"Narrow consolidation (3m < {THRESHOLD}%): {len(narrow)}")
print(f"Wide range: {len(results) - len(narrow)}")

# save
with open('/home/workspace/stock-analysis/ohlc_results.json', 'w') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print("\nTop narrow consolidation candidates:")
for r in narrow[:15]:
    print(f"  {r['stock_code']} {r['name']:15s} drop:{r['change_pct']:+.1f}% range_3m:{r['range_3m_pct']:.1f}% trend:{r['trend_20d']}")

print(f"\nSaved to ohlc_results.json")
