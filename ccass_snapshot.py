"""Reconstruct true CCASS holdings snapshots from the local change-log tables.

Why this exists
---------------
`holdings.parquet` / `parthold.parquet` / `incremental/holdings_*.parquet` are
*change logs*: a row appears only on the days a participant's balance moved.
Reading a single `at_date` therefore returns "who traded that day", NOT
"who holds the stock". Stock 01241 on 2026-07-31 has 2 change rows but 105
actual intermediary holders.

The correct snapshot is the last-known balance per participant as of a date
(forward-fill), keeping zero rows as genuine exits. Validated against the
`dailylog` table's own authoritative `c5`/`c10`/`intermed_cnt` columns, and
against check-site.ai, for 01241 / 06898 / 01428 — exact match.

Percentages are computed against *issued shares* (from `issued_shares.parquet`),
falling back to CCASS-total when unavailable. Using the sum-of-visible-rows as
the denominator was the second bug: it forced top-5 to always read 100%.
"""

from __future__ import annotations

import functools
from pathlib import Path

CCASS = Path(__file__).parent.parent / "Desktop" / "db" / "CCASS"

HOLDING_SOURCES = (
    CCASS / "holdings.parquet",
    CCASS / "incremental" / "holdings_20251225_20260522.parquet",
    CCASS / "parthold.parquet",
)
DAILYLOG_SOURCES = (
    CCASS / "dailylog.parquet",
    CCASS / "incremental" / "dailylog_20251225_20260522.parquet",
)
SHORTNAMES = CCASS / "shortnames.parquet"
PARTICIPANTS = CCASS / "participants.parquet"
ISSUED = CCASS / "issued_shares.parquet"


def _existing(paths):
    return [str(p) for p in paths if p.exists()]


def _connect():
    import duckdb

    return duckdb.connect()


def norm_code(stock_code) -> str:
    return str(stock_code).strip().lstrip("0") or "0"


def pad_code(stock_code) -> str:
    return norm_code(stock_code).zfill(5)


@functools.lru_cache(maxsize=4096)
def resolve_issue_id(stock_code) -> int | None:
    """Map a listed stock code to its CCASS issueID (ordinary/H listing)."""
    code = norm_code(stock_code)
    if not SHORTNAMES.exists():
        return None
    sql = """
        SELECT TRY_CAST(issue_id AS BIGINT) AS iid
        FROM read_parquet(?)
        WHERE LTRIM(stock_code, '0') = ?
          AND (to_date IS NULL OR to_date = '')
          AND TRY_CAST(issue_id AS BIGINT) IS NOT NULL
        ORDER BY COALESCE(parallel, FALSE) ASC, from_date DESC
        LIMIT 1
    """
    with _connect() as con:
        row = con.execute(sql, [str(SHORTNAMES), code]).fetchone()
    return int(row[0]) if row and row[0] is not None else None


@functools.lru_cache(maxsize=4096)
def issued_shares(stock_code) -> int | None:
    if not ISSUED.exists():
        return None
    with _connect() as con:
        row = con.execute(
            "SELECT issued_shares FROM read_parquet(?) WHERE stock_code = ? LIMIT 1",
            [str(ISSUED), norm_code(stock_code)],
        ).fetchone()
    return int(row[0]) if row and row[0] else None


def _dailylog(issue_id: int, as_of: str | None):
    """Authoritative per-day aggregates published by CCASS itself."""
    paths = _existing(DAILYLOG_SOURCES)
    if not paths:
        return None
    clause = "AND at_date <= CAST(? AS DATE)" if as_of else ""
    params: list = [paths, issue_id]
    if as_of:
        params.append(as_of)
    sql = f"""
        SELECT at_date, intermed_hldg, intermed_cnt, ncip_hldg, ncip_cnt,
               cip_hldg, cip_cnt, c5, c10, cust_hldg, brok_hldg
        FROM read_parquet(?)
        WHERE issue_id = ? {clause}
        ORDER BY at_date DESC
        LIMIT 1
    """
    with _connect() as con:
        row = con.execute(sql, params).fetchone()
    if not row:
        return None
    keys = ["at_date", "intermed_hldg", "intermed_cnt", "ncip_hldg", "ncip_cnt",
            "cip_hldg", "cip_cnt", "c5", "c10", "cust_hldg", "brok_hldg"]
    out = dict(zip(keys, row))
    out["at_date"] = str(out["at_date"])[:10]
    return out


@functools.lru_cache(maxsize=1)
def coverage_range():
    """Earliest/latest date present in the local holdings change-log."""
    paths = _existing(HOLDING_SOURCES)
    if not paths:
        return [None, None]
    with _connect() as con:
        row = con.execute(
            "SELECT MIN(at_date), MAX(at_date) FROM read_parquet(?)", [paths]
        ).fetchone()
    return [str(row[0])[:10] if row and row[0] else None,
            str(row[1])[:10] if row and row[1] else None]


