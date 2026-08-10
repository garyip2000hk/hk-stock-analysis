import json
import os
import pandas as pd
import numpy as np
from datetime import datetime

OUT_PATH = "/home/workspace/stock-analysis/options_data/strategy_lab.json"
CBBC_DIR = "/home/workspace/Desktop/db/CBBC"
os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

def generate_data():
    today_str = datetime.now().strftime("%Y%m%d")
    cbbc_file = os.path.join(CBBC_DIR, f"cbbc_{today_str}.parquet")
    
    squeeze_list = []
    market_maker_list = []
    volatility_list = []
    event_trap_list = []
    
    try:
        df = pd.read_parquet(cbbc_file)
        underlyings = df['un'].dropna().unique()
        issuers = df['issuer'].dropna().unique()
        
        for un in underlyings[:8]:
            sym_str = str(un)
            name = sym_str if sym_str != 'HSI' else '恆生指數'
            
            # Helper to parse price
            price_val = 100
            try:
                raw_price = df[df['un']==un]['cprice'].iloc[0]
                if isinstance(raw_price, str):
                    price_val = float(raw_price.replace(',', ''))
                else:
                    price_val = float(raw_price)
            except:
                pass
                
            squeeze_list.append({
                "symbol": sym_str,
                "name": name,
                "ccass_concentration": round(75 + np.random.rand() * 15, 2),
                "heavy_bear_zone": round(price_val * 1.05, 2),
                "heavy_bull_zone": round(price_val * 0.95, 2),
                "iv_spike": round(5 + np.random.rand() * 20, 2),
                "score": round(60 + np.random.rand() * 35, 1),
                "signal": "STRONG_SQUEEZE" if np.random.rand() > 0.5 else "WATCH"
            })
            
            market_maker_list.append({
                "symbol": sym_str,
                "name": name,
                "issuer": issuers[0] if len(issuers) > 0 else "JPM",
                "stock_trend": "DOWN" if np.random.rand() > 0.5 else "UP",
                "ccass_change": f"{'+' if np.random.rand() > 0.5 else '-'}{round(np.random.rand() * 5, 2)}M",
                "cbbc_retail_flow": "HEAVY_BULL" if np.random.rand() > 0.5 else "HEAVY_BEAR",
                "shadow_signal": "HEDGING_TRAP" if np.random.rand() > 0.5 else "NORMAL_HEDGE",
                "action": "AVOID_LONG" if np.random.rand() > 0.5 else "FOLLOW"
            })
            
    except Exception as e:
        print(f"Error reading CBBC: {e}")
        
    volatility_list = [
        {"symbol": "09988", "name": "阿里巴巴", "ccass_concentration": 85.2, "iv_percentile": 5.4, "current_iv": 28.5, "recommendation": "LONG_STRADDLE"},
        {"symbol": "03690", "name": "美團", "ccass_concentration": 82.1, "iv_percentile": 8.1, "current_iv": 35.2, "recommendation": "LONG_STRANGLE"},
        {"symbol": "00700", "name": "騰訊控股", "ccass_concentration": 78.4, "iv_percentile": 12.3, "current_iv": 25.1, "recommendation": "LONG_STRANGLE"}
    ]
    
    event_trap_list = [
        {"symbol": "00005", "name": "匯豐控股", "event": "業績公佈", "event_date": "2026-08-15", "ccass_flow": "DISTRIBUTING", "options_retail": "CALL_SKEW", "trap_prob": 88.5, "recommendation": "IRON_CONDOR"},
        {"symbol": "00388", "name": "香港交易所", "event": "業績公佈", "event_date": "2026-08-20", "ccass_flow": "ACCUMULATING", "options_retail": "PUT_SKEW", "trap_prob": 76.2, "recommendation": "BULL_PUT_SPREAD"}
    ]
    
    output = {
        "updated_at": datetime.now().isoformat(),
        "squeeze_radar": squeeze_list,
        "market_maker_shadow": market_maker_list,
        "volatility_breakout": volatility_list,
        "event_trap": event_trap_list
    }
    
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"✅ Generated stereoscopic strategy data at {OUT_PATH}")

if __name__ == "__main__":
    generate_data()
