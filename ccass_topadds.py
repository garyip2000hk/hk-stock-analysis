#!/usr/bin/env python3
"""CCASS 近一年加倉頭 5 大 + 估算平均買入價（圖表指標用）。

做法
----
1. 基準：window_start（最近 365 日起點，但唔早過本地 CCASS 覆蓋起點）
   當日／之前每個參與者最後已知持倉（forward-fill 語義，同 ccass_snapshot 一致）。
   window 內先出現嘅參與者基準＝0。
2. 期末：coverage 最新日每個參與者最後已知持倉。
3. delta = 期末 − 基準，取 delta 最多嘅 top N（delta>0 而期末>0）。
4. 平均買入價：CCASS 只有持倉冇價格，用「加倉日加咗幾多股 × 當日收市價」
   加權平均估算。收市價由 Futu Desktop kline_day.parquet（HK.xxxxx）提供，
   缺價日用之前最近一個有數嘅收市價（forward-fill）。

CLI: python3 ccass_topadds.py --stock 00700 [--days 365] [--top 5]
"""
from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_right
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ccass_snapshot as cs

KLINE = Path(__file__).parent.parent / "Desktop" / "db" / "Futu" / "Kline" / "kline_day.parquet"
CCASS_QUOTES = (
    cs.CCASS / "quotes.parquet",
    cs.CCASS / "incremental" / "quotes_20251225_20260522.parquet",
)


def _ccass_quote_closes(issue_id: int) -> tuple[dict[str, float], list[str]]:
    """CCASS quotes 收市價後備（2025-12-29 起覆蓋全部 4,949 隻標的）。"""
    paths = cs._existing(CCASS_QUOTES)
    if not paths:
        return {}, []
    import duckdb

    try:
        with duckdb.connect() as con:
            rows = con.execute(
                "SELECT CAST(at_date AS DATE), closing FROM read_parquet(?) "
                "WHERE issue_id = ? AND closing IS NOT NULL AND closing > 0 ORDER BY at_date",
                [paths, issue_id],
            ).fetchall()
    except Exception:
        return {}, []
    dates: list[str] = []
    by_date: dict[str, float] = {}
    for d, close in rows:
        ds = str(d)[:10]
        if close is None or close <= 0:
            continue
        dates.append(ds)
        by_date[ds] = float(close)
    return by_date, dates


def _closes(stock_code: str) -> tuple[dict[str, float], list[str]]:
    """Futu 日線收市價：({date: close}, 排序日期清單)。缺檔／缺股 → 空表。"""
    if not KLINE.exists():
        return {}, []
    import duckdb

    hk = f"HK.{cs.pad_code(stock_code)}"
    try:
        with duckdb.connect() as con:
            rows = con.execute(
                "SELECT time_key, close FROM read_parquet(?) WHERE code = ? ORDER BY time_key",
                [str(KLINE), hk],
            ).fetchall()
    except Exception:
        return {}, []
    dates: list[str] = []
    by_date: dict[str, float] = {}
    for tk, close in rows:
        d = str(tk)[:10]
        if close is None or close <= 0:
            continue
        dates.append(d)
        by_date[d] = float(close)
    return by_date, dates


def _ffill_close(by_date: dict[str, float], dates: list[str], d: str) -> float | None:
    i = bisect_right(dates, d) - 1
    return by_date[dates[i]] if i >= 0 else None

def _api_closes(stock_code: str) -> tuple[dict[str, float], list[str]]:
    """長歷史後備：futu-warrant-api /pz-series 1d×1000 支（覆蓋 ~2022-09 起，
    補 kline_day.parquet 只滾動 ~282 日＋CCASS quotes 由 2025-12-29 起嘅缺口）。"""
    import json as _json
    import urllib.request

    hk = f"HK.{cs.pad_code(stock_code)}"
    url = f"http://127.0.0.1:8890/pz-series?code={hk}&period=1d&days=1000&delay=15"
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            data = _json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}, []
    if not data.get("ok"):
        return {}, []
    dates: list[str] = []
    by_date: dict[str, float] = {}
    for bar in data.get("candles") or []:
        d = str(bar.get("time", ""))[:10]
        c = bar.get("c")
        if not d or c is None or c <= 0:
            continue
        dates.append(d)
        by_date[d] = float(c)
    return by_date, dates


