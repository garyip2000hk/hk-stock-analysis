#!/usr/bin/env python3
"""US 大型股 Wheel 掃描（short put 收貨 → covered call 收租）。

數據：IB 日K（spot + HV20 做 IV proxy）＋ Yahoo calendarEvents（業績日過濾）。
⚠️ 無 OPRA 訂閱，期權金用 HV20 BS 估算——實際盤口 premium 可能更厚（put skew），
   此處只做排序參考；落盤前必須對真實盤口。
輸出：options_data/us_wheel.json
"""
import json
import math
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
IB_KLINE = Path("/home/workspace/Desktop/db/IB/Kline/kline_ib_day.parquet")
OUT = HERE / "options_data" / "us_wheel.json"
CRUMB_CACHE = HERE / "options_data" / ".yahoo_crumb.json"

UNIVERSE = ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "META", "TSLA"]
DTE_CHOICES = [30, 45]
PUT_DELTA = 0.30
CC_DELTA = 0.25
R = 0.035
Z_PUT = 0.5244   # -Phi^-1(0.30)
Z_CC = 0.6745    # Phi^-1(0.75)


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_put(s, k, t, vol):
    if t <= 0 or vol <= 0:
        return 0.0
    d1 = (math.log(s / k) + (R + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    return k * math.exp(-R * t) * _norm_cdf(-d2) - s * _norm_cdf(-d1)


def bs_call(s, k, t, vol):
    if t <= 0 or vol <= 0:
        return 0.0
    d1 = (math.log(s / k) + (R + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    return s * _norm_cdf(d1) - k * math.exp(-R * t) * _norm_cdf(d2)


def yahoo_session():
    now = time.time()
    if CRUMB_CACHE.exists():
        c = json.loads(CRUMB_CACHE.read_text())
        if now - c["ts"] < 3600:
            return c["cookies"], c["crumb"]
    subprocess.run(["curl", "-s", "-m", "15", "-c", "/tmp/ycookies.txt",
                    "-H", "User-Agent: Mozilla/5.0", "https://fc.yahoo.com",
                    "-o", "/dev/null"], check=False)
    crumb = subprocess.run(["curl", "-s", "-m", "15", "-b", "/tmp/ycookies.txt",
                            "-H", "User-Agent: Mozilla/5.0",
                            "https://query2.finance.yahoo.com/v1/test/getcrumb"],
                           capture_output=True, text=True).stdout.strip()
    CRUMB_CACHE.write_text(json.dumps({"ts": now, "cookies": "/tmp/ycookies.txt",
                                       "crumb": crumb}))
    return "/tmp/ycookies.txt", crumb


def next_earnings(sym: str, cookies: str, crumb: str) -> str | None:
    url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
           f"?modules=calendarEvents&crumb={crumb}")
    out = subprocess.run(["curl", "-s", "-m", "15", "-b", cookies,
                          "-H", "User-Agent: Mozilla/5.0", url],
                         capture_output=True, text=True).stdout
    try:
        ev = json.loads(out)["quoteSummary"]["result"][0]["calendarEvents"]["earnings"]
        dates = ev.get("earningsDate") or []
        return dates[0]["fmt"] if dates else None
    except Exception:
        return None


def scan():
    df = pd.read_parquet(IB_KLINE)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cookies, crumb = yahoo_session()
    rows = []
    for sym in UNIVERSE:
        g = df[df.symbol == sym].sort_values("date")
        if len(g) < 30:
            continue
        closes = g.close.astype(float)
        spot = float(closes.iloc[-1])
        ret = closes.pct_change().dropna()
        hv20 = float(ret.tail(20).std() * math.sqrt(252) * 100)
        vol = max(hv20, 15.0) / 100
        earn = next_earnings(sym, cookies, crumb)
        time.sleep(0.3)
        best = None
        for dte in DTE_CHOICES:
            t = dte / 365
            k = spot * math.exp(-(Z_PUT * vol * math.sqrt(t)) + (R + 0.5 * vol * vol) * t)
            k = round(k)
            prem = bs_put(spot, k, t, vol)
            ann = prem / k * (365 / dte) * 100
            in_earn = bool(earn) and earn <= (pd.Timestamp(today) + pd.Timedelta(days=dte)).strftime("%Y-%m-%d")
            cand = {"dte": dte, "strike": k, "premium_est": round(prem, 2),
                    "ann_yield_pct": round(ann, 1),
                    "breakeven": round(k - prem, 2),
                    "cash_needed": k * 100,
                    "earnings_in_window": in_earn}
            if in_earn:
                continue
            if best is None or cand["ann_yield_pct"] > best["ann_yield_pct"]:
                best = cand
        cc_t = 30 / 365
        cc_k = spot * math.exp(Z_CC * vol * math.sqrt(cc_t) + (R + 0.5 * vol * vol) * cc_t)
        cc_prem = bs_call(spot, round(cc_k), cc_t, vol)
        rows.append({
            "symbol": sym, "spot": round(spot, 2), "hv20": round(hv20, 1),
            "next_earnings": earn,
            "short_put": best,
            "covered_call_30d": {"strike": round(cc_k), "premium_est": round(cc_prem, 2),
                                 "ann_yield_pct": round(cc_prem / spot * (365 / 30) * 100, 1)},
        })
    rows.sort(key=lambda r: -(r["short_put"]["ann_yield_pct"] if r["short_put"] else 0))
    out = {"as_of": today, "iv_proxy": "HV20（無 OPRA，估算盤口前參考）",
           "rules": "業績日喺期內 → 唔做；short put delta≈0.30；接貨後 covered call delta≈0.25",
           "rows": rows}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"written {OUT}")
    for r in rows:
        sp = r["short_put"]
        tag = "✅" if sp else "⛔ 業績期內"
        sp_txt = (f"K{sp['strike']} prem${sp['premium_est']} 年化{sp['ann_yield_pct']}%"
                  if sp else f"（next earn {r['next_earnings']}）")
        print(f"  {r['symbol']:6} spot {r['spot']:>8} HV20 {r['hv20']:>5.1f}  {tag} {sp_txt}  earn={r['next_earnings']}")


if __name__ == "__main__":
    scan()
