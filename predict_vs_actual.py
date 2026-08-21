#!/usr/local/bin/python3
"""
predict_vs_actual.py — 恆指「預測 vs 真實」數據引擎

數據源：
  1. kline_index.parquet (OpenD) — HSI OHLC + VHSI 歷史
  2. Google Sheet「工作表2」— CBBC 街貨分佈每日記錄（用戶每日貼入）
  3. Google Sheet「工作表4」— 最新 OHLC / VHSI / EM 階梯
  4. Google Sheet「HSI_Data」— GOOGLEFINANCE 收市 backup
  5. Google Sheet「strangle_log」— 每週 Short Strangle 記錄

EM 公式同工作表4完全一致：EM_N = close × VHSI/100 ÷ divisor
  divisor: 1日=√252, 2日=√126, 1週=√52, 1月=√12, 6週=√6, 3月=√4, 半年=√2, 1年=√1

輸出：JSON（stdout 或 --out <path>）
"""
import argparse
import csv
import io
import json
import math
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

SHEET_ID = "1dS9G5GSoq7Ue-Ni00lOXam37cR-Z8bMrMWP2ysUUCXA"
KLINE = Path("/home/workspace/Desktop/db/Futu/Kline/kline_index.parquet")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

HORIZONS = [("1", "1日", math.sqrt(252)), ("2", "2日", math.sqrt(126)),
            ("5", "1週", math.sqrt(52)), ("21", "1月", math.sqrt(12)),
            ("42", "6週", math.sqrt(6)), ("63", "3月", math.sqrt(4)),
            ("126", "半年", math.sqrt(2)), ("252", "1年", 1.0)]

