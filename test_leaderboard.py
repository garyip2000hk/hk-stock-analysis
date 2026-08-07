import duckdb
import ccass_snapshot as cs
import json
import pandas as pd
from datetime import datetime, date, timedelta

con = duckdb.connect()

quotes = json.load(open('imported/quotes.json'))
last_date = max(quotes['quotes'].keys())
latest_quotes = quotes['quotes'][last_date]
names = quotes['names']

issued_df = con.execute("SELECT LTRIM(stock_code, '0') as sc, MAX(shares) as shares FROM read_parquet(?) GROUP BY 1", [str(cs.ISSUED)]).df()
issued = {str(k).zfill(5): v for k, v in zip(issued_df.sc, issued_df.shares)}

ccass_df = con.execute("""
    SELECT 
        LTRIM(s.stock_code, '0') as sc, 
        d.at_date, 
        d.c5, 
        d.c10 
    FROM read_parquet(?) d
    JOIN read_parquet(?) s ON d.issue_id = s.issue_id
""", [cs._existing(cs.DAILYLOG_SOURCES), str(cs.SHORTNAMES)]).df()

ccass_df['sc'] = ccass_df['sc'].astype(str).str.zfill(5)
ccass_df = ccass_df.sort_values(['sc', 'at_date'])

print(ccass_df.head())
