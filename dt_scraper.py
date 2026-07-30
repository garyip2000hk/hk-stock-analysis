import subprocess
import time
import json
import re

URLS = {
    'rights_issue': 'https://disclosuretracker.com/rights_issue',
    'placing': 'https://disclosuretracker.com/placing',
    'general_offer': 'https://disclosuretracker.com/general_offer',
    'stock_consolidation': 'https://disclosuretracker.com/stock_consolidation',
    'stock_split': 'https://disclosuretracker.com/stock_split',
    'convertible_bonds': 'https://disclosuretracker.com/convertible_bonds'
}

def get_text(url):
    print(f"Loading {url}...")
    subprocess.run(['agent-browser', 'open', url], check=True)
    time.sleep(4)
    res = subprocess.run(['agent-browser', 'get', 'text', 'body'], capture_output=True, text=True)
    return res.stdout

def extract_events(text, event_type):
    events = []
    lines = text.split('\n')
    
    current_code = None
    for i, line in enumerate(lines):
        # Look for pattern like 08483.hk or 09978.hk
        m = re.match(r'^0?(\d{4,5})\.hk', line.strip())
        if m:
            code = m.group(1).zfill(5)
            # The next few lines contain the data depending on the table structure
            name = lines[i+1].strip() if i+1 < len(lines) else ""
            date = lines[i+2].strip() if i+2 < len(lines) else ""
            
            event = {
                'stock_code': code,
                'type': event_type,
                'title': f"{name} {event_type} ({date})",
                'date': date
            }
            
            # Extract additional fields based on type
            # ... we will just store raw strings and let ccass_analyzer map them later
            events.append(event)
            print(f"Found {event_type} for {code}: {name} ({date})")
            
    return events

def main():
    print("Starting DT scraper...")
    # Assume already logged in from earlier
    all_events = []
    for k, url in URLS.items():
        text = get_text(url)
        events = extract_events(text, k)
        all_events.extend(events)
        
    print(f"Total events found: {len(all_events)}")
    
    # Update cache
    cache_file = 'corp_actions_cache.json'
    try:
        with open(cache_file, 'r') as f:
            cache = json.load(f)
    except Exception:
        cache = {}
        
    added = 0
    for e in all_events:
        code = e['stock_code']
        if code not in cache:
            cache[code] = []
            
        # Check duplicate
        exists = any(x['date'] == e['date'] and x['type'] == e['type'] for x in cache[code])
        if not exists:
            cache[code].append(e)
            added += 1
            
    with open(cache_file, 'w') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
        
    print(f"Added {added} new events to cache.")

if __name__ == '__main__':
    main()
