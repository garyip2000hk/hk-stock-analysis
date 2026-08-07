import json
from pathlib import Path

BASE = Path(__file__).parent.parent / "Desktop" / "db" / "CCASS"
MANIFEST_FILE = BASE / "manifest.json"
INCREMENTAL_MANIFEST_FILE = BASE / "incremental" / "manifest.json"


def available():
    return any(BASE.glob("*.parquet")) or any((BASE / "incremental").glob("*.parquet"))


def _table_range(table):
    import duckdb
    try:
        with duckdb.connect() as con:
            row = con.execute(
                "SELECT MIN(at_date), MAX(at_date), COUNT(*) FROM read_parquet(?)",
                [_path(table)],
            ).fetchone()
        if not row or row[0] is None:
            return {"range": [], "rows": 0}
        return {"range": [str(row[0])[:10], str(row[1])[:10]], "rows": int(row[2])}
    except Exception:
        return {"range": [], "rows": 0}


def _date_range(table: str):
    paths = [
        path for path in sorted(BASE.glob(f"**/{table}*.parquet"))
        if ".before_" not in path.name and not path.name.endswith(".tmp")
    ]
    if not paths:
        return [None, None]
    import duckdb
    try:
        with duckdb.connect() as con:
            row = con.execute(
                "SELECT MIN(TRY_CAST(at_date AS DATE)), MAX(TRY_CAST(at_date AS DATE)) FROM read_parquet(?)",
                [[str(path) for path in paths]],
            ).fetchone()
        return [str(row[0]) if row and row[0] else None, str(row[1]) if row and row[1] else None]
    except Exception:
        return [None, None]

def table_coverage():
    return {table: _date_range(table) for table in ("holdings", "dailylog", "quotes", "parthold", "bigchanges", "pquotes")}


def manifest():
    if not MANIFEST_FILE.exists():
        meta = {}
    else:
        meta = json.loads(MANIFEST_FILE.read_text(encoding="utf-8-sig"))
    tables = table_coverage()
    valid_ranges = [r for r in tables.values() if r[0] and r[1]]
    meta["table_coverage"] = tables
    meta["combined_range"] = [min(r[0] for r in valid_ranges), max(r[1] for r in valid_ranges)] if valid_ranges else []
    meta["latest_local_date"] = max((r[1] for r in valid_ranges), default=None)
    if INCREMENTAL_MANIFEST_FILE.exists():
        meta["incremental"] = json.loads(INCREMENTAL_MANIFEST_FILE.read_text(encoding="utf-8-sig"))
    return meta


def _path(table):
    return str(BASE / "**" / f"{table}*.parquet")


def query_stock(stock_code, days=90, limit=500, start_date=None, end_date=None):
    if not available():
        return {"status": "missing", "rows": []}
    import duckdb
    code = str(stock_code).strip().lstrip("0") or "0"
    sql = """
        WITH names AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY issue_id, stock_code, use_date
                ORDER BY row_id DESC
            ) AS rn
            FROM read_parquet(?)
        ), valid_names AS (
            SELECT * FROM names WHERE rn = 1
        ), pars AS (
            SELECT part_id, part_name,
                   ROW_NUMBER() OVER (PARTITION BY part_id ORDER BY part_name DESC) AS prn
            FROM read_parquet(?)
        ), valid_pars AS (
            SELECT part_id, part_name FROM pars WHERE prn = 1 AND part_name IS NOT NULL
        ), matched AS (
            SELECT h.at_date, CAST(h.part_id AS VARCHAR) AS part_id, h.issue_id, h.holding,
                   s.stock_code, s.short_name, p.part_name,
                   ROW_NUMBER() OVER (
                       PARTITION BY h.issue_id, h.at_date, CAST(h.part_id AS VARCHAR)
                       ORDER BY s.parallel ASC, s.use_date DESC, s.row_id DESC
                   ) AS stock_name_rank
            FROM read_parquet(?) h
            JOIN valid_names s ON s.issue_id = h.issue_id
             AND LTRIM(s.stock_code, '0') = ?
             AND s.use_date <= h.at_date
             AND (s.from_date IS NULL OR TRY_CAST(s.from_date AS DATE) <= h.at_date)
             AND (s.to_date IS NULL OR s.to_date = '' OR TRY_CAST(s.to_date AS DATE) > h.at_date)
            LEFT JOIN valid_pars p ON TRY_CAST(p.part_id AS VARCHAR) = CAST(h.part_id AS VARCHAR)
        )
        SELECT at_date, part_id, issue_id, holding, stock_code, short_name, part_name
        FROM matched
        WHERE stock_name_rank = 1
          AND at_date >= COALESCE(CAST(? AS DATE), (current_date - (? * INTERVAL '1 day'))::DATE)
          AND at_date <= COALESCE(CAST(? AS DATE), current_date)
        ORDER BY at_date DESC, holding DESC
        LIMIT ?
    """
    with duckdb.connect() as con:
        rows = con.execute(sql, [_path("shortnames"), _path("participants"), _path("holdings"), code, start_date, days, end_date, limit]).fetchall()
    keys = ["at_date", "part_id", "issue_id", "holding", "stock_code", "short_name", "part_name"]
    return {
        "status": "ok",
        "stock_code": code,
        "days": days,
        "rows": [dict(zip(keys, row)) for row in rows],
    }


