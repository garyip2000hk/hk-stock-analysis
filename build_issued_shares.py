#!/usr/bin/env python3
"""Extract issued-share counts from the HKEX equity JSON dumps into one parquet."""
import glob
import json
import os
import re
from pathlib import Path

BASE = Path(__file__).parent.parent / "Desktop" / "db" / "HKEXequity" / "equity"
OUT = Path(__file__).parent.parent / "Desktop" / "db" / "CCASS" / "issued_shares.parquet"

AMT = re.compile(r'"amt_os":"([\d,]+)"')
NAME = re.compile(r'"issuer_name":"([^"]*)"')
DATE = re.compile(r'"shares_issued_date":"([^"]*)"')


def latest_dir():
    dirs = sorted(d for d in BASE.iterdir() if d.is_dir() and d.name.isdigit())
    if not dirs:
        raise SystemExit(f"no equity snapshot dirs under {BASE}")
    return dirs[-1]


def main():
    src = latest_dir()
    rows = []
    for path in glob.glob(str(src / "*.json")):
        text = open(path, encoding="utf-8", errors="ignore").read()
        amt = AMT.search(text)
        if not amt:
            continue
        shares = int(amt.group(1).replace(",", ""))
        if shares <= 0:
            continue
        code = os.path.basename(path).split(".")[0].lstrip("0") or "0"
        name = NAME.search(text)
        asof = DATE.search(text)
        rows.append({
            "stock_code": code,
            "issued_shares": shares,
            "issuer_name": name.group(1) if name else None,
            "shares_issued_date": asof.group(1) if asof else None,
            "snapshot": src.name,
        })

    import duckdb
    with duckdb.connect() as con:
        con.execute("CREATE TABLE t (stock_code VARCHAR, issued_shares BIGINT, issuer_name VARCHAR, shares_issued_date VARCHAR, snapshot VARCHAR)")
        con.executemany(
            "INSERT INTO t VALUES (?,?,?,?,?)",
            [(r["stock_code"], r["issued_shares"], r["issuer_name"], r["shares_issued_date"], r["snapshot"]) for r in rows],
        )
        con.execute("COPY t TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(OUT)])
    print(json.dumps({"snapshot": src.name, "stocks": len(rows), "out": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
