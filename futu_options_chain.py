"""
futu_options_chain.py — 期權鏈即時數據（OpenD 版）

用途：替代 HKEX《股票期權日報》嘅「即時」缺口。HKEX 日報係 T+1 收市後先出，
      OpenD 攞到即市每口行使價嘅 IV / delta / greeks / OI / 買賣價。
      歷史回測仍用 HKEX（OpenD 冇歷史期權鏈），呢個模組專責「今日」活數據。

輸出：
  /home/workspace/stock-analysis/options_data/futu_chain_YYYYMMDD.parquet  （全鏈）
  /home/workspace/stock-analysis/options_data/futu_atm_iv_YYYYMMDD.parquet  （每股 ATM IV 摘要）

CLI:
  python3 futu_options_chain.py                     # 全部 135 隻期權標的
  python3 futu_options_chain.py --stock 00700       # 單隻
  python3 futu_options_chain.py --min-dte 20 --max-dte 60   # 揀到期月 DTE 範圍
"""
import argparse
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import futu
import pandas as pd

BASE = Path("/home/workspace/stock-analysis")
OUT_DIR = BASE / "options_data"
SPEC_PATH = OUT_DIR / "contract_specs.json"
KLINE_PATH = Path("/home/workspace/Desktop/db/Futu/Kline/kline_day.parquet")

HOST = "127.0.0.1"
PORT = 11111

HKT = timezone(timedelta(hours=8))

# 標準化後嘅輸出欄
OUT_COLS = [
    "code", "underlying", "expiry", "strike", "option_type", "lot_size",
    "last_price", "bid_price", "ask_price", "volume",
    "option_open_interest", "option_implied_volatility",
    "option_delta", "option_gamma", "option_theta", "option_vega", "option_rho",
]


def now_hkt():
    return datetime.now(HKT)


def log(msg):
    print(f"[{now_hkt():%H:%M:%S}] {msg}", flush=True)


def connect():
    return futu.OpenQuoteContext(host=HOST, port=PORT)


def underlyings():
    if not SPEC_PATH.exists():
        return []
    specs = json.loads(SPEC_PATH.read_text())
    keys = specs if isinstance(specs, list) else list(specs)
    return [f"HK.{k}" if not str(k).startswith("HK.") else str(k) for k in keys]


def latest_close(code):
    """由 OpenD 日K攞最新收市價做 spot。"""
    try:
        df = pd.read_parquet(KLINE_PATH)
        rows = df[df["code"] == code]
        if len(rows):
            return float(rows.sort_values("time_key")["close"].iloc[-1])
    except Exception:
        pass
    return None


def pick_expiry(ctx, code, min_dte, max_dte):
    """揀 DTE 落喺 [min_dte, max_dte] 嘅到期月，優先最接近 30 DTE；否則用最近一個。

    get_option_expiration_date 有頻控（約 30 次/30 秒），失敗會自動等 5 秒重試。
    """
    for attempt in range(3):
        ret, df = ctx.get_option_expiration_date(code)
        if ret == futu.RET_OK and df is not None and len(df) > 0:
            break
        log(f"    ↳ {code} expiry 頻控/失敗 ret={ret} err={str(df)[:60]} (第{attempt+1}次)")
        time.sleep(5)
    else:
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


def collect_chain(ctx, code, expiry):
    """get_option_chain（單一到期月）→ 標準化 DataFrame。"""
    ymd = expiry.strftime("%Y-%m-%d")
    ret, chain = ctx.get_option_chain(
        code=code, start=ymd, end=ymd,
        option_type=futu.OptionType.ALL, option_cond_type=futu.OptionCondType.ALL,
    )
    if ret != futu.RET_OK or chain is None or len(chain) == 0:
        return None
    chain = chain.copy()
    chain["underlying"] = code
    chain["expiry"] = chain["strike_time"].astype(str).str[:10]
    chain["strike"] = chain["strike_price"]
    return chain


