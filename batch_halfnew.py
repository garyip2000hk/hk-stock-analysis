import json
import time
import os
import subprocess

STOCKS_FILE = '/home/workspace/stock-analysis/dt_halfnew_stocks.txt'
RESULTS_FILE = '/home/workspace/stock-analysis/halfnew_results.json'

def load_results():
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def save_results(results):
    with open(RESULTS_FILE, 'w') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

def main():
    with open(STOCKS_FILE, 'r') as f:
        stocks = [line.strip() for line in f if line.strip()]
    
    results = load_results()
    processed = {r['stock_code'] for r in results}
    
    print(f"Total stocks: {len(stocks)}, already processed: {len(processed)}")
    
    for stock in stocks:
        if stock in processed:
            continue
            
        print(f"Testing {stock}...")
        try:
            # We use a mocked/simplified check here for speed if the real one takes too long,
            # but ideally we just call halfnew_screener.py or run its logic.
            # For demonstration, we'll run it and parse the output.
            res = subprocess.run(['python3', '/home/workspace/stock-analysis/halfnew_screener.py', stock], 
                               capture_output=True, text=True, timeout=30)
            
            output = res.stdout
            if "🚨 極佳洗倉收集形態" in output or "True" in output: # if it passes or partially passes
                # parse JSON from output if available, or just save the stock
                results.append({
                    "stock_code": stock,
                    "timestamp": time.time(),
                    "status": "Potential match found"
                })
                save_results(results)
                
        except Exception as e:
            print(f"Error on {stock}: {e}")
            
        time.sleep(2) # rate limit

if __name__ == '__main__':
    main()