def query_stock_range(stock_code, start_date=None, end_date=None, limit=100000):
    if not available():
        return {"status": "missing", "rows": []}
    import duckdb
    code = str(stock_code).strip().lstrip("0") or "0"
    clauses = ["LTRIM(s.stock_code, '0') = ?"]
    params = [code]
    if start_date:
        clauses.append("h.at_date >= CAST(? AS DATE)")
        params.append(str(start_date))
    if end_date:
        clauses.append("h.at_date <= CAST(? AS DATE)")
        params.append(str(end_date))
    sql = f"""
        WITH names AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY issue_id, stock_code, use_date
                ORDER BY row_id DESC
            ) AS rn
            FROM read_parquet(?)
        ), valid_names AS (
            SELECT * FROM names WHERE rn = 1
        ), pars AS (
            SELECT part_id, part_name,
                   ROW_NUMBER() OVER (PARTITION BY part_id ORDER BY part_name DESC) AS prn
            FROM read_parquet(?)
        ), valid_pars AS (
            SELECT part_id, part_name FROM pars WHERE prn = 1 AND part_name IS NOT NULL
        ), matched AS (
            SELECT h.at_date, CAST(h.part_id AS VARCHAR) AS part_id, h.issue_id, h.holding,
                   s.stock_code, s.short_name, p.part_name,
                   ROW_NUMBER() OVER (
                       PARTITION BY h.issue_id, h.at_date, CAST(h.part_id AS VARCHAR)
                       ORDER BY s.parallel ASC, s.use_date DESC, s.row_id DESC
                   ) AS stock_name_rank
            FROM read_parquet(?) h
            JOIN valid_names s ON s.issue_id = h.issue_id
             AND {' AND '.join(clauses)}
             AND s.use_date <= h.at_date
             AND (s.from_date IS NULL OR TRY_CAST(s.from_date AS DATE) <= h.at_date)
             AND (s.to_date IS NULL OR s.to_date = '' OR TRY_CAST(s.to_date AS DATE) > h.at_date)
            LEFT JOIN valid_pars p ON TRY_CAST(p.part_id AS VARCHAR) = CAST(h.part_id AS VARCHAR)
        )
        SELECT at_date, part_id, issue_id, holding, stock_code, short_name, part_name
        FROM matched
        WHERE stock_name_rank = 1
        ORDER BY at_date ASC, holding DESC
        LIMIT ?
    """
    with duckdb.connect() as con:
        rows = con.execute(sql, [_path("shortnames"), _path("participants"), _path("holdings"), *params, limit]).fetchall()
    keys = ["at_date", "part_id", "issue_id", "holding", "stock_code", "short_name", "part_name"]
    return {"status": "ok", "stock_code": str(stock_code).zfill(5), "rows": [dict(zip(keys, row)) for row in rows]}
