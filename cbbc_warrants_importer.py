import pandas as pd
import os
import time
from datetime import datetime
import requests

CBBC_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz9vg_FuDr_MTwdtV7gLWbhSgtasdZ1z3_dbqk7rdb7WnK1eOrFiafLv99BXHf7fjEN2a7i4HpdWJ5/pub?gid=1621636669&single=true&output=csv"
WARRANTS_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz9vg_FuDr_MTwdtV7gLWbhSgtasdZ1z3_dbqk7rdb7WnK1eOrFiafLv99BXHf7fjEN2a7i4HpdWJ5/pub?gid=2101557690&single=true&output=csv"
SPOT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz9vg_FuDr_MTwdtV7gLWbhSgtasdZ1z3_dbqk7rdb7WnK1eOrFiafLv99BXHf7fjEN2a7i4HpdWJ5/pub?gid=1031962744&single=true&output=csv"

CBBC_DIR = "/home/workspace/Desktop/db/CBBC"
WARRANTS_DIR = "/home/workspace/Desktop/db/Warrants"
SPOT_DIR = "/home/workspace/Desktop/db/Spot"
os.makedirs(CBBC_DIR, exist_ok=True)
os.makedirs(WARRANTS_DIR, exist_ok=True)
os.makedirs(SPOT_DIR, exist_ok=True)

def sync_data():
    today_str = datetime.now().strftime("%Y%m%d")
    print(f"[{datetime.now()}] Fetching CBBC data...")
    try:
        cbbc_df = pd.read_csv(CBBC_URL)
        cbbc_path = os.path.join(CBBC_DIR, f"cbbc_{today_str}.parquet")
        cbbc_df.to_parquet(cbbc_path, index=False)
        print(f"✅ Saved CBBC data: {len(cbbc_df)} rows to {cbbc_path}")
    except Exception as e:
        print(f"❌ Failed to fetch CBBC: {e}")

    print(f"[{datetime.now()}] Fetching Warrants data...")
    try:
        warrants_df = pd.read_csv(WARRANTS_URL)
        warrants_path = os.path.join(WARRANTS_DIR, f"warrants_{today_str}.parquet")
        warrants_df.to_parquet(warrants_path, index=False)
        print(f"✅ Saved Warrants data: {len(warrants_df)} rows to {warrants_path}")
    except Exception as e:
        print(f"❌ Failed to fetch Warrants: {e}")
        
        print(f"[{datetime.now()}] Fetching Spot data from CSV...")
    try:
        spot_df = pd.read_csv(SPOT_URL)
        spot_path = os.path.join(SPOT_DIR, f"spot_{today_str}.parquet")
        spot_df.to_parquet(spot_path, index=False)
        print(f"✅ Saved Spot data: {len(spot_df)} rows to {spot_path}")
    except Exception as e:
        print(f"❌ Failed to fetch Spot data: {e}")

if __name__ == "__main__":
    sync_data()