@functools.lru_cache(maxsize=1)
def market_latest_date():
    """Latest date any stock moved in CCASS = the market's newest snapshot date."""
    return coverage_range()[1]


def snapshot(stock_code, as_of: str | None = None, top_n: int = 30) -> dict:
    """True holder snapshot: last-known balance per participant as of `as_of`."""
    code = pad_code(stock_code)
    issue_id = resolve_issue_id(stock_code)
    if issue_id is None:
        return {"error": f"股票代號 {code} 喺 CCASS 資料庫搵唔到", "stock_code": code}

    paths = _existing(HOLDING_SOURCES)
    if not paths:
        return {"error": "本地 CCASS 持倉檔案唔存在", "stock_code": code}

    date_clause = "AND at_date <= CAST(? AS DATE)" if as_of else ""
    params: list = [paths, issue_id]
    if as_of:
        params.append(as_of)
    params.append(str(PARTICIPANTS))

    sql = f"""
        WITH raw AS (
            SELECT CAST(part_id AS BIGINT) AS part_id,
                   CAST(holding AS BIGINT) AS holding,
                   at_date
            FROM read_parquet(?)
            WHERE issue_id = ? {date_clause}
        ), ranked AS (
            SELECT part_id, holding, at_date,
                   ROW_NUMBER() OVER (PARTITION BY part_id ORDER BY at_date DESC) AS rn
            FROM raw
        ), latest AS (
            SELECT part_id, holding, at_date AS last_change
            FROM ranked WHERE rn = 1 AND holding > 0
        ), pars AS (
            SELECT CAST(part_id AS BIGINT) AS part_id, ccass_id, part_name,
                   ROW_NUMBER() OVER (PARTITION BY part_id ORDER BY at_date DESC) AS prn
            FROM read_parquet(?)
        )
        SELECT l.part_id, p.ccass_id, p.part_name, l.holding, l.last_change
        FROM latest l
        LEFT JOIN pars p ON p.part_id = l.part_id AND p.prn = 1
        ORDER BY l.holding DESC
    """
    with _connect() as con:
        rows = con.execute(sql, params).fetchall()
        max_date = con.execute(
            f"SELECT MAX(at_date) FROM read_parquet(?) WHERE issue_id = ? {date_clause}",
            params[:3] if as_of else params[:2],
        ).fetchone()[0]

    if not rows:
        return {"error": f"{code} 冇本地 CCASS 持倉紀錄", "stock_code": code}

    log = _dailylog(issue_id, as_of)
    ccass_total = sum(r[3] for r in rows)
    issued = issued_shares(stock_code)
    # Prefer issued shares (matches HKEX/webb-site "Stake %"), else CCASS total.
    base = issued or (log or {}).get("intermed_hldg") or ccass_total

    holders = []
    for idx, (part_id, ccass_id, part_name, holding, last_change) in enumerate(rows, 1):
        holders.append({
            "rank": idx,
            "participant_id": ccass_id or str(part_id),
            "name": part_name or f"PARTICIPANT {part_id}",
            "shares": int(holding),
            "percentage": round(holding * 100 / base, 4) if base else 0.0,
            "last_change": str(last_change)[:10],
        })

    def cum(n):
        return round(sum(h["percentage"] for h in holders[:n]), 2)

    def pct_of_base(value):
        return round(value * 100 / base, 2) if base and value else None

    log_date = (log or {}).get("at_date")
    last_move = str(max_date)[:10] if max_date else None
    market_max = market_latest_date()
    as_of_date = as_of[:10] if as_of else market_max
    if market_max and as_of_date and as_of_date > market_max:
        as_of_date = market_max
    effective_date = as_of_date or last_move
    same_day = bool(log_date and effective_date and log_date == effective_date)

    # `dailylog` publishes CCASS's own top-5/top-10 aggregates. When it covers the
    # same date, trust it over our reconstruction: holders whose last movement
    # predates the local window start are invisible to a change-log replay.
    conc = {"top_5": cum(5), "top_10": cum(10), "top_20": cum(20)}
    conc_source = "reconstructed"
    if same_day:
        auth_5 = pct_of_base((log or {}).get("c5"))
        auth_10 = pct_of_base((log or {}).get("c10"))
        if auth_5 is not None and auth_10 is not None:
            conc = {"top_5": auth_5, "top_10": auth_10,
                    "top_20": max(conc["top_20"], auth_10)}
            conc_source = "ccass_dailylog"

    intermed = (log or {}).get("intermed_hldg") if same_day else None
    missing_shares = max(0, (intermed or 0) - ccass_total) if intermed else 0
    known_cnt = (log or {}).get("intermed_cnt") if same_day else None

    return {
        "stock_code": code,
        "issue_id": issue_id,
        "date": effective_date,
        "last_movement": last_move,
        "dailylog_date": log_date,
        "source": "local-reconstructed",
        "basis": "issued_shares" if issued else ("ccass_intermediaries" if log else "visible_rows"),
        "issued_shares": issued,
        "ccass_total": ccass_total,
        "percentage_base": base,
        "total_participants": len(holders),
        "dailylog_participants": known_cnt,
        "concentration": conc,
        "concentration_source": conc_source,
        "ccass_share_of_issued": round(ccass_total * 100 / issued, 2) if issued else None,
        "coverage": coverage_range(),
        "completeness": {
            "named_holders": len(holders),
            "ccass_holders": known_cnt,
            "unmapped_participants": max(0, (known_cnt or 0) - len(holders)) if known_cnt else 0,
            "unattributed_shares": missing_shares,
            "unattributed_pct": pct_of_base(missing_shares) or 0.0,
            "note": (
                "尾倉持有人最後一次變動早於本地資料起點，未能逐個還原；"
                "頭5大／頭10大已改用 CCASS 官方匯總數。"
            ) if missing_shares else "全部持倉已對齊 CCASS 官方匯總數。",
        },
        "top_holders": holders[:top_n],
        "participants": holders,
    }


