#!/usr/bin/env python3
"""
牛熊波幅雷達 dataset 生成器（OpenD 版，取代舊 Manus 私有 repo）

每朝 08:10 HKT 跑（由 cbbc_radar_scheduler.py 排程，失敗 08:40/09:10 重試）：
  1. HSI / VHSI 收市快照（OpenD）
  2. 恒指牛熊證名單：OpenD get_warrant（HK.800000 全批，status==NORMAL；本地 Desktop/db/CBBC scrape 只做後備）
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

「先」「後」方向（HSI ADR CK sheet 邏輯，2026-08-21 移植）：
  先 = 夜期變化 + HSIADR 指數變化，±50 點 AND 規則（兩邊同向過 50 先有方向；任一邊 ≤50 → 窄幅）
  後 = 相對期指張數（街貨 ÷ 換股比率 ÷ 50，GS 口徑，已 19/19 對數驗證）：
       動態 200 點格（anchor=round(close,100)，同 GS 表）、上下各 3 個最近格、
       格遠邊距離 < DayRange(=(High−Low)/2) 先計入；upSum − downSum > +500 屠熊 / < −500 殺牛
"""
import json
import re
import os
import subprocess
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
HSIADR_URL = "https://www.futunn.com/hk/index/.HSIADR-US"
SIGNAL_THRESHOLD = 50.0  # 「HSI ADR CK」sheet：±50 點先算有方向（AND 規則）
# 恒指成分嘅美股 ADR 代理籃（按恒指約數權重 % 加權，權重季度檢視；OpenD 攞日 K 收市）。
# ⚠️ 唔可以用等權中概籃（2026-08-21 教訓：網易 -5.9%/B站 -3.8% 等細權重股拖到等權 -1.02% →
# 假訊號 -262 點；同一晚加權只係 +4.8 點）。非恒指成分（PDD/NIO/BEKE）唔入籃。
# 騰訊/美團/小米/友邦等冇美股上市 ADR，覆蓋約 20% 恒指權重，數值只係方向代理。
ADR_BASKET = {
    "US.BABA": 8.0,   # 阿里 09991（恒指 8% 上限）
    "US.HSBC": 7.5,   # 匯控 00005
    "US.NTES": 1.5,   # 網易 09999
    "US.JD": 1.0,     # 京東 09618
    "US.BIDU": 0.8,   # 百度 09888
    "US.LI": 0.4,     # 理想 02015
    "US.BILI": 0.3,   # B站 09626
    "US.XPEV": 0.2,   # 小鵬 09868
    "US.ZTO": 0.2,    # 中通 02057
    "US.TCOM": 0.2,   # 攜程 09961
}
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


def cbbc_codes_opend(ctx):
    """恒指牛熊證名單直接由 OpenD get_warrant 攞（唔使本地 scrape 檔）。"""
    from futu import RET_OK, WrtType, SortField
    from futu.quote.quote_get_warrant import Request
    codes, begin = [], 0
    while True:
        req = Request()
        req.begin = begin
        req.num = 200
        req.sort_field = SortField.CODE
        req.ascend = True
        req.type_list = [WrtType.BULL, WrtType.BEAR]
        ret, data = ctx.get_warrant(HSI, req)
        if ret != RET_OK:
            raise RuntimeError(f"get_warrant 失敗: {data}")
        df, last_page, all_count = data
        if df is None or df.empty:
            break
        live = df[df["status"] == "NORMAL"]
        codes.extend(live["stock"].astype(str).tolist())
        got = len(df)
        begin += got
        if last_page or got < 200:
            break
        if all_count is not None and begin >= all_count:
            break
        time.sleep(0.3)
    if not codes:
        raise RuntimeError("get_warrant 返回 0 隻牛熊證")
    log(f"恒指牛熊證名單: OpenD get_warrant → {len(codes)} 隻（NORMAL）")
    return codes, "OpenD get_warrant"


def cbbc_codes_local():
    f = latest_cbbc_file()
    if f is None:
        raise RuntimeError("搵唔到 CBBC scrape 檔")
    df = pd.read_parquet(f)
    hsi_df = df[df["un"] == "HSI"]
    codes = ["HK." + s.zfill(5) for s in hsi_df["sym"].astype(str)]
    log(f"恒指牛熊證名單（後備）: {f.name} → {len(codes)} 隻")
    return codes, f.name


