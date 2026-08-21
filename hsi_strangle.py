"""
hsi_strangle.py — 恒指 Short Strangle（OpenD 版）

將 gsmart-box 嘅 HSI Short Strangle 策略邏輯，改用 OpenD 即市數據重現：
  - VHSI (HK.800125) 分級 → 決定入唔入場 / 做單邊定雙邊
  - 恒指價 (HK.800000) → 計 1 星期預期波幅 → 揀行使價
  - 恒指期權鏈 (HK.800000 期權) → 搵實際合約 + 即市買賣價 / IV / delta

VHSI 分級（同 gsmart-box strategy.ts detectMode 一致）：
  < 20  → SKIP     不入場
  20–22 → PutOnly  只做 Short Put（L 擴大 ×1.3）
  22–40 → Full     SC + SP 雙邊
  > 40  → HighVol  SC + SP 各 1 手

行使價（1 星期預期波幅 EM = HSI × VHSI/100 × √(5/252)）：
  K (Short Call) = round up 到 200 點  (HSI + EM)
  L (Short Put)  = round down 到 200 點 (HSI − EM × putMult)
"""
import argparse
import json
import math
from datetime import datetime, timedelta, timezone

import futu
import pandas as pd

HOST = "127.0.0.1"
PORT = 11111
HSI = "HK.800000"
VHSI = "HK.800125"
HKT = timezone(timedelta(hours=8))
WEEK_TRADING_DAYS = 5
YEAR_TRADING_DAYS = 252

MODE_CONFIGS = {
    "SKIP": {"label": "不入場", "call": False, "put": False, "putMult": 1},
    "PutOnly": {"label": "Short Put Only", "call": False, "put": True, "putMult": 1.3},
    "Full": {"label": "Full Short Strangle (SC+SP)", "call": True, "put": True, "putMult": 1},
    "HighVol": {"label": "High Vol (SC+SP 各1手)", "call": True, "put": True, "putMult": 1},
}


def now_hkt():
    return datetime.now(HKT)


def log(msg):
    print(f"[{now_hkt():%H:%M:%S}] {msg}", flush=True)


def connect():
    return futu.OpenQuoteContext(host=HOST, port=PORT)


def detect_mode(vhsi):
    if vhsi is None:
        return None
    if vhsi < 20:
        return "SKIP"
    if vhsi < 22:
        return "PutOnly"
    if vhsi <= 40:
        return "Full"
    return "HighVol"


def round_up(n, step=200):
    return math.ceil(n / step) * step


def round_down(n, step=200):
    return math.floor(n / step) * step


def calc_strikes(hsi, vhsi, mode):
    cfg = MODE_CONFIGS[mode]
    em = hsi * (vhsi / 100) * math.sqrt(WEEK_TRADING_DAYS / YEAR_TRADING_DAYS)
    if mode == "SKIP":
        return {"em": round(em, 1), "K": None, "L": None}
    K = round_up(hsi + em) if cfg["call"] else None
    L = round_down(hsi - em * cfg["putMult"]) if cfg["put"] else None
    return {"em": round(em, 1), "K": K, "L": L}


def get_index_snapshot(ctx):
    ret, snap = ctx.get_market_snapshot([VHSI, HSI])
    if ret != futu.RET_OK:
        return None, None
    out = {}
    for code in (VHSI, HSI):
        row = snap[snap["code"] == code]
        if len(row):
            out[code] = float(row.iloc[0]["last_price"])
    return out.get(VHSI), out.get(HSI)


def pick_expiry(ctx, min_dte=20, max_dte=60):
    ret, df = ctx.get_option_expiration_date(HSI)
    if ret != futu.RET_OK or df is None or len(df) == 0:
        return None
    today = pd.Timestamp(now_hkt().date())
    expiries = sorted(pd.to_datetime(df["strike_time"]).tolist())
    cands = []
    for e in expiries:
        dte = (e - today).days
        if min_dte <= dte <= max_dte:
            cands.append((abs(dte - 30), e))
    if cands:
        return min(cands)[1]
    return expiries[0]


