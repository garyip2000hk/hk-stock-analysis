"""
daily_pipeline.py — 每日全自動分析 pipeline
Runs all data import + analysis in sequence.
Output: daily_report.json
"""

import json, sys, time, re
from pathlib import Path
from datetime import date, timedelta

BASE = Path(__file__).parent

def normalize_date(d):
    if not d: return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", d): return d[:10]
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", d)
    if m: return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"^(\d{4})/(\d{2})/(\d{2})$", d)
    if m: return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None
sys.path.insert(0, str(BASE))

def run():
    start = time.time()
    today = date.today().isoformat()
    print(f"=== Daily Pipeline — {today} ===\n")

    report = {"date": today, "sections": {}}

    # 1. Data Import
    print("[1/6] Data Import...")
    try:
        from desktop_data_bridge import run_full_import
        run_full_import()
        report["sections"]["import"] = {"status": "ok"}
    except Exception as e:
        report["sections"]["import"] = {"status": "error", "error": str(e)}
        print(f"  ✗ {e}")


    # 1b. CBBC & Warrants Data
    print("\n[1b/13] CBBC & Warrants Data...")
    try:
        import cbbc_warrants_importer
        cbbc_warrants_importer.sync_data()
        report["sections"]["cbbc_warrants"] = {"status": "ok"}
    except Exception as e:
        report["sections"]["cbbc_warrants"] = {"status": "error", "error": str(e)}
        print(f"  ✗ {e}")

    # 2. Corporate Actions
    print("\n[2/11] Corporate Actions...")
    try:
        from corp_scanner import load_cache
        cache = load_cache()
        events = []
        for code, evts in cache.items():
            for e in evts:
                d = e.get("date", "")
                if d:
                    e["_code"] = code
                    events.append(e)
        report["sections"]["corp_actions"] = {
            "status": "ok",
            "total": len(events),
            "recent": sorted([e for e in events if normalize_date(e.get("date","")) and normalize_date(e.get("date","")) <= today and normalize_date(e.get("date","")) >= (date.fromisoformat(today) - timedelta(days=30)).isoformat()], key=lambda x: normalize_date(x.get("date","")), reverse=True)[:20],
        }
        print(f"  ✓ {len(events)} events")
    except Exception as e:
        report["sections"]["corp_actions"] = {"status": "error", "error": str(e)}
        print(f"  ✗ {e}")

    # 3. Short Positions
    print("\n[3/11] Short Position Analysis...")
    try:
        from short_analyzer import top_shorted, short_changes, short_squeeze_candidates
        report["sections"]["short_positions"] = {
            "status": "ok",
            "top_shorted": top_shorted(20),
            "big_changes": short_changes(7, 20),
            "squeeze_candidates": short_squeeze_candidates(),
        }
        print(f"  ✓ Top shorted: {report['sections']['short_positions']['top_shorted'][0]['name']}")
    except Exception as e:
        report["sections"]["short_positions"] = {"status": "error", "error": str(e)}
        print(f"  ✗ {e}")

    # 4. Announcements
    print("\n[4/11] Announcement Analysis...")
    try:
        from announcement_indexer import get_summary, get_recent_signals
        summary = get_summary(365)
        signals = get_recent_signals(365)
        report["sections"]["announcements"] = {
            "status": "ok",
            "summary": summary,
            "recent_signals": signals[:50],
        }
        total = sum(s["count"] for s in summary.values())
        print(f"  ✓ {total} categorized announcements")
    except Exception as e:
        report["sections"]["announcements"] = {"status": "error", "error": str(e)}
        print(f"  ✗ {e}")

    # 5. Company Registry
    print("\n[5/11] Company Registry...")
    try:
        from cr_analyzer import new_incorporations, name_changes, shell_signals, get_stats
        report["sections"]["company_registry"] = {
            "status": "ok",
            "stats": get_stats(),
            "new_incorporations": new_incorporations(7, 20),
            "name_changes": name_changes(7, 20),
            "shell_signals": shell_signals()[:20],
        }
        print(f"  ✓ {report['sections']['company_registry']['stats']['total_incorporations']} new companies")
    except Exception as e:
        report["sections"]["company_registry"] = {"status": "error", "error": str(e)}
        print(f"  ✗ {e}")

    # 6. CCASS local compressed dataset
    print("\n[6/11] CCASS Local Dataset...")
    try:
        from ccass_local import available, manifest
        meta = manifest()
        report["sections"]["ccass"] = {
            "status": "ok" if available() else "missing",
            "source": meta.get("source") or meta.get("source_snapshot"),
            "range": meta.get("combined_range") or meta.get("range"),
            "base_range": meta.get("range"),
            "incremental_range": (meta.get("incremental") or {}).get("range"),
            "latest_source_date": ((meta.get("incremental") or {}).get("range") or meta.get("range") or [None, None])[-1],
            "format": meta.get("format"),
            "tables": meta.get("tables", {}),
            "incremental_tables": (meta.get("incremental") or {}).get("tables", {}),
            "storage_bytes": sum(p.stat().st_size for p in Path("/home/workspace/Desktop/db/CCASS").glob("**/*.parquet")),
        }
        if available():
            print(f"  ✓ {meta.get('combined_range') or meta.get('range')} — Parquet local data available")
        else:
            print("  ! CCASS local dataset not found")
    except Exception as e:
        report["sections"]["ccass"] = {"status": "error", "error": str(e)}
        print(f"  ✗ {e}")

    # 6b. Online fallback readiness: local data remains canonical, while recent CCASS can be fetched on demand.
    try:
        from hybrid_ccass_pipeline import get_stock_data
        report["sections"]["ccass_online_fallback"] = {
            "status": "ready",
            "policy": "local-first-with-online-fallback",
            "note": "Queries after local coverage attempt a live HKEX CCASS fetch and fall back to local data on failure.",
            "example": get_stock_data("00001", end_date=today, prefer_web=False)["selected"].get("source"),
        }
    except Exception as e:
        report["sections"]["ccass_online_fallback"] = {"status": "error", "error": str(e)}

    # 6c. Options IV (HKEX stock options daily report)
    print("\n[7/11] Options IV...")
    try:
        import options_scraper, iv_analyzer
        options_scraper.ingest([date.today() - timedelta(days=i) for i in range(3, -1, -1)], verbose=False)
        rows = iv_analyzer.analyse()
        ivs = sorted(r["iv"] for r in rows if r.get("iv") is not None)
        report["sections"]["options_iv"] = {
            "status": "ok",
            "date": rows[0]["date"] if rows else None,
            "count": len(rows),
            "iv_median": ivs[len(ivs) // 2] if ivs else None,
            "expensive": [r for r in rows if r["score"] >= 3][:20],
            "cheap": sorted(rows, key=lambda r: (r["score"], r["iv_hv"] or 9))[:20],
        }
        print(f"  ✓ {len(rows)} 隻期權標的，IV 中位數 {report['sections']['options_iv']['iv_median']}%")
    except Exception as e:
        report["sections"]["options_iv"] = {"status": "error", "error": str(e)}
        print(f"  ✗ {e}")

    # 6d. CCASS × 期權交叉資金流
    print("\n[8/11] CCASS × Options cross...")
    try:
        import ccass_options_cross
        cross = ccass_options_cross.analyse()
        CROSS_OUT = Path(__file__).parent / "options_data" / "ccass_options_cross.json"
        CROSS_OUT.write_text(json.dumps(cross, ensure_ascii=False, indent=1, default=str))
        alerts = [r for r in cross if r["score"] >= 4]
        report["sections"]["ccass_options_cross"] = {
            "status": "ok",
            "date": cross[0]["date"] if cross else None,
            "ccass_date": cross[0].get("ccass_date") if cross else None,
            "count": len(cross),
            "alerts": alerts[:20],
            "bullish": [r for r in cross if r["bias"] == "看多" and r["score"] >= 3][:15],
            "bearish": [r for r in cross if r["bias"] == "看空" and r["score"] >= 3][:15],
        }
        print(f"  ✓ {len(cross)} 隻交叉分析，{len(alerts)} 個強訊號")
    except Exception as e:
        report["sections"]["ccass_options_cross"] = {"status": "error", "error": str(e)}
        print(f"  ✗ {e}")

    # 6e. 事件驅動期權策略
    print("\n[9/11] Event-driven option strategies...")
    try:
        import earnings_calendar
        earnings_calendar.build(since_days=45, verbose=False)
        import strategy_engine
        strat = strategy_engine.analyse()
        STRAT_OUT = Path(__file__).parent / "options_data" / "strategies.json"
        STRAT_OUT.write_text(json.dumps(strat, ensure_ascii=False, indent=1, default=str))
        report["sections"]["option_strategies"] = {
            "status": "ok",
            "date": strat[0]["date"] if strat else None,
            "count": len(strat),
            "top": strat[:15],
        }
        print(f"  ✓ {len(strat)} 個事件策略")
    except Exception as e:
        report["sections"]["option_strategies"] = {"status": "error", "error": str(e)}
        print(f"  ✗ {e}")

    # 6f. 內容簡報（第 5 項：財經內容自動化）
    print("\n[10/11] Content briefs...")
    try:
        import content_feed
        briefs = content_feed.build()
        content_feed.OPT.mkdir(parents=True, exist_ok=True)
        content_feed.OUT.write_text(
            json.dumps(
                {
                    "generated_at": date.today().isoformat(),
                    "briefs": briefs,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        kinds = {}
        for b in briefs:
            kinds[b["kind"]] = kinds.get(b["kind"], 0) + 1
        report["sections"]["content_briefs"] = {
            "status": "ok",
            "count": len(briefs),
            "by_kind": kinds,
            "path": str(content_feed.OUT),
        }
        print(f"  ✓ {len(briefs)} 條 brief → {content_feed.OUT.name}")
    except Exception as e:
        report["sections"]["content_briefs"] = {"status": "error", "error": str(e)}
        print(f"  ✗ {e}")


    # 6g. Leaderboard
    print("\n[11/12] Leaderboard...")
    try:
        import build_leaderboard
        build_leaderboard.build()
        report["sections"]["leaderboard"] = {"status": "ok"}
    except Exception as e:
        report["sections"]["leaderboard"] = {"status": "error", "error": str(e)}
        print(f"  ✗ {e}")

    print("\n[12/12] Saving report...")
    out = BASE / "daily_report.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    elapsed = round(time.time() - start, 1)
    print(f"\n=== Done in {elapsed}s → {out} ===")
    return report


if __name__ == "__main__":
    run()