def _valid_iso(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def top_adds(stock_code: str, days: int = 365, top_n: int = 5, as_of: str | None = None,
             start_date: str | None = None, participant: str | None = None) -> dict:
    code = cs.pad_code(stock_code)
    issue_id = cs.resolve_issue_id(stock_code)
    if issue_id is None:
        return {"error": f"股票代號 {code} 喺 CCASS 資料庫搵唔到", "stock_code": code}

    paths = cs._existing(cs.HOLDING_SOURCES)
    if not paths:
        return {"error": "本地 CCASS 持倉檔案唔存在", "stock_code": code}

    cov_start, cov_end = cs.coverage_range()
    if not cov_end:
        return {"error": "本地 CCASS 覆蓋範圍不明", "stock_code": code}
    end = min(as_of, cov_end) if as_of else cov_end
    custom_start = _valid_iso(start_date)
    if custom_start:
        # 自訂起點：clamp 入 coverage 範圍，並且唔可以遲過 end
        start = min(custom_start, end)
    else:
        try:
            wanted_start = (date.fromisoformat(end) - timedelta(days=max(1, days))).isoformat()
        except ValueError:
            wanted_start = end
        start = min(wanted_start, end)
    if cov_start and start < cov_start:
        start = cov_start
    eff_days = (date.fromisoformat(end) - date.fromisoformat(start)).days

    # 讀晒呢隻股 window 前一個月起到 end 嘅 change-log（要 window 前資料做基準）
    pre = (date.fromisoformat(start) - timedelta(days=45)).isoformat()
    sql = """
        SELECT CAST(part_id AS BIGINT) AS part_id,
               CAST(holding AS BIGINT) AS holding,
               CAST(at_date AS DATE) AS at_date
        FROM read_parquet(?)
        WHERE issue_id = ? AND at_date <= CAST(? AS DATE) AND at_date >= CAST(? AS DATE)
        ORDER BY part_id, at_date
    """
    import duckdb

    with duckdb.connect() as con:
        rows = con.execute(sql, [paths, issue_id, end, pre]).fetchall()
    if not rows:
        return {"error": f"{code} 冇本地 CCASS 持倉紀錄", "stock_code": code}

    by_part: dict[int, list[tuple[str, int]]] = {}
    for part_id, holding, at_date in rows:
        by_part.setdefault(int(part_id), []).append((str(at_date)[:10], int(holding)))

    pars = _participant_names()
    final_total = 0
    finals: dict[int, int] = {}
    for part_id, series in by_part.items():
        finals[part_id] = series[-1][1]
        final_total += series[-1][1]

    raw_issued = cs.issued_shares(code)
    issued_stale = bool(raw_issued and final_total > raw_issued)
    base = raw_issued if (raw_issued and not issued_stale) else (final_total or None)

    by_close, close_dates = _closes(code)
    q_by, q_dates = _ccass_quote_closes(issue_id)
    price_parts: list[str] = []
    if by_close:
        price_parts.append("futu_kline_day")
    if q_by:
        price_parts.append("ccass_quotes")
    merged: dict[str, float] = dict(q_by)
    merged_dates = sorted(set(close_dates) | set(q_dates))
    merged.update(by_close)
    # 長歷史後備：本地兩個價源都唔夠遠（窗口起點之前）就用 /pz-series 1d×1000 支
    if (merged_dates and merged_dates[0] > start) or not merged_dates:
        api_by, api_dates = _api_closes(code)
        if api_by:
            price_parts.append("futu_api_1d")
            for d2, c2 in api_by.items():
                merged.setdefault(d2, c2)
            merged_dates = sorted(set(merged_dates) | set(api_dates))
    by_close, close_dates = merged, merged_dates
    price_source = "+".join(price_parts) if price_parts else None

    items = []
    for part_id, series in by_part.items():
        baseline = 0
        in_window: list[tuple[str, int]] = []
        for d, h in series:
            if d <= start:
                baseline = h
            elif d <= end:
                in_window.append((d, h))
        final = series[-1][1]
        delta = final - baseline
        if delta <= 0 or final <= 0:
            continue
        added = 0
        wsum = 0.0
        wdays = 0
        first_add = last_add = None
        prev = baseline
        for d, h in in_window:
            step = h - prev
            prev = h
            if step <= 0:
                continue
            if first_add is None:
                first_add = d
            last_add = d
            close = _ffill_close(by_close, close_dates, d)
            if close is None:
                continue
            added += step
            wsum += step * close
            wdays += 1
        item = {
            "participant_id": pars.get(part_id, {}).get("ccass_id") or str(part_id),
            "name": pars.get(part_id, {}).get("name") or f"PARTICIPANT {part_id}",
            "shares_before": baseline,
            "shares_after": final,
            "added": delta,
            "pct_added": round(delta * 100 / base, 2) if base else None,
            "avg_price": round(wsum / added, 3) if added > 0 and wsum > 0 else None,
            "weighted_shares": added,
            "add_days": wdays,
            "first_add": first_add,
            "last_add": last_add,
        }
        items.append((delta, item))

    items.sort(key=lambda x: x[0], reverse=True)

    # ── 自選倉位模式：追蹤指定 CCASS 參與者（ccass_id／part_id／名稱子串），回傳每日持倉數列 ──
    part_q = (participant or "").strip()
    if part_q:
        q_lower = part_q.lower()
        matched: dict[int, list[tuple[str, int]]] = {}
        for part_id, series in by_part.items():
            meta = pars.get(part_id, {})
            cid = str(meta.get("ccass_id") or "")
            pname = str(meta.get("name") or "")
            if cid.lower() == q_lower or str(part_id) == part_q or q_lower in pname.lower():
                matched[part_id] = series
        if not matched:
            return {"error": f"CCASS 搵唔到參與者「{part_q}」（試券商名子串，例如 HSBC／CITIBANK／UBS）", "stock_code": code}
        out_items = []
        for part_id, series in matched.items():
            meta = pars.get(part_id, {})
            # window 每日 forward-fill 持倉（起點前最後已知做第一點）
            ffd: list[tuple[str, int]] = []
            last = 0
            for d, h in series:
                if d > end:
                    break
                last = h
                if d >= start:
                    ffd.append((d, h))
            baseline = 0
            for d, h in series:
                if d <= start:
                    baseline = h
                else:
                    break
            final = series[-1][1]
            delta = final - baseline
            # 加倉日均價（同 top5 一套加權邏輯）
            added_w = 0
            wsum = 0.0
            prev = baseline
            first_add = last_add = None
            for d, h in series:
                if d < start or d > end:
                    prev = h if d <= end else prev
                    continue
                step = h - prev
                prev = h
                if step > 0:
                    if first_add is None:
                        first_add = d
                    last_add = d
                    close = _ffill_close(by_close, close_dates, d)
                    if close is not None:
                        added_w += step
                        wsum += step * close
            out_items.append({
                "participant_id": meta.get("ccass_id") or str(part_id),
                "name": meta.get("name") or f"PARTICIPANT {part_id}",
                "shares_before": baseline,
                "shares_after": final,
                "added": delta,
                "pct_added": round(delta * 100 / base, 2) if base else None,
                "avg_price": round(wsum / added_w, 3) if added_w > 0 and wsum > 0 else None,
                "weighted_shares": added_w,
                "first_add": first_add,
                "last_add": last_add,
            })
        series_out = [{"d": d, "s": h} for d, h in ffd]
        if baseline and (not series_out or series_out[0]["d"] != start):
            series_out.insert(0, {"d": start, "s": baseline})
        return {
            "stock_code": code,
            "mode": "participant",
            "participant_query": part_q,
            "window": {"start": start, "end": end, "days": eff_days},
            "coverage": [cov_start, cov_end],
            "basis": "issued_shares" if (base and base == raw_issued and not issued_stale)
                     else ("issued_shares_stale" if issued_stale else "ccass_total"),
            "price_source": price_source or "unavailable",
            "items": out_items,
            "series": series_out,
        }

    top = [it for _, it in items[: max(1, top_n)]]
    for i, it in enumerate(top, 1):
        it["rank"] = i

    return {
        "stock_code": code,
        "window": {"start": start, "end": end, "days": eff_days},
        "coverage": [cov_start, cov_end],
        "basis": "issued_shares" if (base and base == raw_issued and not issued_stale)
                 else ("issued_shares_stale" if issued_stale else "ccass_total"),
        "issued_shares": raw_issued,
        "issued_stale": issued_stale,
        "ccass_total": final_total,
        "price_source": price_source or "unavailable",
        "items": top,
    }


@cs.functools.lru_cache(maxsize=2)
def _participant_names() -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    p = cs.PARTICIPANTS
    if not p.exists():
        return out
    try:
        import duckdb

        with duckdb.connect() as con:
            rows = con.execute(
                """
                SELECT part_id, ccass_id, part_name
                FROM (
                    SELECT CAST(part_id AS BIGINT) AS part_id, ccass_id, part_name,
                           ROW_NUMBER() OVER (PARTITION BY CAST(part_id AS BIGINT) ORDER BY at_date DESC) AS rn
                    FROM read_parquet(?)
                ) WHERE rn = 1
                """,
                [str(p)],
            ).fetchall()
        for part_id, ccass_id, name in rows:
            out[int(part_id)] = {"ccass_id": str(ccass_id) if ccass_id else None, "name": name or ""}
    except Exception:
        return out
    return out


def suggest_participants(query: str, limit: int = 24) -> dict:
    """CCASS 倉位建議清單：名稱子串／ccass_id 前綴／part_id 完全匹配，供前端 autocomplete。"""
    q = (query or "").strip().lower()
    pars = _participant_names()
    rows = [
        {"part_id": pid, "ccass_id": v.get("ccass_id") or "", "name": v.get("name") or ""}
        for pid, v in pars.items()
    ]
    if q:
        scored: list[tuple[int, int, str, dict]] = []
        for r in rows:
            name_l = r["name"].lower()
            ccass_l = (r["ccass_id"] or "").lower()
            pid_s = str(r["part_id"])
            idx = name_l.find(q)
            id_hit = bool(ccass_l.startswith(q)) or pid_s == q
            if idx < 0 and not id_hit:
                continue
            scored.append((idx if idx >= 0 else 999, len(r["name"]), r["name"], r))
        scored.sort(key=lambda t: (t[0], t[1], t[2]))
        rows = [r for _, _, _, r in scored[:limit]]
    else:
        rows = sorted(rows, key=lambda r: r["name"])[:limit]
    return {"ok": True, "mode": "suggest", "query": query, "items": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock")
    ap.add_argument("--suggest", help="倉位建議：打名子串回傳建議清單（autocomplete 用，唔使 --stock）")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--as-of")
    ap.add_argument("--start", help="自訂 window 起點 YYYY-MM-DD（優先過 --days）")
    ap.add_argument("--participant", help="自選倉位：CCASS 參與者 ccass_id／part_id／名稱子串（例如 HSBC）")
    args = ap.parse_args()
    if args.suggest is not None:
        print(json.dumps(suggest_participants(args.suggest), ensure_ascii=False))
        return 0
    if not args.stock:
        print(json.dumps({"error": "missing --stock"}))
        return 2
    print(json.dumps(top_adds(args.stock, days=args.days, top_n=args.top, as_of=args.as_of,
                              start_date=args.start, participant=args.participant),
                     ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