def pull_chain(ctx, expiry):
    ymd = expiry.strftime("%Y-%m-%d") if hasattr(expiry, "strftime") else str(expiry)[:10]
    ret, chain = ctx.get_option_chain(
        code=HSI, start=ymd, end=ymd,
        option_type=futu.OptionType.ALL, option_cond_type=futu.OptionCondType.ALL,
    )
    if ret != futu.RET_OK or chain is None or len(chain) == 0:
        return None
    chain = chain.copy()
    chain["expiry"] = chain["strike_time"].astype(str).str[:10]
    chain["strike"] = chain["strike_price"]

    codes = chain["code"].tolist()
    frames = []
    for i in range(0, len(codes), 400):
        ret2, snap = ctx.get_market_snapshot(codes[i:i + 400])
        if ret2 == futu.RET_OK and snap is not None:
            frames.append(snap)
    if not frames:
        return chain
    snap = pd.concat(frames, ignore_index=True)
    cols = ["code"]
    for c in ["last_price", "bid_price", "ask_price", "volume",
              "option_open_interest", "option_implied_volatility",
              "option_delta", "option_gamma", "option_theta",
              "option_vega", "option_rho"]:
        if c in snap.columns:
            cols.append(c)
    snap = snap[cols]
    return chain.merge(snap, on="code", how="left")


def find_contract(chain, cp, strike):
    rows = chain[(chain["option_type"] == cp) & (chain["strike"] == float(strike))]
    if len(rows) == 0:
        return None
    r = rows.iloc[0]
    return {
        "code": r["code"],
        "strike": float(r["strike"]),
        "bid": float(r.get("bid_price") or 0),
        "ask": float(r.get("ask_price") or 0),
        "iv": float(r.get("option_implied_volatility") or 0),
        "delta": float(r.get("option_delta") or 0),
        "oi": int(r.get("option_open_interest") or 0),
    }


def atm_iv(chain, spot):
    c = chain.copy()
    c["dist"] = (c["strike"] - spot).abs()
    near = c.nsmallest(6, "dist")
    ivs = [float(r["option_implied_volatility"]) for _, r in near.iterrows()
           if pd.notna(r.get("option_implied_volatility")) and float(r["option_implied_volatility"]) > 0]
    if not ivs:
        return None
    return round(sum(ivs) / len(ivs), 2)


def is_friday():
    return now_hkt().weekday() == 4


def run():
    ctx = connect()
    try:
        vhsi, hsi = get_index_snapshot(ctx)
        log(f"VHSI={vhsi}  HSI={hsi}")

        mode = detect_mode(vhsi)
        cfg = MODE_CONFIGS.get(mode, {})
        log(f"模式: {mode} ({cfg.get('label')})")

        expiry = pick_expiry(ctx)
        log(f"目標到期月: {expiry.strftime('%Y-%m-%d') if expiry else None}")

        chain = pull_chain(ctx, expiry) if expiry else None

        strikes = calc_strikes(hsi, vhsi, mode) if (hsi and vhsi and mode) else None

        result = {
            "time": now_hkt().isoformat(),
            "vhsi": vhsi,
            "hsi": hsi,
            "mode": mode,
            "mode_label": cfg.get("label"),
            "friday": is_friday(),
            "expiry": expiry.strftime("%Y-%m-%d") if expiry else None,
            "strikes": strikes,
            "atm_iv": atm_iv(chain, hsi) if (chain is not None and hsi) else None,
            "contracts": {},
            "note": "",
        }

        if mode == "SKIP":
            result["note"] = "VHSI < 20，本週不入場"
        elif strikes and chain is not None:
            legs = []
            if strikes["K"] is not None:
                legs.append(("SC", "CALL", strikes["K"]))
            if strikes["L"] is not None:
                legs.append(("SP", "PUT", strikes["L"]))
            for tag, cp, k in legs:
                c = find_contract(chain, cp, k)
                result["contracts"][tag] = c
                log(f"  {tag} {k}: {c}")
        elif is_friday():
            result["note"] = "今日係星期五 — 嚴禁加注/開新倉"

        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return result
    finally:
        ctx.close()


if __name__ == "__main__":
    run()
