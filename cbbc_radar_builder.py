#!/usr/bin/env python3
"""
牛熊波幅雷達 dataset 生成器（OpenD 版，取代舊 Manus 私有 repo）

每晚 21:30 HKT 跑（由 cbbc_radar_scheduler.py 排程）：
  1. HSI / VHSI 收市快照（OpenD）
  2. 恒指牛熊證名單：Desktop/db/CBBC/ 最新檔（un=='HSI'）
  3. OpenD 批量快照 → 收回價 + 街貨量（百萬份）
  4. 200 點區間聚合 → 覆蓋分／加權分／判定
  5. 歷史日記錄存 cbbc_radar/days/YYYY-MM-DD.json（append-only），回 5 日
  6. 輸出 cbbc_radar/dataset_latest.json；設 CBBC_RADAR_UPDATE_URL+SECRET 就 POST 上 gsmart-box

公式（由舊 bundled dataset 反推驗證）：
  expectedMove1Day = close × vhsi(小數) / √252
  overlapFraction  = 區間同屠殺區 [close, upperDay] 或 [lowerDay, close] 嘅重疊比例（分母 hi−lo）
  distanceWeight   = max(0.15, 1 − |mid−close| / EM)
  coverageScore    = (bearCovered − bullCovered) / Σ
  weightedScore    = (Σ bear加權 − Σ bull加權) / Σ
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

HKT = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "cbbc_radar"
DAYS_DIR = OUT_DIR / "days"
DATASET_PATH = OUT_DIR / "dataset_latest.json"
CBBC_DB = Path("/home/workspace/Desktop/db/CBBC")
HSI = "HK.800000"
VHSI = "HK.800125"
NIGHT_FUT = "HK.HSImain"
# 美股上市嘅恒指相關 ADR 代理籃（OpenD 可攞，用嚟估「先」方向；等權平均，唔係官方港股 ADR 指數）
ADR_BASKET = ["US.BABA", "US.JD", "US.BIDU", "US.NTES", "US.PDD", "US.XPEV", "US.NIO", "US.LI", "US.TCOM", "US.BILI", "US.BEKE", "US.ZTO"]
ZONE_SIZE = 200
KEEP_DAYS = 5
MAX_WAIT_SECONDS = 120


def log(msg):
    print(f"[{datetime.now(HKT):%H:%M:%S}] {msg}", flush=True)


def is_trading_day(ctx, today_str):
    try:
        start_dt = (datetime.now(HKT) - timedelta(days=20)).strftime("%Y-%m-%d")
        ret, days = ctx.request_trading_days(market="HK", start=start_dt, end=today_str)
        if ret != 0:
            return True  # 問唔到就假設交易日，唔好亂 skip
        return today_str in [d["time"][:10] for d in days]
    except Exception:
        return True


def snapshot(ctx, codes):
    from futu import RET_OK
    frames = []
    for i in range(0, len(codes), 200):
        batch = codes[i : i + 200]
        ret, df = ctx.get_market_snapshot(batch)
        if ret == RET_OK and not df.empty:
            frames.append(df)
        else:
            log(f"⚠️ 快照批次 {i} 失敗: {df}")
        time.sleep(0.4)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def latest_cbbc_file():
    files = sorted(CBBC_DB.glob("cbbc_*.parquet"), reverse=True)
    return files[0] if files else None


def cbbc_codes():
    f = latest_cbbc_file()
    if f is None:
        raise RuntimeError("搵唔到 CBBC scrape 檔")
    df = pd.read_parquet(f)
    hsi_df = df[df["un"] == "HSI"]
    codes = ["HK." + s.zfill(5) for s in hsi_df["sym"].astype(str)]
    log(f"恒指牛熊證名單: {f.name} → {len(codes)} 隻")
    return codes, f.name


def previous_night_premarket(ctx, trading_date, close):
    """前一晚嘅「先」方向：夜期（HK.HSImain 夜市收 vs 日市收）+ ADR 代理籃（OpenD 美股）。"""
    from datetime import date as _date
    try:
        y, m, d = map(int, trading_date.split("-"))
        day = _date(y, m, d)
        prev_day = (day - timedelta(days=7))
        ret, kdf, _ = ctx.request_history_kline(
            NIGHT_FUT, start=prev_day.isoformat(), end=trading_date,
            ktype="K_5M", max_count=1000, session="ALL",
        )
        night_change = None
        if ret == 0 and len(kdf):
            kdf = kdf.copy()
            kdf["d"] = kdf["time_key"].str[:10]
            kdf["t"] = kdf["time_key"].str[11:16]
            day_close = kdf[(kdf["d"] < trading_date) & (kdf["t"] <= "16:30")]
            night_bars = kdf[((kdf["d"] < trading_date) & (kdf["t"] >= "17:00")) | ((kdf["d"] == trading_date) & (kdf["t"] <= "03:05"))]
            if len(day_close) and len(night_bars):
                night_change = float(night_bars.iloc[-1]["close"] - day_close.iloc[-1]["close"])

        adr_change = None
        r2, us = ctx.get_market_snapshot(ADR_BASKET)
        if r2 == 0 and len(us):
            us = us[(us["last_price"] > 0) & (us["prev_close_price"] > 0)]
            if len(us) >= 3:
                pct = ((us["last_price"] / us["prev_close_price"]) - 1).mean()
                adr_change = round(close * float(pct), 1)

        adjustment = night_change if night_change is not None else adr_change
        if adjustment is None:
            return None
        direction = "up" if adjustment > 0 else ("down" if adjustment < 0 else None)
        confirmed = (
            night_change is not None and adr_change is not None
            and (night_change > 0) == (adr_change > 0)
        )
        src = "夜期+ADR（OpenD）" if adr_change is not None else "夜期（OpenD）"
        return {
            "adrChange": None if adr_change is None else round(adr_change, 1),
            "nightFuturesChange": None if night_change is None else round(night_change, 1),
            "adjustment": round(adjustment, 1),
            "initialDirection": direction,
            "isConfirmed": bool(confirmed),
            "adjustedReference": round(close + adjustment, 2),
            "sourceLabel": src + ("，同向確認" if confirmed else ""),
            "status": f"先{'升' if adjustment > 0 else ('跌' if adjustment < 0 else '平')}，等待目標",
        }
    except Exception as e:
        log(f"⚠️ 前一晚 premarket 失敗（唔影響主體）: {e}")
        return None


def build_day(ctx, trading_date, prediction_date=None):
    mmdd = trading_date[5:].replace("-", "-")
    # ── HSI + VHSI 快照 ──
    # 用 prev_close_price（trading_date 當日嘅正式收市價），唔係 last_price（即市價）
    idx = snapshot(ctx, [HSI, VHSI])
    hsi = idx[idx["code"] == HSI].iloc[0]
    vhsi_row = idx[idx["code"] == VHSI].iloc[0]
    close = float(hsi["prev_close_price"])
    open_p = float(hsi["open_price"])
    high = float(hsi["high_price"])
    low = float(hsi["low_price"])
    vhsi = float(vhsi_row["prev_close_price"]) / 100.0  # 存小數（與舊格式一致）
    em1d = close * vhsi / (252 ** 0.5)
    upper_day = round(close + em1d)
    lower_day = round(close - em1d)
    log(f"HSI 收 {close:,.2f}（{mmdd}，prev_close） VHSI {vhsi*100:.2f} EM±{em1d:.1f} → {lower_day}-{upper_day}")

    # ── 牛熊證快照 ──
    codes, src_file = cbbc_codes()
    snap = snapshot(ctx, codes)
    snap = snap[snap["wrt_recovery_price"] > 0].copy()
    log(f"快照返回 {len(snap)} 隻（名單 {len(codes)}）")

    # ── 區間聚合 ──
    snap["zone_lo"] = (snap["wrt_recovery_price"] // ZONE_SIZE * ZONE_SIZE).astype(int)
    agg = snap.groupby("zone_lo").agg(out=("wrt_street_vol", "sum"), n=("code", "count"))
    zones = []
    bull_covered = bear_covered = bull_w = bear_w = 0.0
    for lo in sorted(agg.index):
        hi = lo + ZONE_SIZE - 1
        mid = lo + ZONE_SIZE / 2 - 0.5
        out = float(agg.loc[lo, "out"]) / 1e6  # 百萬份
        side = "lower" if mid < close else "upper"
        if side == "upper":
            covered_pts = max(0, min(hi, upper_day) - max(lo, close))
        else:
            covered_pts = max(0, min(hi, close) - max(lo, lower_day))
        ov_frac = covered_pts / (hi - lo) if side else 0
        cov_amt = out * ov_frac
        dist_w = max(0.15, 1 - abs(mid - close) / em1d) if em1d > 0 else 0.15
        w_contrib = cov_amt * dist_w
        if out <= 0 and ov_frac <= 0:
            continue
        if side == "lower":
            bull_covered += cov_amt
            bull_w += w_contrib
        else:
            bear_covered += cov_amt
            bear_w += w_contrib
        zones.append({
            "lo": int(lo), "hi": int(hi), "mid": mid,
            "outstanding": round(out, 2), "note": "",
            "side": side,
            "overlapFraction": round(ov_frac, 4),
            "coveredAmount": round(cov_amt, 4),
            "distanceWeight": round(dist_w, 4),
            "weightedContribution": round(w_contrib, 4),
        })

    # 只留 ±3000 點內嘅 zones（遠期深價外 CBBC 對雷達冇意義，舊版都只顯示 ±2700 左右）
    zones = [z for z in zones if abs(z["mid"] - close) <= 3000 or z["overlapFraction"] > 0]
    zones.sort(key=lambda z: z["lo"], reverse=True)

    total_cov = bull_covered + bear_covered
    total_w = bull_w + bear_w
    coverage_score = ((bear_covered - bull_covered) / total_cov) if total_cov else 0
    weighted_score = ((bear_w - bull_w) / total_w) if total_w else 0

    # ── 判定：「後」方向用當日 08:00 結算牛熊數據；「先」方向用前一晚夜期+ADR ──
    premarket = previous_night_premarket(ctx, prediction_date or trading_date, close)
    if weighted_score >= 0.25:
        back = "上屠熊"
    elif weighted_score <= -0.25:
        back = "下殺牛"
    else:
        back = "窄幅波動"
    if premarket and premarket.get("initialDirection"):
        verdict = f"先{'升' if premarket['initialDirection'] == 'up' else '跌'}後{back}"
    else:
        verdict = back

    # ── targetFocus ──
    side = "upper" if weighted_score > 0.25 else ("lower" if weighted_score < -0.25 else None)
    target_focus = None
    if side:
        cand = [z for z in zones if z["side"] == side and z["weightedContribution"] > 0]
        if cand:
            top = max(cand, key=lambda z: z["weightedContribution"])
            raw_d = max(0, (top["lo"] - close) if side == "upper" else (close - top["hi"]))
            band = _band(raw_d)
            target_focus = {
                "side": side, "lo": top["lo"], "hi": top["hi"],
                "rawDistance": round(raw_d, 1), "adjustedDistance": round(raw_d, 1),
                "band": band,
            }

    log(f"判定: {verdict} | coverage {coverage_score:+.4f} | weighted {weighted_score:+.4f} | zones {len(zones)}")
    record = {
        "date": trading_date, "mmdd": mmdd,
        "settlementDate": trading_date,
        "close": round(close, 2), "open": round(open_p, 2), "high": round(high, 2), "low": round(low, 2),
        "vhsi": round(vhsi, 6),
        "expectedMove1Day": round(em1d, 4),
        "upperDay": upper_day, "lowerDay": lower_day,
        "zones": zones,
        "bullCovered": round(bull_covered, 4), "bearCovered": round(bear_covered, 4),
        "bullWeighted": round(bull_w, 4), "bearWeighted": round(bear_w, 4),
        "coverageScore": round(coverage_score, 4), "weightedScore": round(weighted_score, 4),
        "verdict": verdict, "premarket": premarket, "targetFocus": target_focus,
        # 信號應用日 = 信號日本身（08:10 已用當日 08:00 結算數據，唔再順延）
        "predictionDate": prediction_date or trading_date,
    }
    return record


def _band(dist):
    if dist <= 100:
        return {"label": "貼身區", "detail": "距離極短", "historicalRate": None, "tone": "watch", "stakeHint": "即日觀察"}
    if dist <= 200:
        return {"label": "101–200點", "detail": "高勝率大注區", "historicalRate": 75, "tone": "strong", "stakeHint": "大注觀察"}
    if dist <= 350:
        return {"label": "201–350點", "detail": "中距離", "historicalRate": None, "tone": "neutral", "stakeHint": "觀察"}
    return {"label": "350點以上", "detail": "偏遠區", "historicalRate": None, "tone": "neutral", "stakeHint": "參考"}


def build_dataset(record):
    DAYS_DIR.mkdir(parents=True, exist_ok=True)
    (DAYS_DIR / f"{record['date']}.json").write_text(json.dumps(record, ensure_ascii=False))
    files = sorted(DAYS_DIR.glob("*.json"), reverse=True)[:KEEP_DAYS]
    days = [json.loads(p.read_text()) for p in files]
    now = datetime.now(HKT).isoformat(timespec="seconds")
    ds = {
        "generatedAt": now,
        "refreshedAt": now,
        "sourceNote": "每個交易日 08:10（HKT）用昨日結算牛熊數據判當日；「先」向按前晚夜期及 ADR（OpenD）。",
        "availableDates": [d["date"] for d in days if days],
        "days": days,
    }
    DATASET_PATH.write_text(json.dumps(ds, ensure_ascii=False))
    return ds


def push(ds):
    url = os.environ.get("CBBC_RADAR_UPDATE_URL")
    secret = os.environ.get("CBBC_RADAR_UPDATE_SECRET")
    if not url or not secret:
        log("PUSH_SKIP: 未設 CBBC_RADAR_UPDATE_URL / SECRET（只寫本地檔）")
        return False
    import urllib.request
    body = json.dumps(ds).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "x-update-secret": secret,
            # Manus edge WAF 會 403 擋 Python-urllib 預設 UA，要扮 browser
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            log(f"PUSH_OK: {url} → HTTP {r.status}")
            return True
    except Exception as e:
        log(f"PUSH_FAIL: {e}")
        return False


def main():
    from futu import OpenQuoteContext
    now_hkt = datetime.now(HKT)
    today = now_hkt.strftime("%Y-%m-%d")
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        if not is_trading_day(ctx, today):
            log("SKIP: 今日非交易日")
            return 0
        # 規則（用戶定）：朝早攞嘅永遠係「上一個交易日」嘅結算數據
        import datetime as _dt
        pred = today  # 適用日 = 今日
        trade_dt = (_dt.datetime.strptime(today, "%Y-%m-%d") - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        while not is_trading_day(ctx, trade_dt):
            trade_dt = (_dt.datetime.strptime(trade_dt, "%Y-%m-%d") - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        log(f"結算數據日: {trade_dt}（適用日: {pred}）")
        record = build_day(ctx, trade_dt, pred)
        ds = build_dataset(record)
        log(f"已寫 {DATASET_PATH}（days={len(ds['days'])}）")
        return 0 if push(ds) else 1
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