def trading_dates(stock_code, start_date: str | None = None,
                  end_date: str | None = None, max_points: int = 40) -> list[str]:
    """Dates in range where this stock actually had CCASS movement."""
    issue_id = resolve_issue_id(stock_code)
    paths = _existing(HOLDING_SOURCES)
    if issue_id is None or not paths:
        return []
    clauses, params = ["issue_id = ?"], [paths, issue_id]
    if start_date:
        clauses.append("at_date >= CAST(? AS DATE)")
        params.append(start_date)
    if end_date:
        clauses.append("at_date <= CAST(? AS DATE)")
        params.append(end_date)
    sql = f"""
        SELECT DISTINCT at_date FROM read_parquet(?)
        WHERE {' AND '.join(clauses)}
        ORDER BY at_date
    """
    with _connect() as con:
        rows = [str(r[0])[:10] for r in con.execute(sql, params).fetchall()]
    if len(rows) <= max_points:
        return rows
    step = len(rows) / (max_points - 1)
    picked = {rows[min(int(i * step), len(rows) - 1)] for i in range(max_points - 1)}
    picked.add(rows[-1])
    return sorted(picked)



def movements(stock_code, start_date: str, end_date: str, min_pct: float = 0.5) -> dict:
    """Compare two true snapshots to find real accumulation / distribution."""
    before = snapshot(stock_code, start_date, top_n=10_000)
    after = snapshot(stock_code, end_date, top_n=10_000)
    if before.get("error"):
        return before
    if after.get("error"):
        return after

    idx_b = {h["participant_id"]: h for h in before["participants"]}
    idx_a = {h["participant_id"]: h for h in after["participants"]}

    changes = []
    for pid in set(idx_b) | set(idx_a):
        b = idx_b.get(pid)
        a = idx_a.get(pid)
        name = (a or b)["name"]
        sb = b["shares"] if b else 0
        sa = a["shares"] if a else 0
        pb = b["percentage"] if b else 0.0
        pa = a["percentage"] if a else 0.0
        if sa == sb:
            continue
        changes.append({
            "participant_id": pid,
            "name": name,
            "shares_before": sb,
            "shares_after": sa,
            "delta_shares": sa - sb,
            "percentage_before": pb,
            "percentage_after": pa,
            "delta_percentage": round(pa - pb, 4),
            "direction": "加倉" if sa > sb else "減倉",
            "is_new": b is None,
            "is_exit": a is None or sa == 0,
        })
    changes.sort(key=lambda c: abs(c["delta_percentage"]), reverse=True)
    significant = [c for c in changes if abs(c["delta_percentage"]) >= min_pct]

    return {
        "stock_code": pad_code(stock_code),
        "date_before": before["date"],
        "date_after": after["date"],
        "concentration_before": before["concentration"],
        "concentration_after": after["concentration"],
        "delta_top_5": round(after["concentration"]["top_5"] - before["concentration"]["top_5"], 2),
        "delta_top_10": round(after["concentration"]["top_10"] - before["concentration"]["top_10"], 2),
        "accumulators": [c for c in significant if c["delta_shares"] > 0][:20],
        "distributors": [c for c in significant if c["delta_shares"] < 0][:20],
        "significant_count": len(significant),
        "changes": changes[:100],
    }


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("stock_code")
    ap.add_argument("--as-of")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    args = ap.parse_args()

    if args.start and args.end:
        out = movements(args.stock_code, args.start, args.end)
    else:
        out = snapshot(args.stock_code, args.as_of)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
