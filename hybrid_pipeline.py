"""Hybrid CCASS data access for local Parquet data with online fallback."""
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

BASE = Path(__file__).parent
CACHE_FILE = BASE / "hybrid_ccass_cache.json"


def normalize_code(stock_code: str) -> str:
    digits = re.sub(r"\D", "", str(stock_code))
    return digits.zfill(5)


def normalize_date(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%m-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError(f"Unsupported date: {value}")


def previous_business_day(value: str | date | None = None) -> str:
    current = datetime.strptime(normalize_date(value) or date.today().isoformat(), "%Y-%m-%d").date()
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current.isoformat()


def _load_cache() -> dict[str, Any]:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def coverage() -> dict[str, Any]:
    import ccass_local
    meta = ccass_local.manifest()
    combined = meta.get("combined_range") or meta.get("range") or []
    incremental = meta.get("incremental") or {}
    return {
        "source": meta.get("dataset", "Webb-site CCASS"),
        "base_range": meta.get("range", []),
        "incremental_range": incremental.get("range", []),
        "combined_range": combined,
        "latest_local_date": combined[-1] if combined else None,
        "available": ccass_local.available(),
        "updated_at": incremental.get("updated_at"),
    }


def _query_local(stock_code: str, start_date: str, end_date: str, limit: int = 20000) -> dict[str, Any]:
    import duckdb
    import ccass_local

    code = normalize_code(stock_code).lstrip("0") or "0"
    sql = """
        WITH names AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY issue_id, stock_code, use_date
                ORDER BY row_id DESC
            ) AS rn
            FROM read_parquet(?)
        ), valid_names AS (
            SELECT * FROM names WHERE rn = 1
        ), matched AS (
            SELECT h.at_date, h.part_id, h.issue_id, h.holding,
                   s.stock_code, s.short_name,
                   ROW_NUMBER() OVER (
                       PARTITION BY h.issue_id, h.at_date
                       ORDER BY s.parallel ASC, s.use_date DESC, s.row_id DESC
                   ) AS stock_name_rank
            FROM read_parquet(?) h
            JOIN valid_names s ON s.issue_id = h.issue_id
             AND LTRIM(s.stock_code, '0') = ?
             AND s.use_date <= h.at_date
             AND (s.from_date IS NULL OR s.from_date <= h.at_date)
             AND (s.to_date IS NULL OR s.to_date > h.at_date)
        )
        SELECT CAST(at_date AS VARCHAR), part_id, issue_id, holding, stock_code, short_name
        FROM matched
        WHERE stock_name_rank = 1
          AND CAST(at_date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        ORDER BY at_date DESC, holding DESC
        LIMIT ?
    """
    if not ccass_local.available():
        return {"status": "missing", "rows": []}
    with duckdb.connect() as con:
        rows = con.execute(sql, [ccass_local._path("shortnames"), ccass_local._path("holdings"), code, start_date, end_date, limit]).fetchall()
    keys = ["date", "participant_id", "issue_id", "shares", "stock_code", "short_name"]
    result = [dict(zip(keys, row)) for row in rows]
    return {"status": "ok", "rows": result, "source": "local_parquet"}


def _local_snapshot(stock_code: str, target_date: str) -> dict[str, Any]:
    result = _query_local(stock_code, "2023-01-01", target_date, limit=50000)
    rows = result.get("rows", [])
    if not rows:
        return {"status": "no_data", "rows": [], "source": "local_parquet"}
    latest = rows[0]["date"][:10]
    snapshot = [row for row in rows if row["date"][:10] == latest]
    return {"status": "ok", "date": latest, "rows": snapshot, "source": "local_parquet"}


def _online_snapshot(stock_code: str, target_date: str | None) -> dict[str, Any]:
    import ccass_scraper
    code = normalize_code(stock_code)
    requested = previous_business_day(target_date)
    cache_key = f"{code}:{requested}"
    cache = _load_cache()
    if cache_key in cache:
        return {**cache[cache_key], "cached": True}
    scraped = ccass_scraper.fetch_ccass(code, requested.replace("-", "/"))
    if "error" in scraped:
        return {"status": "error", "source": "online_hkex", "date": requested, "error": scraped["error"], "rows": []}
    rows = []
    for item in scraped.get("participants", []):
        rows.append({
            "date": requested,
            "participant_id": item.get("participant_id"),
            "issue_id": item.get("participant_id"),
            "shares": item.get("shares", 0),
            "stock_code": code,
            "short_name": item.get("name", ""),
            "percentage": item.get("percentage", 0),
        })
    result = {"status": "ok", "source": "online_hkex", "date": requested, "rows": rows}
    cache[cache_key] = result
    _save_cache(cache)
    return result


def snapshot(stock_code: str, target_date: str | None = None, allow_online: bool = True) -> dict[str, Any]:
    code = normalize_code(stock_code)
    requested = previous_business_day(target_date)
    local_latest = coverage().get("latest_local_date")
    if local_latest and requested <= local_latest:
        local = _local_snapshot(code, requested)
        if local.get("rows"):
            return {"stock_code": code, "requested_date": requested, **local, "coverage": coverage()}
    if allow_online:
        online = _online_snapshot(code, requested)
        return {"stock_code": code, "requested_date": requested, **online, "coverage": coverage()}
    return {"stock_code": code, "requested_date": requested, "status": "unavailable", "rows": [], "coverage": coverage()}


def range_snapshots(stock_code: str, start_date: str, end_date: str, allow_online: bool = True) -> dict[str, Any]:
    start = normalize_date(start_date)
    end = normalize_date(end_date)
    code = normalize_code(stock_code)
    local = _query_local(code, start, end, limit=100000)
    if local.get("rows"):
        by_date: dict[str, list[dict[str, Any]]] = {}
        for row in local["rows"]:
            by_date.setdefault(row["date"][:10], []).append(row)
        return {"stock_code": code, "source": "local_parquet", "start_date": start, "end_date": end, "by_date": by_date, "coverage": coverage()}
    if allow_online:
        return {"stock_code": code, "source": "online_hkex_on_demand", "start_date": start, "end_date": end, "by_date": {}, "coverage": coverage(), "warning": "Historical online backfill is intentionally on-demand; use snapshot() for specific dates."}
    return {"stock_code": code, "source": "unavailable", "start_date": start, "end_date": end, "by_date": {}, "coverage": coverage()}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_code")
    parser.add_argument("--date")
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    result = range_snapshots(args.stock_code, args.start, args.end) if args.start and args.end else snapshot(args.stock_code, args.date)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
