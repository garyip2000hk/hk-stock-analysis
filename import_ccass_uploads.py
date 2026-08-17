#!/usr/bin/env python3
import csv
import json
import shutil
import tempfile
from datetime import date
from pathlib import Path
import duckdb

BASE = Path('/home/workspace/Desktop/db/CCASS')
INCOMING = Path('/home/workspace/incoming/ccass')
MANIFEST = BASE / 'manifest.json'
TABLES = {
    'holdings': ('holdings', ['part_id','issue_id','holding','at_date']),
    'parthold': ('parthold', ['part_id','issue_id','holding','at_date']),
    'dailylog': ('dailylog', ['at_date','issue_id','intermed_hldg','intermed_cnt','ncip_hldg','ncip_cnt','cip_hldg','cip_cnt','c5','c10','cust_hldg','brok_hldg']),
    'bigchanges': ('bigchanges', ['at_date','issue_id','part_id','stk_change','prev_date']),
    'quotes': ('quotes', ['issue_id','at_date','prev_close','closing','ask','bid','high','low','vol','turn','susp','newsusp','noclose']),
    'pquotes': ('pquotes', ['issue_id','at_date','prev_close','closing','ask','bid','high','low','vol','turn','susp','newsusp','noclose']),
    'shortnames': ('shortnames', ['issue_id','short_name','from_date','to_date','row_id','stock_code','use_date','stock_ex_id','parallel']),
}

def date_bounds(files):
    values=[]
    for path in files:
        with path.open(newline='', encoding='utf-8-sig') as fh:
            reader=csv.DictReader(fh)
            for row in reader:
                for key in ('at_date','use_date','from_date','to_date'):
                    value=(row.get(key) or '').strip()
                    if value and value not in ('0000-00-00','NULL'):
                        try: values.append(date.fromisoformat(value))
                        except ValueError: pass
    return min(values).isoformat() if values else None, max(values).isoformat() if values else None

def sql_path(path):
    return str(path).replace("'", "''")

def csv_table(path):
    with path.open(newline='', encoding='utf-8-sig') as fh:
        columns=[x.strip().strip('"') for x in next(csv.reader(fh), [])]
    return next((table for table, (_, expected) in TABLES.items() if columns == expected), None)

def csv_date_bounds(path):
    values=[]
    with path.open(newline='', encoding='utf-8-sig') as fh:
        for row in csv.DictReader(fh):
            for key in ('at_date','use_date','from_date','to_date'):
                value=(row.get(key) or '').strip()
                if value and value not in ('0000-00-00','NULL'):
                    try: values.append(date.fromisoformat(value))
                    except ValueError: pass
    return (min(values), max(values)) if values else (None, None)