def snapshot_chains(ctx, chain, codes):
    """分批攞期權即市 snapshot，merge 返 IV / greeks / OI / 買賣價。"""
    frames = []
    for i in range(0, len(codes), 400):
        ret, snap = ctx.get_market_snapshot(codes[i : i + 400])
        if ret == futu.RET_OK and snap is not None:
            frames.append(snap)
        time.sleep(0.4)
    if not frames:
        return chain
    snap = pd.concat(frames, ignore_index=True)
    # 只保留 snapshot 特有欄，避免同 chain 欄重複
    snap_cols = ["code"]
    for c in ["last_price", "bid_price", "ask_price", "volume",
              "option_open_interest", "option_implied_volatility",
              "option_delta", "option_gamma", "option_theta",
              "option_vega", "option_rho"]:
        if c in snap.columns:
            snap_cols.append(c)
    snap = snap[snap_cols]
    return chain.merge(snap, on="code", how="left")


def compute_atm_iv(chain, spot):
    """ATM IV = 行使價最貼 spot 嘅 call/put IV 平均。"""
    if not spot:
        return None, None
    c = chain.copy()
    c["dist"] = (c["strike"] - spot).abs()
    near = c.nsmallest(4, "dist")
    ivs = []
    for _, r in near.iterrows():
        v = r.get("option_implied_volatility")
        if pd.notna(v) and float(v) > 0:
            ivs.append(float(v))
    if not ivs:
        return None, None
    atm_strike = float(near.iloc[0]["strike"])
    return atm_strike, round(sum(ivs) / len(ivs), 2)


def run_import(stock=None, min_dte=20, max_dte=60):
    if stock:
        s = stock.strip()
        codes = [f"HK.{s}" if not s.startswith("HK.") else s]
    else:
        codes = underlyings()
    if not codes:
        return {"status": "error", "error": "冇期權標的"}

    ctx = connect()
    try:
        rows = []
        for code in codes:
            expiry = pick_expiry(ctx, code, min_dte, max_dte)
            if expiry is None:
                log(f"✗ {code}: 冇到期月")
                continue
            chain = collect_chain(ctx, code, expiry)
            if chain is None:
                log(f"✗ {code}: 冇期權鏈")
                continue
            rows.append(chain)
            time.sleep(1.1)

        if not rows:
            return {"status": "error", "error": "冇拉到任何期權鏈"}

        full = pd.concat(rows, ignore_index=True)

        codes_list = full["code"].tolist()
        log(f"snapshot {len(codes_list)} 口期權…")
        full = snapshot_chains(ctx, full, codes_list)

        out_cols = [c for c in OUT_COLS if c in full.columns]
        out = full[out_cols]

        summary = []
        for code, grp in full.groupby("underlying"):
            spot = latest_close(code)
            atm_strike, atm_iv = compute_atm_iv(grp, spot)
            summary.append({
                "underlying": code,
                "spot": spot,
                "expiry": grp["expiry"].iloc[0],
                "atm_strike": atm_strike,
                "atm_iv": atm_iv,
                "n_contracts": int(len(grp)),
            })

        ymd = now_hkt().strftime("%Y%m%d")
        chain_path = OUT_DIR / f"futu_chain_{ymd}.parquet"
        atm_path = OUT_DIR / f"futu_atm_iv_{ymd}.parquet"
        out.to_parquet(chain_path, index=False)
        pd.DataFrame(summary).to_parquet(atm_path, index=False)

        log(f"✅ 鏈 {len(out)} 行 → {chain_path.name}")
        log(f"✅ ATM IV 摘要 {len(summary)} 行 → {atm_path.name}")

        n_atm = sum(1 for s in summary if s["atm_iv"])
        return {
            "status": "ok",
            "underlyings": len(summary),
            "with_atm_iv": n_atm,
            "chain_rows": int(len(out)),
            "chain_file": str(chain_path),
            "atm_file": str(atm_path),
        }
    finally:
        ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", default=None, help="單隻，如 00700")
    ap.add_argument("--min-dte", type=int, default=20)
    ap.add_argument("--max-dte", type=int, default=60)
    args = ap.parse_args()
    res = run_import(stock=args.stock, min_dte=args.min_dte, max_dte=args.max_dte)
    print(json.dumps(res, ensure_ascii=False, default=str))
