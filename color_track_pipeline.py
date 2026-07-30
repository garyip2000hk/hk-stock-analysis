#!/usr/bin/env python3
"""Color tracking pipeline: fetch CCASS data for date range, run color tracker."""
import sys, json, os
sys.path.insert(0, os.path.dirname(__file__))
from ccass_analyzer import fetch_stock
from color_tracker import analyze_color_tracking

def main():
    if len(sys.argv) < 4:
        print(json.dumps({"error": "Usage: color_track_pipeline.py STOCK FROM TO"}))
        return

    stock = sys.argv[1].zfill(5)
    from_str = sys.argv[2]  
    to_str = sys.argv[3]

    # Parse dates
    from_date = from_str.replace('-', '/')
    to_date = to_str.replace('-', '/')
    
    # Check if from_date looks like YYYYMMDD without separators
    if len(from_str) == 8 and '/' not in from_str:
        from_date = f"{from_str[:4]}/{from_str[4:6]}/{from_str[6:]}"
    if len(to_str) == 8 and '/' not in to_str:
        to_date = f"{to_str[:4]}/{to_str[4:6]}/{to_str[6:]}"

    # Adjust to nearest trading day (skip weekends)
    def to_trading_day(d):
        while d.weekday() >= 5:  # Saturday=5, Sunday=6
            d = d - timedelta(days=1)
        return d
    
    from datetime import datetime, timedelta
    df = to_trading_day(datetime.strptime(from_date, "%Y/%m/%d"))
    dt = to_trading_day(datetime.strptime(to_date, "%Y/%m/%d"))

    days = max(1, (dt - df).days)
    # Use only 3 key dates (start, middle, end) to stay within API timeout (~60s total)
    mid = df + timedelta(days=days // 2)
    dates = [
        df.strftime("%Y/%m/%d"),
        mid.strftime("%Y/%m/%d"),
        dt.strftime("%Y/%m/%d"),
    ]
    # Deduplicate if any dates collapsed
    dates = list(dict.fromkeys(dates))

    # Cache
    cache_path = os.path.join(os.path.dirname(__file__), "ccass_snapshot_cache.json")
    cache = {}
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except:
        pass

    data_points = []
    for ds in dates:
        ck = f"{stock}_{ds}"
        if ck in cache:
            data = cache[ck]
        else:
            data = fetch_stock(stock, ds)
            cache[ck] = data

        if not data.get('error') and data.get('participants'):
            data_points.append({
                'date': ds,
                'stock_code': stock,
                'participants': data['participants']
            })

    with open(cache_path, 'w') as f:
        json.dump(cache, f, ensure_ascii=False)

    if len(data_points) < 2:
        print(json.dumps({"error": f"Not enough data points ({len(data_points)})"}, ensure_ascii=False))
        return

    result = analyze_color_tracking(data_points)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