def fetch_hsiadr():
    """富途恒指 ADR 指數（.HSIADR-US）。「HSI ADR CK」sheet 同源嘅 futunn 頁面
    （OpenD 唔支援美股指數報價，實測 get_market_snapshot 回「暂不支持美股指数」）。
    頁面個變化 = HSIADR 值 − HSI 昨日日市收，同 sheet C4 同一口徑。
    回 (指數值, 帶符號變化) 或 None。"""
    try:
        out = subprocess.run(
            ["curl", "-sL", "--max-time", "15",
             "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
             HSIADR_URL],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception as e:
        log(f"⚠️ futunn HSIADR 抓取失敗: {e}")
        return None
    m = re.search(r"mg-r-8 price direct-(?:up|down)[^>]*>\s*([\d,\.]+)", out)
    c = re.search(r"change-price\"[^>]*>\s*([+-])\s*([\d\.]+)", out)
    if not (m and c):
        log("⚠️ futunn HSIADR 頁面 parse 唔到價格/變化（結構改咗？）")
        return None
    val = float(m.group(1).replace(",", ""))
    chg = float(c.group(2)) * (1 if c.group(1) == "+" else -1)
    log(f"HSIADR 指數: {val:,.1f}（{chg:+.1f} 點，futunn）")
    return val, round(chg, 1)


def _sheet_back_direction(snap, close, day_range):
    """「HSI ADR CK」sheet 嘅「後」方向（GS 相對期指張數邏輯）：
    - 動態 200 點格：anchor = round(close,100)，同 GS 街貨分佈表格線一致
    - 上方（熊區）最近 3 格、下方（牛區）最近 3 格
    - 可達：上方格 (格頂−close) < DayRange；下方格 (close−格底) < DayRange
    - upSum − downSum > +500 → 屠熊；< −500 → 殺牛；否則窄幅"""
    anchor = round(close / 100) * 100
    up_sum = dn_sum = 0.0
    up_zones, dn_zones = [], []
    up = snap[snap["wrt_recovery_price"] > close]
    dn = snap[snap["wrt_recovery_price"] < close]
    if len(up):
        g = up.assign(z=up["wrt_recovery_price"].map(lambda x: int(anchor + (x - anchor) // 200 * 200))).groupby("z")["contracts"].sum()
        for z in sorted([k for k in g.index if k >= anchor])[:3]:
            reach = (z + 199) - close
            n = float(g[z])
            up_zones.append({"lo": int(z), "hi": int(z + 199), "contracts": round(n, 1),
                             "reach": round(reach, 1), "counted": bool(reach < day_range)})
            if reach < day_range:
                up_sum += n
    if len(dn):
        g = dn.assign(z=dn["wrt_recovery_price"].map(lambda x: int(anchor + (x - anchor) // 200 * 200))).groupby("z")["contracts"].sum()
        for z in sorted([k for k in g.index if k < anchor], reverse=True)[:3]:
            reach = close - z
            n = float(g[z])
            dn_zones.append({"lo": int(z), "hi": int(z + 199), "contracts": round(n, 1),
                             "reach": round(reach, 1), "counted": bool(reach < day_range)})
            if reach < day_range:
                dn_sum += n
    diff = up_sum - dn_sum
    label = "上屠熊" if diff > 500 else ("下殺牛" if diff < -500 else "窄幅波動")
    return {"dayRange": round(day_range, 1), "upSum": round(up_sum, 1), "downSum": round(dn_sum, 1),
            "diff": round(diff, 1), "upZones": up_zones, "downZones": dn_zones, "label": label}


def _adr_proxy_change(ctx, prediction_date, close):
    """恒指 ADR 代理：恒指成分美股 ADR 按恒指權重加權嘅隔夜變化 × HSI close。
    用日 K 收市計（美股快照 update_time 個 field 係壞嘅，唔可靠）；日 K 攞唔到先後備快照。
    覆蓋權重 < 15% 就當無數據。"""
    from datetime import date as _date, timedelta as _td
    end_d = _date.fromisoformat(prediction_date)
    rets = {}
    missing = []
    for code in ADR_BASKET:
        r = None
        try:
            ret, df, _ = ctx.request_history_kline(
                code, start=str(end_d - _td(days=10)), end=prediction_date,
                ktype="K_DAY", max_count=10,
            )
            if ret == 0 and df is not None and len(df) >= 2:
                df = df.tail(2).reset_index(drop=True)
                d_last = str(df.iloc[-1]["time_key"])[:10]
                # 最後一根一定要係「尋晚嗰節」美股交易日（prediction_date 減 1～4 日）
                if (end_d - _td(days=4)).isoformat() <= d_last < prediction_date:
                    r = float(df.iloc[-1]["close"]) / float(df.iloc[-2]["close"]) - 1.0
        except Exception:
            r = None
        if r is None:
            missing.append(code)
        else:
            rets[code] = r
    if missing:
        try:
            ret2, sn = ctx.get_market_snapshot(missing)
            if ret2 == 0 and len(sn):
                for _, row in sn.iterrows():
                    lp, pc = float(row["last_price"]), float(row["prev_close_price"])
                    if lp > 0 and pc > 0:
                        rets[row["code"]] = lp / pc - 1.0
        except Exception:
            pass
    num = sum(ADR_BASKET[c] * r for c, r in rets.items())
    den = sum(ADR_BASKET[c] for c in rets)
    if den < 15.0:
        log(f"ADR 代理覆蓋不足（權重 {den:.1f}% < 15%）→ 當無數據")
        return None
    detail = " ".join(
        f"{c.split('.')[-1]}{r*100:+.2f}%" for c, r in sorted(rets.items(), key=lambda kv: -ADR_BASKET[kv[0]])
    )
    chg = close * (num / den)
    log(f"ADR 代理: 加權 {num/den*100:+.3f}% → {chg:+.1f} 點（{detail}）")
    return round(chg, 1)


def previous_night_premarket(ctx, trading_date, prediction_date, close):
    """前一晚嘅「先」方向：夜期（HK.HSImain 夜市收 vs 日市收）+ ADR 代理籃（OpenD 美股）。
    trading_date=結算數據日、prediction_date=訊號適用日。夜期 = 結算日晚 17:00 → 適用日凌晨 03:05。"""
    from datetime import date as _date
    try:
        # 夜期：HSImain 快照（朝早跑時 last_price = 尋晚 T+1 段收市、prev_close = 昨日日市收）。
        # 唔用歷史 K線 —— futu 期貨 K線嘅 T+1 段延遲入庫，朝早跑時根本未有尋晚 bars。
        night_change = None
        r_n, ns = ctx.get_market_snapshot([NIGHT_FUT])
        if r_n == 0 and len(ns):
            row = ns.iloc[0]
            last = float(row["last_price"])
            prev_c = float(row["prev_close_price"])
            upd = str(row["update_time"])  # 夜期最後成交時間
            # 合法時間窗：結算日 17:00 → 翌日 03:05（週五晚嗰節收喺週六 03:00，
            # 所以基準係 trading_date 唔係 prediction_date；開市後 live 價會被呢個窗擋走）
            try:
                upd_dt = datetime.strptime(upd[:16], "%Y-%m-%d %H:%M")
            except ValueError:
                upd_dt = None
            win_lo = datetime.strptime(f"{trading_date} 17:00", "%Y-%m-%d %H:%M")
            win_hi = win_lo + timedelta(hours=10, minutes=5)
            if last > 0 and prev_c > 0 and upd_dt is not None and win_lo <= upd_dt <= win_hi:
                night_change = round(last - prev_c, 1)
                log(f"夜期原始值: 日市收={prev_c:.0f} / 夜期收({upd})={last:.0f} → 變化 {night_change:+.1f}")
            else:
                log(f"夜期快照時間唔啱（update_time={upd}，預期 {trading_date} 17:00–翌日 03:05）或價為 0，當無訊號處理")

        # ADR 主源：富途 HSIADR 指數（「HSI ADR CK」sheet 同源；頁面個變化 = HSIADR − HSI 昨收）。
        # OpenD 唔支援美股指數報價 → 抓唔到先後備恒指權重加權 ADR 籃（OpenD 美股）。
        adr_change = None
        adr_src = "ADR 代理籃（OpenD）"
        try:
            hsiadr = fetch_hsiadr()
        except Exception as e:
            log(f"⚠️ HSIADR 抓取異常: {e}")
            hsiadr = None
        if hsiadr is not None:
            adr_change = hsiadr[1]
            adr_src = "HSIADR 指數（futunn）"
        else:
            try:
                adr_change = _adr_proxy_change(ctx, prediction_date, close)
            except Exception as e:
                log(f"⚠️ ADR 代理計唔到: {e}")

        # 方向判定（「HSI ADR CK」sheet 規則 2026-08-21）：±50 點，AND 條件 —
        # 先跌 = ADR < −50 且 夜期 < −50；先升 = ADR > 50 且 夜期 > 50；
        # 任何一邊 |變化| ≤ 50 → 先窄幅波動（OR 條件）；兩邊過 50 但相反 → 窄幅（sheet 冇定義，保守處理）。
        # 得一邊有數據時照計嗰邊（confirmed=False 標明數據唔齊）。
        direction = confirmed = None
        adjustment = 0.0
        t = SIGNAL_THRESHOLD
        if night_change is not None and adr_change is not None:
            if night_change > t and adr_change > t:
                direction, confirmed, adjustment = "up", True, night_change
            elif night_change < -t and adr_change < -t:
                direction, confirmed, adjustment = "down", True, night_change
        elif night_change is not None:
            if abs(night_change) > t:
                direction, adjustment = ("up" if night_change > 0 else "down"), night_change
        elif adr_change is not None:
            if abs(adr_change) > t:
                direction, adjustment = ("up" if adr_change > 0 else "down"), adr_change
        src = f"夜期（OpenD）+ {adr_src}"
        return {
            "adrChange": None if adr_change is None else round(adr_change, 1),
            "adrSource": adr_src,
            "nightFuturesChange": None if night_change is None else round(night_change, 1),
            "adjustment": round(adjustment, 1),
            "initialDirection": direction,
            "isConfirmed": bool(confirmed),
            "adjustedReference": round(close + adjustment, 2),
            "sourceLabel": src + ("，同向確認" if confirmed else ""),
            "status": (f"先{'升' if direction == 'up' else '跌'}，等待目標" if direction else "先窄幅波動"),
        }
    except Exception as e:
        log(f"⚠️ 前一晚 premarket 失敗（唔影響主體）: {e}")
        return None


def build_day(ctx, trading_date, prediction_date=None):
    mmdd = trading_date[5:].replace("-", "-")
    # ── HSI + VHSI 快照 ──
    # close 用歷史日 K 攞 trading_date 嘅正式收市（2026-08-21 教訓：朝早 08:00 snapshot 嘅
    # prev_close 可能仲未更新，會落後一日）；snapshot prev_close 只做後備。
    idx = snapshot(ctx, [HSI, VHSI])
    hsi = idx[idx["code"] == HSI].iloc[0]
    vhsi_row = idx[idx["code"] == VHSI].iloc[0]
    close = None
    k_open = k_high = k_low = None
    try:
        r_k, kdf, _ = ctx.request_history_kline(HSI, start=trading_date, end=trading_date, ktype="K_DAY", max_count=2)
        if r_k == 0 and len(kdf):
            krow = kdf.iloc[-1]
            close = float(krow["close"])
            k_open, k_high, k_low = float(krow["open"]), float(krow["high"]), float(krow["low"])
    except Exception as e:
        log(f"⚠️ HSI 日 K 攞唔到（{e}），用 snapshot prev_close 後備")
    if close is None or close <= 0:
        close = float(hsi["prev_close_price"])
    # open/high/low 一樣優先用日 K：開市前快照可能未更新，開市後會歸零（09:16 重跑實測全變 0）
    open_p = k_open if k_open and k_open > 0 else float(hsi["open_price"])
    high = k_high if k_high and k_high > 0 else float(hsi["high_price"])
    low = k_low if k_low and k_low > 0 else float(hsi["low_price"])
    vhsi = float(vhsi_row["prev_close_price"]) / 100.0  # 存小數（與舊格式一致）
    em1d = close * vhsi / (252 ** 0.5)
    upper_day = round(close + em1d)
    lower_day = round(close - em1d)
    log(f"HSI 收 {close:,.2f}（{mmdd}，prev_close） VHSI {vhsi*100:.2f} EM±{em1d:.1f} → {lower_day}-{upper_day}")

    # ── 牛熊證快照 ──
    try:
        codes, src_file = cbbc_codes_opend(ctx)
    except Exception as e:
        log(f"⚠️ OpenD 攞名單失敗（{e}）→ 用本地 scrape 後備")
        codes, src_file = cbbc_codes_local()
    snap = snapshot(ctx, codes)
    snap = snap[snap["wrt_recovery_price"] > 0].copy()
    log(f"快照返回 {len(snap)} 隻（名單 {len(codes)}）")

    # 相對期指張數（GS 口徑，2026-08-21 對 gswarrants 官方表驗證 19/19 全中）：
    # 街貨量 ÷（換股比率 × 50）；「後」方向判定（HSI ADR CK sheet 邏輯）用呢個而唔係百萬份街貨。
    ratio = snap["wrt_conversion_ratio"].where(snap["wrt_conversion_ratio"] > 0)
    snap["contracts"] = (snap["wrt_street_vol"] / (ratio * 50.0)).fillna(0.0)

    # ── 區間聚合 ──
    snap["zone_lo"] = (snap["wrt_recovery_price"] // ZONE_SIZE * ZONE_SIZE).astype(int)
    agg = snap.groupby("zone_lo").agg(out=("wrt_street_vol", "sum"), n=("code", "count"), contracts=("contracts", "sum"))
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
            "contracts": round(float(agg.loc[lo, "contracts"]), 1),
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

    # ── 判定（「HSI ADR CK」sheet 邏輯 2026-08-21）──
    # 「後」：DayRange =（昨日 High−Low）/2（日 K 攞唔到就後備 EM）；上下各 3 個最近 200 點格，
    # 可達先計入；相對期指張數 upSum − downSum ±500 定屠熊／殺牛／窄幅。
    premarket = previous_night_premarket(ctx, trading_date, prediction_date or trading_date, close)
    day_range = (high - low) / 2 if (high and low and high > low) else em1d
    sheet_back = _sheet_back_direction(snap, close, day_range)
    back = sheet_back["label"]
    log(f"sheet 後向: up {sheet_back['upSum']:.0f} vs down {sheet_back['downSum']:.0f} "
        f"(diff {sheet_back['diff']:+.0f}, DayRange {sheet_back['dayRange']:.0f}) → {back}")
    if premarket:
        d0 = premarket.get("initialDirection")
        if d0:
            verdict = f"先{'升' if d0 == 'up' else '跌'}後{back}"
        else:
            verdict = "窄幅波動" if back == "窄幅波動" else f"先窄幅波動後{back}"
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
        "sheetBack": sheet_back,
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
        "sourceNote": "每個交易日 08:00（HKT）用昨日結算牛熊數據判當日；「先」向按前晚夜期（OpenD）+ HSIADR 指數（futunn，後備=恒指權重加權 ADR 籃）±50 AND 規則；「後」向用相對期指張數上下各 3 區 ±500 規則（HSI ADR CK sheet 邏輯，OpenD 計）；08:45 重算 premarket 再推一次。",
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


def push_only():
    """唔重建，淨係將現有 dataset_latest.json 重新 POST 上 gsmart-box。
    用途：Manus 08:40 會用舊數據覆蓋我哋 08:00 推嘅版本，08:45 重推一次確保我哋嘅先係最終。"""
    if not DATASET_PATH.exists():
        log(f"REPUSH_FAIL: 搵唔到 {DATASET_PATH}")
        return 1
    ds = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    day0 = ds["days"][0]
    log(f"重推 dataset（generatedAt={ds.get('generatedAt')} date={day0['date']} predictionDate={day0.get('predictionDate')}）")
    return 0 if push(ds) else 1


def _pm_quality(pm):
    if not pm:
        return -1
    return (1 if pm.get("nightFuturesChange") is not None else 0) + (1 if pm.get("adrChange") is not None else 0)


def refresh_premarket():
    """08:45 用：夜期/ADR 數據朝早可能延遲入庫（futu 美股日 K、期貨快照都有呢個風險），
    重計一次 premarket，覆蓋變好先更新 dataset 同 verdict 再推；否則照舊重推。"""
    from futu import OpenQuoteContext
    if not DATASET_PATH.exists():
        log(f"REFRESH_FAIL: 搵唔到 {DATASET_PATH}")
        return 1
    ds = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    day0 = ds["days"][0]
    pred = day0.get("predictionDate") or day0["date"]
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        pm = previous_night_premarket(ctx, day0["date"], pred, day0["close"])
    finally:
        ctx.close()
    if pm and _pm_quality(pm) > _pm_quality(day0.get("premarket")):
        day0["premarket"] = pm
        sb = day0.get("sheetBack") or {}
        back = sb.get("label") or ("上屠熊" if day0.get("weightedScore", 0) >= 0.25
                                   else ("下殺牛" if day0.get("weightedScore", 0) <= -0.25 else "窄幅波動"))
        d0 = pm.get("initialDirection")
        day0["verdict"] = (
            f"先{'升' if d0 == 'up' else '跌'}後{back}" if d0
            else ("窄幅波動" if back == "窄幅波動" else f"先窄幅波動後{back}")
        )
        (DAYS_DIR / f"{day0['date']}.json").write_text(json.dumps(day0, ensure_ascii=False))
        ds["refreshedAt"] = datetime.now(HKT).isoformat(timespec="seconds")
        DATASET_PATH.write_text(json.dumps(ds, ensure_ascii=False))
        log(f"REFRESH_UPDATED: verdict={day0['verdict']} 夜期={pm.get('nightFuturesChange')} ADR={pm.get('adrChange')}")
    else:
        log("REFRESH_SAME: premarket 無變好，照舊重推")
    return 0 if push(ds) else 1


def main():
    if "--push-only" in sys.argv:
        return push_only()
    if "--refresh-premarket" in sys.argv:
        return refresh_premarket()
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
