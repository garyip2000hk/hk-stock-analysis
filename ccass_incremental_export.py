#!/usr/bin/env python3
"""Export today's CCASS rows from the Windows MySQL database to Parquet."""
import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

try:
    import mysql.connector
except ImportError:
    mysql = None

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = pq = None

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "Desktop" / "db" / "CCASS"
TABLE_COLUMNS = {
    "holdings": ["partID", "issueID", "holding", "atDate"],
    "parthold": ["partID", "issueID", "holding", "atDate"],
    "dailylog": ["atDate", "issueID", "intermedHldg", "intermedCnt", "NCIPhldg", "NCIPcnt", "CIPhldg", "CIPcnt", "c5", "c10", "CustHldg", "BrokHldg"],
    "bigchanges": ["atDate", "issueID", "partID", "stkchg", "prevDate"],
}


def config_from_env():
    return {
        "host": os.getenv("CCASS_DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("CCASS_DB_PORT", "3306")),
        "user": os.getenv("CCASS_DB_USER", "root"),
        "password": os.getenv("CCASS_DB_PASSWORD", ""),
        "database": os.getenv("CCASS_DB_NAME", "CCASS"),
        "enigma_database": os.getenv("ENIGMA_DB_NAME", "enigma"),
    }


def connect(cfg):
    if mysql is None:
        raise RuntimeError("mysql-connector-python is not installed")
    return mysql.connector.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"],
        password=cfg["password"], database=cfg["database"],
        connection_timeout=20,
    )


def query_rows(conn, table, day):
    cols = TABLE_COLUMNS[table]
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join('`'+c+'`' for c in cols)} FROM `{table}` WHERE `atDate`=%s", (day,))
    rows = cur.fetchall()
    cur.close()
    return cols, rows


def write_parquet(path, columns, rows):
    if pa is None or pq is None:
        raise RuntimeError("pyarrow is not installed")
    fields = []
    arrays = []
    for i, name in enumerate(columns):
        vals = [r[i] for r in rows]
        if name in {"atDate", "prevDate"}:
            vals = [v.isoformat() if hasattr(v, "isoformat") else v for v in vals]
            fields.append((name, pa.string()))
        else:
            fields.append((name, pa.array(vals).type if vals else pa.string()))
        arrays.append(pa.array(vals, type=fields[-1][1]))
    table = pa.Table.from_arrays(arrays, names=columns)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)


def append_day(path, columns, rows):
    if not rows:
        return 0
    if path.exists():
        old = pq.read_table(path)
        new = pa.Table.from_pylist([dict(zip(columns, r)) for r in rows], schema=old.schema)
        combined = pa.concat_tables([old, new], promote_options="default")
    else:
        arrays = {c: [r[i] for r in rows] for i, c in enumerate(columns)}
        combined = pa.Table.from_pydict(arrays)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(combined, tmp, compression="zstd")
    tmp.replace(path)
    return len(rows)


def export(cfg, out, day, full_rewrite=False):
    out.mkdir(parents=True, exist_ok=True)
    conn = connect(cfg)
    counts = {}
    try:
        for table in TABLE_COLUMNS:
            columns, rows = query_rows(conn, table, day)
            target = out / f"{table}.parquet"
            if full_rewrite or not target.exists():
                write_parquet(target, columns, rows)
            else:
                append_day(target, columns, rows)
            counts[table] = len(rows)
    finally:
        conn.close()
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--full-rewrite", action="store_true")
    args = ap.parse_args()
    if not args.out.is_absolute():
        args.out = args.out.resolve()
    try:
        counts = export(config_from_env(), args.out, args.date, args.full_rewrite)
        print(json.dumps({"status": "ok", "date": args.date, "counts": counts, "out": str(args.out)}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
