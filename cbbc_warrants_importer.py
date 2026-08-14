import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

CBBC_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz9vg_FuDr_MTwdtV7gLWbhSgtasdZ1z3_dbqk7rdb7WnK1eOrFiafLv99BXHf7fjEN2a7i4HpdWJ5/pub?gid=1621636669&single=true&output=csv"
WARRANTS_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz9vg_FuDr_MTwdtV7gLWbhSgtasdZ1z3_dbqk7rdb7WnK1eOrFiafLv99BXHf7fjEN2a7i4HpdWJ5/pub?gid=2101557690&single=true&output=csv"
SPOT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz9vg_FuDr_MTwdtV7gLWbhSgtasdZ1z3_dbqk7rdb7WnK1eOrFiafLv99BXHf7fjEN2a7i4HpdWJ5/pub?gid=1031962744&single=true&output=csv"

CBBC_DIR = "/home/workspace/Desktop/db/CBBC"
WARRANTS_DIR = "/home/workspace/Desktop/db/Warrants"
SPOT_DIR = "/home/workspace/Desktop/db/Spot"
DOWNLOAD_ATTEMPTS = 3
INITIAL_BACKOFF_SECONDS = 2


def find_latest_snapshot(directory: str | Path, prefix: str) -> Path | None:
    """Return the latest valid parquet snapshot for a dataset, if one exists."""
    paths = sorted(Path(directory).glob(f"{prefix}_*.parquet"))
    return paths[-1] if paths else None


def read_csv_with_retry(
    url: str,
    label: str,
    reader: Callable[[str], Any] = pd.read_csv,
    attempts: int = DOWNLOAD_ATTEMPTS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Any:
    """Download a CSV with bounded exponential backoff for temporary upstream failures."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return reader(url)
        except Exception as error:
            last_error = error
            if attempt == attempts:
                break
            delay = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(f"⚠️ {label} download failed (attempt {attempt}/{attempts}): {error}; retrying in {delay}s")
            sleep_fn(delay)
    raise RuntimeError(f"{label} download failed after {attempts} attempts: {last_error}") from last_error


def sync_dataset(url: str, label: str, directory: str, prefix: str, today_str: str) -> dict[str, object]:
    """Save a fresh dataset or retain the newest valid local snapshot on failure."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    try:
        dataframe = read_csv_with_retry(url, label)
        output_path = Path(directory) / f"{prefix}_{today_str}.parquet"
        dataframe.to_parquet(output_path, index=False)
        result = {
            "status": "fresh",
            "rows": len(dataframe),
            "path": str(output_path),
            "source_date": today_str,
        }
        print(f"✅ Saved {label} data: {len(dataframe)} rows to {output_path}")
        return result
    except Exception as error:
        fallback = find_latest_snapshot(directory, prefix)
        if fallback:
            snapshot_date = fallback.stem.removeprefix(f"{prefix}_")
            print(f"⚠️ {label} download unavailable: {error}. Reusing latest snapshot {fallback.name}.")
            return {
                "status": "stale",
                "rows": None,
                "path": str(fallback),
                "source_date": snapshot_date,
                "error": str(error),
            }
        print(f"❌ {label} download unavailable and no valid local snapshot exists: {error}")
        return {
            "status": "failed",
            "rows": None,
            "path": None,
            "source_date": None,
            "error": str(error),
        }


def sync_data() -> dict[str, dict[str, object]]:
    """Synchronize CBBC, warrants and spot data without discarding usable local snapshots."""
    today_str = datetime.now().strftime("%Y%m%d")
    print(f"[{datetime.now()}] Fetching CBBC, warrants and spot data...")
    return {
        "cbbc": sync_dataset(CBBC_URL, "CBBC", CBBC_DIR, "cbbc", today_str),
        "warrants": sync_dataset(WARRANTS_URL, "Warrants", WARRANTS_DIR, "warrants", today_str),
        "spot": sync_dataset(SPOT_URL, "Spot", SPOT_DIR, "spot", today_str),
    }


if __name__ == "__main__":
    sync_data()
