import re
import json

def parse_halfnew():
    with open('/tmp/dt_halfnew_text.txt', 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    data = []
    # Find the header line index
    start_idx = 0
    for i, line in enumerate(lines):
        if line.startswith("代號\t名稱"):
            start_idx = i + 1
            break
            
    if start_idx == 0:
        return
        
    for line in lines[start_idx:]:
        if '.hk' not in line:
            if "AI 分析" in line:
                break
            continue
            
        parts = line.strip().split('\t')
        if len(parts) >= 4:
            stock_code = parts[0].replace('.hk', '')
            name = parts[1]
            ipo_price = parts[2]
            list_date = parts[3]
            
            data.append({
                'stock_code': stock_code,
                'name': name,
                'ipo_price': ipo_price,
                'list_date': list_date,
                'raw_line': line.strip()
            })
            
    with open('/home/workspace/stock-analysis/halfnew_cache.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        
    print(f"Saved {len(data)} semi-new stocks.")

if __name__ == '__main__':
    parse_halfnew()
