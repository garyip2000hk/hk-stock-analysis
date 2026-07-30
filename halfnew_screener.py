import yfinance as yf
from datetime import datetime, timedelta
import json
import os
from ccass_analyzer import fetch_stock, diff

def check_price_action(stock_code):
    """Check if stock dropped 70%+ from peak and moved sideways for 9 months."""
    try:
        ticker = f"{int(stock_code):04d}.HK"
        stock = yf.Ticker(ticker)
        
        # Get data since IPO (approx 3 years max for half-new)
        hist = stock.history(period="3y")
        if hist.empty or len(hist) < 200: # Need at least ~9 months of data
            return False, "Not enough data"
            
        all_time_high = hist['High'].max()
        
        # 9 months ago date
        nine_months_ago = datetime.now() - timedelta(days=270)
        recent_hist = hist[hist.index >= nine_months_ago.strftime('%Y-%m-%d')]
        
        if recent_hist.empty:
            return False, "No recent data"
            
        recent_high = recent_hist['High'].max()
        recent_low = recent_hist['Low'].min()
        current_price = recent_hist['Close'].iloc[-1]
        
        # Condition 1: Dropped 70%+ from peak
        drop_pct = (all_time_high - current_price) / all_time_high
        if drop_pct < 0.70:
            return False, f"Drop only {drop_pct:.1%}, needs 70%+"
            
        # Condition 2: Sideways for 9 months 
        # (e.g. Recent High is not more than 60% above Recent Low, or similar logic)
        # Let's use a standard sideways metric: max is within 1.6x of min
        sideways_ratio = recent_high / recent_low if recent_low > 0 else 999
        if sideways_ratio > 1.8:
            return False, f"Too volatile in last 9m (ratio {sideways_ratio:.2f})"
            
        return True, {
            "ath": float(all_time_high),
            "current": float(current_price),
            "drop_pct": float(drop_pct),
            "recent_high": float(recent_high),
            "recent_low": float(recent_low)
        }
    except Exception as e:
        return False, str(e)

def check_ccass_accumulation(stock_code):
    """Check if CCASS shows accumulation over last 9 months."""
    try:
        nine_months_ago = datetime.now() - timedelta(days=270)
        while nine_months_ago.weekday() >= 5: 
            nine_months_ago -= timedelta(days=1)
            
        d1_str = nine_months_ago.strftime('%Y/%m/%d')
        
        old_data = fetch_stock(stock_code, d1_str)
        new_data = fetch_stock(stock_code) # Today
        
        if 'error' in old_data or 'error' in new_data:
            return False, "CCASS data missing"
            
        diff_data = diff(old_data, new_data, threshold=0.5)
        
        # Calculate accumulation
        changes = diff_data.get('changes', [])
        
        positive_changes = sum([c['delta_pct'] for c in changes if c['delta_pct'] > 0])
        
        if positive_changes >= 2.0: # At least 2% net accumulation by top players
            top_accumulators = [c for c in changes if c['delta_pct'] > 0][:3]
            return True, {
                "accumulated_pct": positive_changes,
                "top_accumulators": top_accumulators
            }
        return False, f"Accumulation too low: {positive_changes:.2f}%"
        
    except Exception as e:
        return False, str(e)

if __name__ == '__main__':
    # Test with a stock
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "01428"
    print(f"Testing {code}...")
    p_pass, p_res = check_price_action(code)
    print(f"Price Action: {p_pass} - {p_res}")
    if p_pass or code == "01428": # force CCASS test
        c_pass, c_res = check_ccass_accumulation(code)
        print(f"CCASS Accumulation: {c_pass} - {c_res}")