MIN_OS = 300  # 街貨門檻，同 strangle records 一致


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def fetch_csv(sheet_param):
    url = (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet={sheet_param}")
    return list(csv.reader(io.StringIO(fetch_text(url))))


def norm_date(s):
    """'2026/8/20 下午 4:00:00' / '2026/8/20' / '2026-08-20' -> '2026-08-20'"""
    if not s:
        return None
    s = str(s).strip()
    head = s.split(" ")[0].replace("/", "-")
    parts = head.split("-")
    if len(parts) != 3:
        return None
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if not (2020 <= y <= 2100):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def num(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_kline():
    hsi, vhsi = {}, {}
    try:
        import pandas as pd
        df = pd.read_parquet(KLINE)
        for _, r in df.iterrows():
            d = str(r["time_key"])[:10]
            if r["code"] == "HK.800000":
                hsi[d] = (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
            elif r["code"] == "HK.800125":
                vhsi[d] = float(r["close"])
    except Exception as e:
        print(f"WARN kline: {e}", file=sys.stderr)
    return hsi, vhsi


def load_sheet4():
    """rows: date -> {o,h,l,c,vhsi}（最新 OHLC/VHSI，覆蓋 parquet 時差）"""
    out = {}
    try:
        rows = fetch_csv("%E5%B7%A5%E4%BD%9C%E8%A1%A84")
    except Exception as e:
        print(f"WARN sheet4: {e}", file=sys.stderr)
        return out
    for r in rows:
        if len(r) < 8:
            continue
        d = norm_date(r[0])
        c = num(r[1])
        if not d or c is None:
            continue
        o, h, l = num(r[2]), num(r[3]), num(r[4])
        v = num(r[6])
        if v is not None and v > 3:  # '1875.00%' -> 18.75
            v = v / 100.0
        elif v is not None and v <= 3:
            v = v * 100 if v < 0.3 else v  # 0.1875 形式 -> 18.75? 保守處理
        out[d] = {"o": o, "h": h, "l": l, "c": c, "v": v}
    return out


def load_hsi_close():
    out = {}
    try:
        rows = fetch_csv("HSI_Data")
    except Exception as e:
        print(f"WARN hsi_data: {e}", file=sys.stderr)
        return out
    for r in rows[1:]:
        d = norm_date(r[0]) if r else None
        c = num(r[1]) if len(r) > 1 else None
        if d and c:
            out[d] = c
    return out


def load_cbbc():
    """date -> {level:int -> os:float}；重複欄位取最左（最新插入）"""
    out = {}
    try:
        rows = fetch_csv("%E5%B7%A5%E4%BD%9C%E8%A1%A82")
    except Exception as e:
        print(f"WARN sheet2: {e}", file=sys.stderr)
        return out
    if not rows:
        return out
    hdr = rows[0]
    for j in range(1, len(hdr)):
        d = norm_date(hdr[j])
        if not d or d in out:
            continue
        col = {}
        for r in rows[1:]:
            if len(r) <= j:
                continue
            lvl = num(r[0])
            os_ = num(r[j])
            if lvl is not None and os_ is not None and os_ > 0:
                col[int(lvl)] = os_
        if col:
            out[d] = col
    return out


def load_strangle_log():
    rows_out = []
    try:
        rows = fetch_csv("strangle_log")
    except Exception as e:
        print(f"WARN strangle_log: {e}", file=sys.stderr)
        return rows_out
    hdr = rows[0] if rows else []
    for r in rows[1:]:
        if not any(x.strip() for x in r if x):
            continue
        rows_out.append({
            "week": r[0] if len(r) > 0 else "",
            "entry": norm_date(r[1]) if len(r) > 1 else None,
            "expiry": norm_date(r[2]) if len(r) > 2 else None,
            "vhsi": num(r[3]) if len(r) > 3 else None,
            "hsi": num(r[4]) if len(r) > 4 else None,
            "mode": r[5].strip() if len(r) > 5 and r[5].strip() else "",
            "adjK": num(r[8]) if len(r) > 8 else None,
            "adjL": num(r[9]) if len(r) > 9 else None,
            "expiry_hsi": num(r[12]) if len(r) > 12 else None,
            "result": r[13].strip() if len(r) > 13 else "",
        })
    return rows_out


def backfill_strangle(strangle, kline_hsi):
    last_k = max(kline_hsi) if kline_hsi else None
    for w in strangle:
        if not w["mode"] or w["mode"] == "SKIP":
            continue
        e, x = w["entry"], w["expiry"]
        if not e or not x or not last_k:
            continue
        days = [k for k in sorted(kline_hsi) if e <= k <= x]
        if not days:
            continue
        wk_hi = max(kline_hsi[k][1] for k in days)
        wk_lo = min(kline_hsi[k][2] for k in days)
        w["wk_hi"], w["wk_lo"] = wk_hi, wk_lo
        if w["expiry_hsi"] is None:
            w["expiry_hsi"] = kline_hsi[days[-1]][3]
        if w["result"]:
            continue
        if x > last_k:
            w["result"] = "OPEN"
            continue
        K, L = w["adjK"], w["adjL"]
        if w["mode"] == "Full" and K and L:
            w["result"] = "WIN" if (wk_hi < K and wk_lo > L) else "LOSS"
        elif w["mode"] == "PutOnly" and L:
            w["result"] = "WIN" if wk_lo > L else "LOSS"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    kline_hsi, kline_vhsi = load_kline()
    s4 = load_sheet4()
    hsi_close = load_hsi_close()
    cbbc = load_cbbc()
    strangle = load_strangle_log()
    backfill_strangle(strangle, kline_hsi)

    # ── 合併每日序列 ──
    dates = sorted(set(kline_hsi) | set(s4) | set(cbbc) | set(hsi_close))
    series = []
    for d in dates:
        ohlc = kline_hsi.get(d)
        s4r = s4.get(d)
        if s4r:
            o, h, l, c = s4r["o"], s4r["h"], s4r["l"], s4r["c"]
            v = s4r["v"] or kline_vhsi.get(d)
        elif ohlc:
            o, h, l, c = ohlc
            v = kline_vhsi.get(d)
        else:
            o = h = l = None
            c = hsi_close.get(d)
            v = None
        if c is None:
            continue
        series.append({"d": d, "o": o, "h": h, "l": l, "c": c, "v": v})
    n = len(series)

    # ── EM 階梯（同工作表4公式一致）──
    for i, row in enumerate(series):
        em = {}
        for key, label, div in HORIZONS:
            if row["v"]:
                e = row["c"] * (row["v"] / 100.0) / div
                em[key] = [round(row["c"] + e, 1), round(row["c"] - e, 1)]
            else:
                em[key] = None
        row["em"] = em

    # ── CBBC 最大群（牛=現價下最重，熊=現價上最重，OS≥300）──
    for row in series:
        col = cbbc.get(row["d"], {})
        bull = bear = None
        for lvl, os_ in col.items():
            if os_ < MIN_OS:
                continue
            if lvl < row["c"] and (bull is None or os_ > bull[1]):
                bull = (lvl, os_)
            if lvl > row["c"] and (bear is None or os_ > bear[1]):
                bear = (lvl, os_)
        row["cb"] = {"bp": bull[0] if bull else None, "bos": bull[1] if bull else None,
                     "sp": bear[0] if bear else None, "sos": bear[1] if bear else None}

    # ── 前瞻實際 + 覆蓋判定 ──
    hkeys = [k for k, _, _ in HORIZONS]
    for i, row in enumerate(series):
        fwd = {}
        for key in hkeys:
            N = int(key)
            j0, j1 = i + 1, min(i + N, n - 1)
            if j0 > j1:
                fwd[key] = None
                continue
            hi = [series[j]["h"] for j in range(j0, j1 + 1) if series[j]["h"] is not None]
            lo = [series[j]["l"] for j in range(j0, j1 + 1) if series[j]["l"] is not None]
            if not hi or not lo:
                fwd[key] = None
                continue
            full = (i + N) <= (n - 1)
            fwd[key] = {"hi": max(hi), "lo": min(lo), "n": j1 - j0 + 1, "full": full}
        row["fwd"] = fwd

    # ── 統計 ──
    cov_stats = {}
    for key, label, _ in HORIZONS:
        N = int(key)
        tot = cov = upb = dnb = 0
        for i, row in enumerate(series):
            f = row["fwd"].get(key)
            em = row["em"].get(key)
            if not f or not f.get("full") or not em or row["h"] is None:
                continue
            tot += 1
            if f["hi"] > em[0]:
                upb += 1
            if f["lo"] < em[1]:
                dnb += 1
            if f["hi"] <= em[0] and f["lo"] >= em[1]:
                cov += 1
        cov_stats[key] = {"label": label, "n": tot, "covered": cov,
                          "pct": round(100 * cov / tot, 1) if tot else None,
                          "up_break": upb, "dn_break": dnb}

    cl_stats = {}
    for H in (5, 21):
        bh = bt = bn = 0
        sh = st = sn = 0
        for i, row in enumerate(series):
            j1 = min(i + H, n - 1)
            if i + 1 > j1 or row["h"] is None:
                continue
            hi = [series[j]["h"] for j in range(i + 1, j1 + 1) if series[j]["h"] is not None]
            lo = [series[j]["l"] for j in range(i + 1, j1 + 1) if series[j]["l"] is not None]
            full = (i + H) <= (n - 1)
            if row["cb"]["bp"] and lo:
                bn += 1
                if min(lo) <= row["cb"]["bp"] and full:
                    bh += 1
                elif min(lo) <= row["cb"]["bp"]:
                    bh += 0
            if row["cb"]["sp"] and hi:
                sn += 1
                if max(hi) >= row["cb"]["sp"] and full:
                    sh += 1
                elif max(hi) >= row["cb"]["sp"]:
                    sh += 0
            # 只計完整窗口
            if not full:
                if row["cb"]["bp"] and lo and min(lo) <= row["cb"]["bp"]:
                    bn -= 1
                if row["cb"]["sp"] and hi and max(hi) >= row["cb"]["sp"]:
                    sn -= 1
        cl_stats[str(H)] = {
            "bull": {"n": bn, "hit": bh, "pct": round(100 * bh / bn, 1) if bn else None},
            "bear": {"n": sn, "hit": sh, "pct": round(100 * sh / sn, 1) if sn else None},
        }

    # ── 街貨矩陣（sparse）──
    heat = {d: sorted(col.items()) for d, col in cbbc.items()}
    max_os = 0.0
    for col in cbbc.values():
        for os_ in col.values():
            max_os = max(max_os, os_)

    payload = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "range": {"start": series[0]["d"], "end": series[-1]["d"], "days": n},
        "horizons": [{"key": k, "label": lb} for k, lb, _ in HORIZONS],
        "series": series,
        "heat": heat,
        "max_os": max_os,
        "stats": {"coverage": cov_stats, "cluster": cl_stats},
        "strangle": strangle,
    }

    js = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if args.out:
        Path(args.out).write_text(js)
        print(f"OK wrote {args.out} ({len(js)/1024:.0f} KB, {n} days)")
    else:
        print(js)


if __name__ == "__main__":
    main()