def main():
    BASE.mkdir(parents=True, exist_ok=True)
    files=sorted(INCOMING.glob('ccass_*.csv'), key=lambda p: p.name)
    if not files:
        raise SystemExit('No uploaded CCASS CSV files found')
    recognized=[]
    ignored=[]
    for path in files:
        if path.stat().st_size <= 256:
            ignored.append({'file':path.name,'reason':'small/empty'})
            continue
        table=csv_table(path)
        if table:
            recognized.append((table,path))
        else:
            ignored.append({'file':path.name,'reason':'unrecognized header'})
    if not recognized:
        raise SystemExit('No recognized CCASS CSV files found')

    run_id=date.today().isoformat()
    counts={}
    added={}
    duplicates={}
    coverage={}
    with tempfile.TemporaryDirectory(dir=BASE) as td_name:
        td=Path(td_name)
        con=duckdb.connect()
        try:
            for table,(source,expected) in TABLES.items():
                matches=[p for t,p in recognized if t==table]
                if not matches:
                    continue
                csv_rel=[]
                for i,path in enumerate(matches):
                    view=f'upload_{table}_{i}'
                    con.execute(f"CREATE OR REPLACE TEMP VIEW {view} AS SELECT * FROM read_csv_auto('{sql_path(path)}', header=true, nullstr=['NULL','\\N'], union_by_name=true)")
                    csv_rel.append(view)
                union=' UNION ALL BY NAME '.join(f'SELECT * FROM {v}' for v in csv_rel)
                stage=td/f'{table}_stage.parquet'
                con.execute(f"COPY (SELECT * FROM ({union}) WHERE true) TO '{sql_path(stage)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
                final=BASE/f'{table}.parquet'
                backup=BASE/f'{table}.parquet.before_merge_{run_id}'
                if final.exists() and not backup.exists():
                    shutil.copy2(final, backup)
                key=', '.join(expected)
                source_union=f"SELECT * FROM read_parquet('{sql_path(stage)}')"
                if final.exists():
                    existing=f"SELECT * FROM read_parquet('{sql_path(final)}')"
                    combined=f"SELECT * FROM ({existing} UNION ALL BY NAME {source_union})"
                else:
                    combined=source_union
                dedup=td/f'{table}.parquet'
                existing_part = f"SELECT *, 0 AS source_rank, '' AS source_file FROM ({existing})" if final.exists() else "SELECT *, 0 AS source_rank, '' AS source_file FROM (SELECT * FROM read_parquet('missing')) WHERE false"
                con.execute(f"COPY (SELECT * EXCLUDE (__rn, source_rank, source_file) FROM (SELECT *, row_number() OVER (PARTITION BY {key} ORDER BY source_rank, source_file DESC) AS __rn FROM ({existing_part} UNION ALL SELECT *, 1 AS source_rank, '' AS source_file FROM ({source_union}))) WHERE __rn=1) TO '{sql_path(dedup)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
                after=con.execute(f"SELECT COUNT(*) FROM read_parquet('{sql_path(dedup)}')").fetchone()[0]
                raw=con.execute(f"SELECT COUNT(*) FROM read_parquet('{sql_path(stage)}')").fetchone()[0]
                if final.exists():
                    prior_keys=f"SELECT {key} FROM read_parquet('{sql_path(final)}')"
                    new_keys=f"SELECT {key} FROM read_parquet('{sql_path(stage)}') EXCEPT SELECT {key} FROM read_parquet('{sql_path(final)}')"
                    new_count=con.execute(f"SELECT COUNT(*) FROM ({new_keys})").fetchone()[0]
                    dup_count=raw-new_count
                else:
                    new_count=after; dup_count=raw-after
                shutil.move(dedup, final)
                counts[table]=after
                added[table]=new_count
                duplicates[table]=max(0,dup_count)
                d1,d2=csv_date_bounds(matches[0]) if len(matches)==1 else (None,None)
                all_dates=[]
                for path in matches:
                    a,b=csv_date_bounds(path)
                    if a: all_dates.append(a)
                    if b: all_dates.append(b)
                coverage[table]=[min(all_dates).isoformat(),max(all_dates).isoformat()] if all_dates else [None,None]
        finally:
            con.close()
    old=json.loads(MANIFEST.read_text(encoding='utf-8-sig')) if MANIFEST.exists() else {}
    old_range=old.get('range') or [None,None]
    all_ranges=[x for x in [old_range[0],*([v[0] for v in coverage.values() if v[0]])] if x]
    all_ends=[x for x in [old_range[1],*([v[1] for v in coverage.values() if v[1]])] if x]
    old.update({'range':[min(all_ranges) if all_ranges else None,max(all_ends) if all_ends else None], 'uploaded_range':[min(v[0] for v in coverage.values() if v[0]),max(v[1] for v in coverage.values() if v[1])], 'updated_at':date.today().isoformat(), 'source':'EC2 MySQL CCASS CSV upload; merge-preserving importer','tables':{**old.get('tables',{}),**counts},'format':'Parquet Zstandard','last_import':{'run_id':run_id,'files':len(recognized),'added_rows':added,'duplicate_rows_ignored':duplicates,'coverage':coverage,'ignored':ignored}})
    MANIFEST.write_text(json.dumps(old,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'ok','uploaded_files':len(recognized),'counts':counts,'added_rows':added,'duplicate_rows_ignored':duplicates,'coverage':coverage,'manifest':str(MANIFEST)},ensure_ascii=False,indent=2))
if __name__=='__main__': main()
