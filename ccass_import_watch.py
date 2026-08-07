#!/usr/bin/env python3
import hashlib
import json
import subprocess
import time
from pathlib import Path

INCOMING = Path('/home/workspace/incoming/ccass')
STATE = INCOMING / '.import_state.json'
IMPORTER = Path('/home/workspace/stock-analysis/import_ccass_uploads.py')
LOG = Path('/home/workspace/stock-analysis/ccass_import_watch.log')
INTERVAL = 60

def signature():
    h = hashlib.sha256()
    for p in sorted(INCOMING.glob('ccass_*.csv')):
        st = p.stat()
        h.update(f'{p.name}:{st.st_size}:{st.st_mtime_ns}'.encode())
    return h.hexdigest()

def log(message):
    line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + message
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')
    print(line, flush=True)

def main():
    last = ''
    if STATE.exists():
        try: last = json.loads(STATE.read_text()).get('signature', '')
        except Exception: pass
    log('CCASS import watcher started')
    while True:
        current = signature()
        if current != last:
            log('New or changed CCASS upload files detected; starting merge')
            result = subprocess.run(['python3', str(IMPORTER)], text=True, capture_output=True)
            if result.returncode == 0:
                last = current
                STATE.write_text(json.dumps({'signature': last, 'imported_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}, indent=2))
                log('CCASS merge completed: ' + result.stdout.strip().replace('\n', ' '))
            else:
                log('CCASS merge failed: ' + (result.stderr or result.stdout).strip().replace('\n', ' '))
        time.sleep(INTERVAL)

if __name__ == '__main__':
    main()
