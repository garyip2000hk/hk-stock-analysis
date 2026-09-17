"""chain_daily_export.py — 每日收市期權鏈 → CSV（供 Google Drive / Sheet 分享）

數據源（全部 append-only 本地歷史）：
  options_data/chain_history.parquet   逐個行使價結算價／IV／成交／未平倉（只留有流動性合約）
  options_data/atm_iv_history.parquet  乾淨 ATM IV（唔用 iv_history 嘅污染 IV）
  options_data/iv_history.parquet      HKEX class summary（成交／未平倉／PCR）

每日輸出三個 CSV 去 reports/chain_export/：
  每日總覽_<date>.csv     每隻標的一行（代表月 ATM IV + IV 排名 + 成交/未平倉）
  期權鏈_精選_<date>.csv   ATM ±8 個行使價 × 最近兩個到期月（約 4,500 行）
  期權鏈_全鏈_<date>.csv   當日全部有流動性合約（約 42,000 行）

用法：
  python3 chain_daily_export.py                    # 補齊所有未 export 嘅交易日（冪等）
  python3 chain_daily_export.py --date 2026-09-16  # 指定一日
  python3 chain_daily_export.py --list             # 睇已 export 邊幾日
  python3 chain_daily_export.py --keep 60          # 只保留最近 60 個交易日 CSV

輸出最後一行係機器可讀狀態：EXPORT_OK / EXPORT_NONE / EXPORT_FAIL
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

BASE = Path(__file__).parent
RAW_DIR = BASE / "options_data" / "raw"
CHAIN = BASE / "options_data" / "chain_history.parquet"
ATM = BASE / "options_data" / "atm_iv_history.parquet"
IVH = BASE / "options_data" / "iv_history.parquet"
OUT_DIR = BASE / "reports" / "chain_export"
STATE = OUT_DIR / "state.json"

PY = "/usr/local/bin/python3"
ATM_WINDOW = 8      # ATM ± 8 個行使價
ATM_EXPIRIES = 2    # 最近 2 個到期月
DEFAULT_KEEP = 90   # 保留最近 N 個交易日 CSV
LOOKBACK = 252

OVERVIEW_COLS = [
    "日期", "股票代號", "HKATS", "名稱", "收市價", "代表到期日", "剩餘日",
    "ATM行使價", "ATM_IV%", "Call_IV%", "Put_IV%", "25D偏斜",
    "IV排名%", "IV百分位%", "一年IV低%", "一年IV高%", "貴平",
    "總成交", "Call成交", "Put成交", "總未平倉", "Call未平倉", "Put未平倉",
    "成交PCR", "未平倉PCR",
]

CHAIN_COLS = [
    "日期", "股票代號", "HKATS", "名稱", "收市價", "到期日", "剩餘日",
    "行使價", "類型", "結算價", "IV%", "成交量", "未平倉", "未平倉變動",
    "行使價/現價", "價內外", "ATM",
]


# ---------------------------------------------------------------- 狀態

def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"exported": {}, "keep": DEFAULT_KEEP}


def save_state(st: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- 數據準備

def raw_dates() -> list[date]:
    return [datetime.strptime(f.name[3:9], "%y%m%d").date() for f in sorted(RAW_DIR.glob("dqe*.txt.gz"))]


def ensure_raw(d: date) -> bool:
    """raw 報告唔喺度就抓一次（HKEX 只保留近一年，抓唔到就放棄）。"""
    f = RAW_DIR / f"dqe{d:%y%m%d}.txt.gz"
    if f.exists():
        return True
    r = subprocess.run([PY, "options_scraper.py", "--date", d.isoformat()], cwd=str(BASE),
                       capture_output=True, text=True, timeout=300)
    return f.exists() or (r.returncode == 0 and f.exists())


def ensure_caches(d: date) -> None:
    """chain_history / atm_iv_history 未包呢日就增量更新。"""
    if CHAIN.exists():
        have = set(pd.to_datetime(pd.read_parquet(CHAIN, columns=["date"])["date"]).dt.date.unique())
        if d not in have:
            subprocess.run([PY, "chain_history.py", "--update"], cwd=str(BASE),
                           capture_output=True, text=True, timeout=1800)
    if ATM.exists():
        a = pd.read_parquet(ATM, columns=["date"])
        if d.isoformat() not in set(a["date"].astype(str).unique()):
            subprocess.run([PY, "atm_history.py", "--build", "--since", d.isoformat()], cwd=str(BASE),
                           capture_output=True, text=True, timeout=1800)


def iv_series_by_code() -> dict[str, pd.Series]:
    """每隻標的嘅代表月 ATM IV 時間序列（計 IV 排名用）。"""
    if not ATM.exists():
        return {}
    import atm_history
    f = atm_history.front_iv()
    if f.empty:
        return {}
    f = f.copy()
    f["date"] = pd.to_datetime(f["date"])
    out = {}
    for code, g in f.groupby("stock_code"):
        s = g.set_index("date")["atm_iv"].sort_index()
        out[str(code)] = s[~s.index.duplicated()]
    return out


def iv_rank_stats(s: pd.Series, asof: pd.Timestamp) -> dict:
    s = s.loc[:asof].dropna().iloc[-LOOKBACK:]
    if len(s) < 2:
        return {}
    cur = float(s.iloc[-1])
    lo, hi = float(s.min()), float(s.max())
    rank = (cur - lo) / (hi - lo) * 100 if hi > lo else 50.0
    return {
        "IV排名%": round(rank, 1),
        "IV百分位%": round(float((s < cur).mean() * 100), 1),
        "一年IV低%": round(lo, 1),
        "一年IV高%": round(hi, 1),
    }


def verdict_label(rank: float | None) -> str:
    if rank is None:
        return "數據不足"
    if rank >= 70:
        return "偏貴（利賣方）"
    if rank <= 30:
        return "偏平（利買方）"
    return "中性"


# ---------------------------------------------------------------- 建表

def build_overview(d: date, atm: pd.DataFrame, ivh: pd.DataFrame, series: dict) -> pd.DataFrame:
    ds = d.isoformat()
    a = atm[atm["date"] == ds]
    v = ivh[pd.to_datetime(ivh["date"]).dt.date == d]
    if a.empty:
        return pd.DataFrame(columns=OVERVIEW_COLS)

    # 代表月：DTE 15-75 之內未平倉最多嗰個月；冇就用最貼近 7 日以上嘅近月
    cand = a[a.dte.between(15, 75)]
    fb = a[a.dte >= 7]
    front = pd.concat([
        cand,
        fb[~fb.set_index(["stock_code"]).index.isin(cand.set_index(["stock_code"]).index)],
    ], ignore_index=True)
    front = (front.sort_values(["stock_code", "oi"]).groupby("stock_code", as_index=False).last())
    if front.empty:
        front = a.sort_values(["stock_code", "dte"]).groupby("stock_code", as_index=False).first()

    rows = []
    asof = pd.Timestamp(d)
    vol_map = {str(r.stock_code).zfill(5): r for r in v.itertuples()} if not v.empty else {}
    for r in front.itertuples():
        code = str(r.stock_code).zfill(5)
        st = iv_rank_stats(series.get(code, pd.Series(dtype=float)), asof)
        rank = st.get("IV排名%")
        iv = vol_map.get(code)
        rows.append({
            "日期": ds, "股票代號": code, "名稱": r.name,
            "收市價": r.close, "代表到期日": str(r.expiry)[:10], "剩餘日": int(r.dte),
            "ATM行使價": r.atm_strike, "ATM_IV%": r.atm_iv,
            "Call_IV%": r.call_iv, "Put_IV%": r.put_iv, "25D偏斜": r.skew_25d,
            **st, "貴平": verdict_label(rank),
            "總成交": getattr(iv, "volume", None), "Call成交": getattr(iv, "call_vol", None),
            "Put成交": getattr(iv, "put_vol", None), "總未平倉": getattr(iv, "oi", None),
            "Call未平倉": getattr(iv, "call_oi", None), "Put未平倉": getattr(iv, "put_oi", None),
            "成交PCR": getattr(iv, "pcr_vol", None), "未平倉PCR": getattr(iv, "pcr_oi", None),
        })
    df = pd.DataFrame(rows)
    if "hkats" in v.columns and not v.empty:
        hk = v[["stock_code", "hkats"]].astype(str).rename(
            columns={"stock_code": "股票代號", "hkats": "HKATS"})
        df["股票代號"] = df["股票代號"].astype(str)
        df = df.merge(hk.drop_duplicates("股票代號"), on="股票代號", how="left")
        df["HKATS"] = df["HKATS"].fillna("")
    df = df.reindex(columns=OVERVIEW_COLS)
    return df.sort_values("總成交", ascending=False, na_position="last").reset_index(drop=True)


def build_chain(d: date, ch: pd.DataFrame, names: dict, sel: bool) -> pd.DataFrame:
    day = ch[pd.to_datetime(ch["date"]).dt.date == d].copy()
    if day.empty:
        return pd.DataFrame(columns=CHAIN_COLS)
    day["hkats"] = day["hkats"].fillna("")
    day["name"] = day["stock_code"].map(names).fillna("")
    if sel:
        parts = []
        for code, g in day.groupby("stock_code"):
            close = float(g["close"].iloc[0])
            if close <= 0:
                continue
            exps = sorted(g["expiry"].unique())[:ATM_EXPIRIES]
            g = g[g["expiry"].isin(exps)]
            for exp, ge in g.groupby("expiry"):
                strikes = sorted(ge["strike"].unique())
                if not strikes:
                    continue
                atm = min(strikes, key=lambda s: abs(s - close))
                i = strikes.index(atm)
                keep = set(strikes[max(0, i - ATM_WINDOW): i + ATM_WINDOW + 1])
                parts.append(ge[ge["strike"].isin(keep)])
        day = pd.concat(parts, ignore_index=True) if parts else day.iloc[:0]
    if day.empty:
        return pd.DataFrame(columns=CHAIN_COLS)

    day["mny"] = (day["strike"] / day["close"]).round(4)
    day["itm"] = [
        "價內" if (t == "C" and k < c) or (t == "P" and k > c) else "價外"
        for t, k, c in zip(day["type"], day["strike"], day["close"])
    ]
    idx = (day.assign(_d=(day["strike"] - day["close"]).abs())
           .groupby(["stock_code", "expiry", "type"])["_d"].idxmin())
    day["atm"] = ""
    day.loc[idx, "atm"] = "ATM"

    out = pd.DataFrame({
        "日期": pd.to_datetime(day["date"]).dt.strftime("%Y-%m-%d"),
        "股票代號": day["stock_code"], "HKATS": day["hkats"], "名稱": day["name"],
        "收市價": day["close"],
        "到期日": pd.to_datetime(day["expiry"]).dt.strftime("%Y-%m-%d"),
        "剩餘日": day["dte"], "行使價": day["strike"],
        "類型": day["type"].map({"C": "Call", "P": "Put"}).fillna(day["type"]),
        "結算價": day["settle"], "IV%": day["iv"],
        "成交量": day["volume"], "未平倉": day["oi"], "未平倉變動": day["oi_chg"],
        "行使價/現價": day["mny"], "價內外": day["itm"], "ATM": day["atm"],
    })
    return (out.sort_values(["股票代號", "到期日", "行使價", "類型"],
                            ascending=[True, True, True, False])
            .reindex(columns=CHAIN_COLS).reset_index(drop=True))


def write_csv(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, encoding="utf-8-sig")


def prune(st: dict) -> int:
    keep = int(st.get("keep", DEFAULT_KEEP))
    days = sorted(st["exported"].keys())
    if len(days) <= keep:
        return 0
    n = 0
    for old in days[:-keep]:
        for f in OUT_DIR.glob(f"*_{old}.csv"):
            f.unlink(missing_ok=True)
            n += 1
        st["exported"].pop(old, None)
    return n


# ---------------------------------------------------------------- 主流程

def export_one(d: date, st: dict, atm: pd.DataFrame, ivh: pd.DataFrame,
               ch: pd.DataFrame, series: dict, names: dict) -> dict:
    ensure_caches(d)
    if not CHAIN.exists():
        raise RuntimeError("chain_history.parquet 未建")
    ch_day = pd.read_parquet(CHAIN)
    ch_day = ch_day[pd.to_datetime(ch_day["date"]).dt.date == d]
    if ch_day.empty:
        return {"date": d.isoformat(), "skipped": "chain_history 冇呢日數據"}

    ov = build_overview(d, atm, ivh, series)
    sel = build_chain(d, ch_day, names, sel=True)
    full = build_chain(d, ch_day, names, sel=False)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p_ov = OUT_DIR / f"每日總覽_{d:%Y-%m-%d}.csv"
    p_sel = OUT_DIR / f"期權鏈_精選_{d:%Y-%m-%d}.csv"
    p_full = OUT_DIR / f"期權鏈_全鏈_{d:%Y-%m-%d}.csv"
    write_csv(ov, p_ov)
    write_csv(sel, p_sel)
    write_csv(full, p_full)

    info = {
        "date": d.isoformat(),
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "overview_rows": int(len(ov)), "selected_rows": int(len(sel)), "full_rows": int(len(full)),
        "files": {k: str(v.relative_to(BASE)) for k, v in
                  (("overview", p_ov), ("selected", p_sel), ("full", p_full))},
        "bytes": {"overview": p_ov.stat().st_size, "selected": p_sel.stat().st_size,
                  "full": p_full.stat().st_size},
    }
    st["exported"][d.isoformat()] = info
    save_state(st)
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="每日收市期權鏈 → CSV")
    ap.add_argument("--date", help="指定交易日 YYYY-MM-DD")
    ap.add_argument("--list", action="store_true", help="列出已 export 日子")
    ap.add_argument("--keep", type=int, help=f"保留最近 N 個交易日 CSV（預設 {DEFAULT_KEEP}）")
    ap.add_argument("--max-days", type=int, default=10, help="單次最多補幾日")
    a = ap.parse_args()

    st = load_state()
    if a.keep:
        st["keep"] = a.keep
    if a.list:
        for k in sorted(st["exported"]):
            i = st["exported"][k]
            print(f"{k}  總覽 {i.get('overview_rows')}  精選 {i.get('selected_rows')}  全鏈 {i.get('full_rows')}")
        return 0

    if a.date:
        targets = [date.fromisoformat(a.date)]
    else:
        have = set(st["exported"])
        targets = [d for d in raw_dates() if d.isoformat() not in have][-a.max_days:]

    if not targets:
        pruned = prune(st)
        save_state(st)
        print(f"EXPORT_NONE: 冇新交易日（已 export {len(st['exported'])} 日"
              + (f"，清理 {pruned} 個舊檔" if pruned else "") + "）")
        return 0

    for d in targets:
        if not ensure_raw(d):
            print(f"EXPORT_FAIL: {d} HKEX 報告未可得（未收市／未刊登）")
            return 1

    atm = pd.read_parquet(ATM) if ATM.exists() else pd.DataFrame()
    ivh = pd.read_parquet(IVH) if IVH.exists() else pd.DataFrame()
    # parquet 嘅 date 欄係 datetime.date 物件 — 統一轉 'YYYY-MM-DD' 字串先比對到
    for _df in (atm, ivh):
        if not _df.empty and "date" in _df.columns:
            _df["date"] = _df["date"].astype(str).str.slice(0, 10)
    series = iv_series_by_code()
    if ivh.empty:
        names = {}
    else:
        names = dict(zip(ivh["stock_code"].astype(str).str.zfill(5), ivh["name"]))

    done = []
    for d in targets:
        info = export_one(d, st, atm, ivh, None, series, names)
        if info.get("skipped"):
            print(f"EXPORT_FAIL: {info['date']} {info['skipped']}")
            return 1
        done.append(info)
        print(f"  ✓ {info['date']}  總覽 {info['overview_rows']} 行 / "
              f"精選 {info['selected_rows']} 行 / 全鏈 {info['full_rows']} 行 "
              f"({info['bytes']['full'] / 1e6:.1f}MB)")

    pruned = prune(st)
    save_state(st)
    latest = done[-1]
    print(f"EXPORT_OK: {','.join(i['date'] for i in done)} | "
          f"latest={latest['date']} overview={latest['overview_rows']} "
          f"selected={latest['selected_rows']} full={latest['full_rows']}"
          + (f" | pruned={pruned}" if pruned else ""))
    for i in done:
        for k, v in i["files"].items():
            print(f"FILE {k} {i['date']} {BASE / v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
