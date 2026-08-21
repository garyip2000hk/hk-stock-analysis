#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""
HSI Short Strangle 全/半自動服務（Futu OpenD 版）

用 gsmart-box「Short Strangle 戰情室」邏輯（VHSI 分級 + 牛熊證風控），
透過 OpenD 取恒指期權即市數據，提供：
  GET  /health
  GET  /signal            今日訊號（mode / K / L / 合約 / BS勝率 / 回測勝率）
  GET  /positions         持倉 + 實時 P&L + 平倉建議
  GET  /track             預測 vs 實際勝率（回測 + 前向 paper 記錄）
  GET  /config
  POST /config            {auto_mode: full|semi, enabled}
  POST /order             {mode, lots, confirm, dry_run}  開倉（PAPER 記錄 或 REAL 經 OpenD）
  POST /close             {position_id, qty, confirm}     平倉

狀態：hsi_state.json（config + positions）；實際戰績：hsi_track.json（paper 前向）。
唔好喺度放任何密碼；REAL 解鎖靠環境變數 FUTU_TRADE_PWD。
"""

import hmac
import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import math
import pandas as pd

import futu
from futu import OpenSecTradeContext, SecurityFirm, TrdEnv, TrdSide, OrderType, RET_OK
from futu import IndexOptionType

import hsi_strangle as hs
import bs as bsx

OPEND_HOST = os.environ.get("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.environ.get("OPEND_PORT", "11111"))
LISTEN_PORT = int(os.environ.get("PORT", "8891"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "hsi_state.json")
TRACK_PATH = os.path.join(BASE_DIR, "hsi_track.json")
BACKTEST_PATH = os.path.join(BASE_DIR, "hsi_bs_backtest.json")
METHOD_COMPARE_PATH = os.path.join(BASE_DIR, "hsi_method_compare.json")
KLINE_INDEX = "/home/workspace/Desktop/db/Futu/Kline/kline_index.parquet"
WEEKLY_MULT = 1.15   # Excel 方法對決回測最佳（96.3% 勝率）
MHI_MULT = 10.0      # 迷你恒指期權每點 HK$10

MULT = 50.0          # 恒指期權每點 HK$50
LOT = MULT           # 1 張 = $50/點
HK_PER_PT = 50.0

# ---------------------------------------------------------------------------
# 狀態
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()


def _now_iso():
    return hs.now_hkt().isoformat()


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_state():
    with _state_lock:
        return _load(STATE_PATH, {"config": {"auto_mode": "semi", "enabled": True, "lots": 1}, "positions": []})


def save_state(state):
    with _state_lock:
        _save(STATE_PATH, state)


def load_track():
    return _load(TRACK_PATH, {"predictions": [], "resolved": [], "actual": []})


def save_track(t):
    _save(TRACK_PATH, t)


# ---------------------------------------------------------------------------
# OpenD quote ctx（每 call 一條，快 + 安全；quote 唔使解鎖）
# ---------------------------------------------------------------------------
def quote_ctx():
    return hs.connect()


_trade_ctx = None
_trade_lock = threading.Lock()
_unlocked = {"REAL": False}


def trade_ctx():
    global _trade_ctx
    with _trade_lock:
        if _trade_ctx is None:
            _trade_ctx = OpenSecTradeContext(host=OPEND_HOST, port=OPEND_PORT, security_firm=SecurityFirm.FUTUSECURITIES)
        return _trade_ctx


def reset_trade_ctx():
    global _trade_ctx
    with _trade_lock:
        try:
            if _trade_ctx:
                _trade_ctx.close()
        except Exception:
            pass
        _trade_ctx = None
        _unlocked["REAL"] = False


def ensure_unlocked():
    if _unlocked["REAL"]:
        return
    pwd = os.environ.get("FUTU_TRADE_PWD")
    if not pwd:
        raise RuntimeError("FUTU_TRADE_PWD 未設定（去 Zo Secrets 加返先可以真落單）")
    ret, data = trade_ctx().unlock_trade(pwd)
    if ret != RET_OK:
        raise RuntimeError(f"解鎖交易失敗: {data}")
    _unlocked["REAL"] = True


def verify_trade_pw(pw):
    """面板實盤閘門：用戶入嘅密碼要同 FUTU_TRADE_PWD 一致（constant-time）。"""
    ref = os.environ.get("FUTU_TRADE_PWD") or ""
    if not ref:
        return False, "FUTU_TRADE_PWD 未設定（去 Zo Secrets 加返）"
    if not pw or not hmac.compare_digest(str(pw).encode(), ref.encode()):
        return False, "交易密碼錯誤"
    return True, ""


def real_account():
    ret, df = trade_ctx().get_acc_list()
    if ret != RET_OK:
        raise RuntimeError(f"攞唔到戶口: {df}")
    # 賣期權要保證金戶口（naked short 唔可以用 CASH 現金戶口），優先 MARGIN
    for _, r in df.iterrows():
        if (str(r.get("trd_env", "")).upper() == "REAL"
                and str(r.get("acc_type", "")).upper() == "MARGIN"
                and str(r.get("acc_status", "")).upper() == "ACTIVE"):
            return str(r["acc_id"])
    for _, r in df.iterrows():
        if str(r.get("trd_env", "")).upper() == "REAL" and str(r.get("acc_status", "")).upper() == "ACTIVE":
            return str(r["acc_id"])
    raise RuntimeError("冇 ACTIVE 嘅 REAL 戶口")


# ---------------------------------------------------------------------------
# 訊號計算（重用 hsi_strangle）
# ---------------------------------------------------------------------------
def _bs_winprob(spot, vhsi, dte, K, L, mode):
    sigma = vhsi / 100.0
    t = max(dte, 1) / 365.0
    if mode == "SKIP":
        return None
    if mode == "Full":
        pl = bsx.prob_touch(spot, K, t, sigma) or 0.0   # 觸上沿
        ps = bsx.prob_touch(spot, L, t, sigma) or 0.0   # 觸下沿
        return round(max(0.0, 1.0 - pl - ps) * 100, 1)
    # PutOnly：贏 = 期內唔觸下沿 L
    return round(max(0.0, 1.0 - (bsx.prob_touch(spot, L, t, sigma) or 0.0)) * 100, 1)


def _backtest_winrate(mode):
    try:
        bt = _load(BACKTEST_PATH, {})
        d = bt.get("results", {}).get("dte5", {}).get("gated", {}).get("by_mode", {})
        m = d.get(mode)
        if m:
            return m.get("win_rate_pct"), m.get("avg_pnl_hkd")
    except Exception:
        pass
    return None, None


def compute_signal(force=False):
    ctx = quote_ctx()
    try:
        vhsi, hsi = hs.get_index_snapshot(ctx)
        mode = hs.detect_mode(vhsi)
        calc_mode = "Full" if (force and mode == "SKIP") else mode
        friday = hs.is_friday()
        dte = 5
        cfg = hs.MODE_CONFIGS.get(calc_mode, {})
        if cfg.get("dte"):
            dte = cfg["dte"]
        strikes = hs.calc_strikes(hsi, vhsi, calc_mode)
        K, L = strikes.get("K"), strikes.get("L")
        expiry = None
        contracts = {}
        atm_iv_val = None
        if (mode != "SKIP" or force) and (K or L):
            exp = hs.pick_expiry(ctx)
            expiry = exp.strftime("%Y-%m-%d") if hasattr(exp, "strftime") else str(exp)[:10]
            chain = hs.pull_chain(ctx, expiry)
            if chain is not None:
                atm_iv_val = hs.atm_iv(chain, hsi)
                if K:
                    contracts["call"] = hs.find_contract(chain, "CALL", K)
                if L:
                    contracts["put"] = hs.find_contract(chain, "PUT", L)
        bs_win = _bs_winprob(hsi, vhsi, dte, K, L, calc_mode) if (K or L) else None
        bt_win, bt_ev = _backtest_winrate(calc_mode)
        return {
            "time": _now_iso(), "vhsi": round(vhsi, 2), "hsi": round(hsi, 2),
            "mode": mode, "mode_label": hs.MODE_CONFIGS.get(mode, {}).get("label", mode),
            "friday": friday, "dte": dte, "expiry": expiry,
            "strikes": {"em": round(strikes.get("em", 0), 1), "K": K, "L": L},
            "atm_iv": atm_iv_val, "contracts": contracts,
            "bs_winprob": bs_win, "backtest_winrate": bt_win, "backtest_ev": bt_ev,
        }
    finally:
        try:
            ctx.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 週版（Excel 週帶）+ MHI 月期權
# ---------------------------------------------------------------------------
def _kline_index():
    try:
        return pd.read_parquet(KLINE_INDEX)
    except Exception:
        return None


def week_band(today=None):
    """Excel 方法：逢本週首個交易日定 band，hold 全週。
    band = base_close ± WEEKLY_MULT × base_close × base_VHSI/100 × √(1/52)
    """
    df = _kline_index()
    if df is None:
        return None
    today = today or hs.now_hkt().date()
    df = df.copy()
    df["d"] = pd.to_datetime(df["time_key"]).dt.date
    hsi_s = df[df["code"] == hs.HSI].set_index("d")["close"]
    vhsi_s = df[df["code"] == hs.VHSI].set_index("d")["close"]
    dates = sorted(set(hsi_s.index) & set(vhsi_s.index))
    dates = [d for d in dates if d <= today]
    if not dates:
        return None
    week_start = today - timedelta(days=today.weekday())
    this_week = [d for d in dates if d >= week_start]
    base_date = this_week[0] if this_week else dates[-1]
    c, v = float(hsi_s[base_date]), float(vhsi_s[base_date])
    em = c * v / 100.0 * math.sqrt(1.0 / 52.0) * WEEKLY_MULT
    upper = hs.round_up(c + em)
    lower = hs.round_down(c - em)
    return {"base_date": base_date.isoformat(), "base_close": round(c, 2),
            "base_vhsi": round(v, 2), "em": round(em, 1),
            "upper": upper, "lower": lower, "mult": WEEKLY_MULT}


def preview_next_week():
    """星期五晚預估下週帶（用最近收市價 + VHSI 做 proxy；週一先正式定帶）。"""
    ctx = quote_ctx()
    try:
        vhsi, hsi = hs.get_index_snapshot(ctx)
        if not hsi or not vhsi:
            return None
        em = hsi * vhsi / 100.0 * math.sqrt(1.0 / 52.0) * WEEKLY_MULT
        upper = hs.round_up(hsi + em)
        lower = hs.round_down(hsi - em)
        out = {"hsi": round(hsi, 2), "vhsi": round(vhsi, 2), "em": round(em, 1),
               "upper": upper, "lower": lower, "mult": WEEKLY_MULT}
        # 下週五到期嘅週期權
        try:
            exp = pick_weekly_expiry(ctx)
            if exp:
                out["expiry"] = exp.strftime("%Y-%m-%d")
        except Exception:
            pass
        # 帶兩條腿嘅合約（下週帶為基礎）
        try:
            expiry = out.get("expiry")
            if expiry:
                chain = hs.pull_chain(ctx, expiry)
                out["contracts"] = _chain_contracts(chain, upper, lower)
        except Exception:
            pass
        out["backtest"] = {"win_rate": 96.3, "avg_pnl": 3210, "worst": -12270}
        return out
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def pick_weekly_expiry(ctx):
    """最近嘅 WEEK 週期權到期日（一般係今個星期五）。"""
    ret, df = ctx.get_option_expiration_date(hs.HSI)
    if ret != futu.RET_OK or df is None or len(df) == 0:
        return None
    today = pd.Timestamp(hs.now_hkt().date())
    df = df.copy()
    df["dt"] = pd.to_datetime(df["strike_time"])
    if "expiration_cycle" in df.columns:
        wk = df[(df["expiration_cycle"] == "WEEK") & (df["dt"] >= today)]
        if len(wk):
            return wk.sort_values("dt").iloc[0]["dt"]
    cands = df[df["dt"] >= today]
    if len(cands):
        return cands.sort_values("dt").iloc[0]["dt"]
    return None


def pick_mhi_expiry(ctx, min_dte=20, max_dte=60):
    """MHI（迷你恒指）月期權到期月：揀 20-60 日內最接近 30 日嗰個。"""
    ret, df = ctx.get_option_expiration_date(hs.HSI, index_option_type=IndexOptionType.SMALL)
    if ret != futu.RET_OK or df is None or len(df) == 0:
        return None
    today = pd.Timestamp(hs.now_hkt().date())
    expiries = sorted(pd.to_datetime(df["strike_time"]).tolist())
    cands = []
    for e in expiries:
        dte = (e - today).days
        if min_dte <= dte <= max_dte:
            cands.append((abs(dte - 30), e))
    if cands:
        return min(cands)[1]
    fut = [e for e in expiries if e > today]
    return fut[0] if fut else None


def pull_mhi_chain(ctx, expiry):
    """MHI 月期權鏈（IndexOptionType.SMALL），同 pull_chain 結構。"""
    ymd = expiry.strftime("%Y-%m-%d") if hasattr(expiry, "strftime") else str(expiry)[:10]
    ret, chain = ctx.get_option_chain(
        code=hs.HSI, start=ymd, end=ymd,
        option_type=futu.OptionType.ALL, option_cond_type=futu.OptionCondType.ALL,
        index_option_type=IndexOptionType.SMALL,
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
              "option_delta", "option_gamma", "option_theta", "option_vega", "option_rho"]:
        if c in snap.columns:
            cols.append(c)
    snap = snap[cols]
    return chain.merge(snap, on="code", how="left")


def _weekly_backtest():
    try:
        d = json.load(open(METHOD_COMPARE_PATH))
        r = d.get("results", {})
        out = {}
        for key, label in (("B_excel_weekly_1.15", "ungated"), ("B_excel_weekly_1.15_gated", "gated")):
            if key in r:
                x = r[key]
                out[label] = {"n": x.get("n"), "win_rate_pct": x.get("win_rate_pct"),
                              "avg_pnl_hkd": x.get("avg_pnl_hkd"), "worst_hkd": x.get("worst_hkd")}
        return out
    except Exception:
        return {}


def _chain_contracts(chain, K, L):
    out = {}
    if chain is None:
        return out
    if K:
        out["call"] = hs.find_contract(chain, "CALL", K)
    if L:
        out["put"] = hs.find_contract(chain, "PUT", L)
    return out


def compute_weekly_signal():
    """週版（Excel 週帶）訊號：band + 週期權合約 + BS/回測勝率。"""
    ctx = quote_ctx()
    try:
        vhsi, hsi = hs.get_index_snapshot(ctx)
        band = week_band()
        mode = hs.detect_mode(vhsi)
        out = {"time": _now_iso(),
               "vhsi": round(vhsi, 2) if vhsi else None,
               "hsi": round(hsi, 2) if hsi else None,
               "mode": mode,
               "mode_label": hs.MODE_CONFIGS.get(mode, {}).get("label", mode),
               "weekday": hs.now_hkt().weekday(),  # 0=Mon ... 4=Fri
               "band": band, "backtest": _weekly_backtest()}
        if band:
            exp = pick_weekly_expiry(ctx)
            if exp is not None:
                expiry_iso = exp.strftime("%Y-%m-%d")
                out["expiry"] = expiry_iso
                dte = max(0, (pd.Timestamp(expiry_iso).date() - hs.now_hkt().date()).days)
                out["dte"] = dte
                try:
                    chain = hs.pull_chain(ctx, expiry_iso)
                    out["contracts"] = _chain_contracts(chain, band["upper"], band["lower"])
                except Exception:
                    out["contracts"] = {}
                out["bs_winprob"] = _bs_winprob(hsi, vhsi, max(dte, 1), band["upper"], band["lower"], "Full")
        return out
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def compute_full_signal(force=False):
    """日+週一次攞晒。"""
    daily = compute_signal(force=force)
    weekly = compute_weekly_signal()
    # 日版 MHI 月期權鏈（備用）
    if daily["mode"] != "SKIP" or force:
        mhi = None
        try:
            ctx2 = quote_ctx()
            mexp = pick_mhi_expiry(ctx2)
            if mexp is not None and daily["strikes"]["K"]:
                cm = pull_mhi_chain(ctx2, mexp)
                mhi = {"expiry": mexp.strftime("%Y-%m-%d"),
                       **_chain_contracts(cm, daily["strikes"]["K"], daily["strikes"]["L"])}
        finally:
            try:
                ctx2.close()
            except Exception:
                pass
        if mhi:
            daily["mhi_opt"] = mhi
    return {"time": _now_iso(), "vhsi": daily.get("vhsi"), "hsi": daily.get("hsi"),
            "daily": daily, "weekly": weekly}


# ---------------------------------------------------------------------------
# 期權代碼 + 落單
# ---------------------------------------------------------------------------
def option_code(expiry_iso, cp, strike, instrument="HSI"):
    yymmdd = expiry_iso.replace("-", "")[2:]
    pref = "MHI" if instrument == "MHI" else "HSI"
    return f"HK.{pref}{yymmdd}{cp.upper()}{int(round(float(strike) * 1000))}"


def place_real_order(code, qty, side, price):
    ensure_unlocked()
    acc = real_account()
    ret, data = trade_ctx().place_order(
        price=price, qty=qty, code=code,
        trd_side=TrdSide.SELL if side == "SELL" else TrdSide.BUY,
        order_type=OrderType.NORMAL, trd_env=TrdEnv.REAL,
        acc_id=acc, remark="hsi-strangle",
    )
    if ret != RET_OK:
        return {"success": False, "error": str(data)}
    oid = None
    try:
        oid = str(data["orderid"].iloc[0])
    except Exception:
        oid = str(data)
    return {"success": True, "order_id": oid}


def mid_or_last(c):
    if not c:
        return None
    bid, ask, last = c.get("bid"), c.get("ask"), c.get("last")
    if bid and ask and bid > 0 and ask > 0:
        return round((bid + ask) / 2.0, 1)
    return last


def open_position(sig, lots, real, version="daily", instrument="HSI"):
    legs = []
    orders = []
    mode = sig["mode"]
    K, L = sig["strikes"].get("K"), sig["strikes"].get("L")
    expiry = sig["expiry"]
    mult = MHI_MULT if instrument == "MHI" else HK_PER_PT
    call_c = sig["contracts"].get("call")
    put_c = sig["contracts"].get("put")
    call_code = (call_c or {}).get("code") or option_code(expiry, "C", K, instrument)
    put_code = (put_c or {}).get("code") or option_code(expiry, "P", L, instrument)
    if mode == "Full" and K:
        legs.append({"cp": "CALL", "strike": K, "code": call_code,
                     "px": mid_or_last(call_c),
                     "bid": (call_c or {}).get("bid"), "ask": (call_c or {}).get("ask")})
    if L:
        legs.append({"cp": "PUT", "strike": L, "code": put_code,
                     "px": mid_or_last(put_c),
                     "bid": (put_c or {}).get("bid"), "ask": (put_c or {}).get("ask")})
    if real:
        for leg in legs:
            # 賣出限價用 bid（確保即時成交，唔好用 mid 掛單乾等）
            sell_px = leg.get("bid") or leg.get("px") or 0
            r = place_real_order(leg["code"], lots, "SELL", sell_px)
            orders.append({"code": leg["code"], **r})
            if not r["success"]:
                return {"success": False, "error": f"{leg['code']} 落單失敗: {r['error']}", "orders": orders}
            time.sleep(2.2)
    pos = {
        "id": f"P{int(time.time())}", "mode": mode, "opened_at": _now_iso(),
        "expiry": expiry, "lots": lots, "real": bool(real),
        "version": version, "instrument": instrument, "mult": mult,
        "hsi_at_open": sig["hsi"], "vhsi_at_open": sig["vhsi"],
        "legs": legs, "orders": orders, "status": "OPEN",
        "premium_pts": round(sum((l["px"] or 0) for l in legs), 1),
    }
    return {"success": True, "position": pos}


# ---------------------------------------------------------------------------
# 平倉建議 + P&L
# ---------------------------------------------------------------------------
def suggest_close(pos, pnl_pts, hsi_now, days_left):
    """回傳 {"key": 面板SUGGEST鍵, "reason": 原因}。"""
    prem = pos.get("premium_pts", 0)
    if days_left is not None and days_left <= 1:
        return {"key": "平倉-臨近到期", "reason": f"剩 {max(days_left,0)} 日到期，避免到期風險"}
    call_leg = next((l for l in pos["legs"] if l["cp"] == "CALL"), None)
    put_leg = next((l for l in pos["legs"] if l["cp"] == "PUT"), None)
    if call_leg and hsi_now >= call_leg["strike"]:
        return {"key": "平倉-止蝕", "reason": f"恒指 {hsi_now:.0f} 升穿上沿 {call_leg['strike']:.0f}"}
    if put_leg and hsi_now <= put_leg["strike"]:
        return {"key": "平倉-止蝕", "reason": f"恒指 {hsi_now:.0f} 跌破下沿 {put_leg['strike']:.0f}"}
    if prem > 0 and pnl_pts >= prem * 0.5:
        return {"key": "平倉-止賺", "reason": f"已賺權金 {pnl_pts:.0f}/{prem:.0f} 點（≥50%），可止賺"}
    return {"key": "持有", "reason": "價位喺區間內，未到期"}


def positions_view():
    st = load_state()
    open_pos = [x for x in st.get("positions", []) if x.get("status") == "OPEN"]
    hsi_now = None
    ctx = None
    chains = {}
    if open_pos:
        try:
            ctx = quote_ctx()
            _, hsi_now = hs.get_index_snapshot(ctx)
        except Exception:
            hsi_now = None
    out = []
    try:
        for pos in open_pos:
            prem = pos.get("premium_pts", 0)
            lots = pos.get("lots", 1)
            expiry = pos.get("expiry")
            mult = pos.get("mult") or (MHI_MULT if pos.get("instrument") == "MHI" else HK_PER_PT)
            days_left = None
            try:
                days_left = (datetime.strptime(expiry, "%Y-%m-%d") - hs.now_hkt().replace(tzinfo=None)).days
            except Exception:
                pass
            # 真實 P&L：逐腿用實時期權鏈買回價（short 要買返，用 ask）
            pnl_pts = None
            if ctx is not None and expiry:
                try:
                    ckey = (expiry, pos.get("instrument") or "HSI")
                    if ckey not in chains:
                        if pos.get("instrument") == "MHI":
                            chains[ckey] = pull_mhi_chain(ctx, expiry)
                        else:
                            chains[ckey] = hs.pull_chain(ctx, expiry)
                    chain = chains[ckey]
                    if chain is not None:
                        buyback = 0.0
                        ok = True
                        for leg in pos["legs"]:
                            c = hs.find_contract(chain, leg["cp"], leg["strike"])
                            if not c:
                                ok = False
                                break
                            buyback += (c.get("ask") or c.get("bid") or 0)
                        if ok:
                            pnl_pts = round(prem - buyback, 1)
                except Exception:
                    pnl_pts = None
            # fallback：冇鏈就用內在值
            if pnl_pts is None and hsi_now is not None:
                intrinsic = sum(
                    max(0.0, (hsi_now - l["strike"]) if l["cp"] == "CALL" else (l["strike"] - hsi_now))
                    for l in pos["legs"]
                )
                pnl_pts = round(prem - intrinsic, 1)
            pnl_hkd = round(pnl_pts * mult * lots, 0) if pnl_pts is not None else None
            suggest = suggest_close(pos, pnl_pts, hsi_now, days_left) if hsi_now is not None else {"key": "持有", "reason": "未有即市數據"}
            out.append({
                "id": pos["id"],
                "mode": pos["mode"],
                "version": pos.get("version", "daily"),
                "instrument": pos.get("instrument", "HSI"),
                "mult": mult,
                "mode_label": hs.MODE_CONFIGS.get(pos["mode"], {}).get("label", pos["mode"]),
                "real": pos.get("real", False),
                "expiry": expiry,
                "lots": lots,
                "legs": pos["legs"],
                "entry_date": (pos.get("opened_at") or "")[:10],
                "premium_pts": prem,
                "premium_hkd": round(prem * mult * lots, 0),
                "hsi_at_open": pos.get("hsi_at_open"),
                "hsi_now": round(hsi_now, 2) if hsi_now is not None else None,
                "days_left": days_left,
                "live_pnl_hkd": pnl_hkd,
                "pnl_pct": round(pnl_pts / prem * 100, 1) if (pnl_pts is not None and prem > 0) else None,
                "suggest": suggest["key"],
                "suggest_reason": suggest["reason"],
                "status": "OPEN",
            })
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
    return {"positions": out, "hsi_now": round(hsi_now, 2) if hsi_now is not None else None}


def monitor_view():
    """Extension 實時監察 payload：持倉 + 即市勝率 + 止賺/止蝕狀態。"""
    pv = positions_view()
    hsi_now = pv.get("hsi_now")
    vhsi_now = None
    try:
        ctx = quote_ctx()
        try:
            v, h = hs.get_index_snapshot(ctx)
            vhsi_now = round(float(v), 2)
            if hsi_now is None:
                hsi_now = round(float(h), 2)
        finally:
            ctx.close()
    except Exception:
        pass
    status_map = {"平倉-止蝕": "STOP_LOSS", "平倉-止賺": "TAKE_PROFIT", "平倉-臨近到期": "EXPIRY"}
    for p in pv["positions"]:
        call_leg = next((l for l in p["legs"] if l["cp"] == "CALL"), None)
        put_leg = next((l for l in p["legs"] if l["cp"] == "PUT"), None)
        K = call_leg["strike"] if call_leg else None
        L = put_leg["strike"] if put_leg else None
        mode = p.get("mode")
        dte = p.get("days_left")
        p["live_winprob"] = (
            _bs_winprob(hsi_now, vhsi_now, max(int(dte or 0), 1), K, L, mode)
            if (hsi_now and vhsi_now and (K or L)) else None
        )
        entry = None
        try:
            d0 = datetime.strptime((p.get("entry_date") or "")[:10], "%Y-%m-%d")
            d1 = datetime.strptime((p.get("expiry") or "")[:10], "%Y-%m-%d")
            if p.get("hsi_at_open") and p.get("vhsi_at_open"):
                entry = _bs_winprob(p["hsi_at_open"], p["vhsi_at_open"], max((d1 - d0).days, 1), K, L, mode)
        except Exception:
            entry = None
        p["entry_winprob"] = entry
        p["tp_sl_status"] = status_map.get(p.get("suggest"), "HOLD")
    return {
        "time": _now_iso(),
        "hsi_now": hsi_now,
        "vhsi_now": vhsi_now,
        "positions": pv["positions"],
    }


# ---------------------------------------------------------------------------
# 前向 paper 記錄（每日一次，resolve DTE 日後）
# ---------------------------------------------------------------------------
def track_today(sig):
    t = load_track()
    today = hs.now_hkt().strftime("%Y-%m-%d")
    preds = t.setdefault("predictions", [])
    if any(p.get("date") == today for p in preds):
        return
    if sig["mode"] == "SKIP" or not sig["strikes"].get("L"):
        return
    ctr = sig.get("contracts") or {}
    prem = round((ctr.get("call") or {}).get("bid", 0) + (ctr.get("put") or {}).get("bid", 0), 1)
    preds.append({
        "date": today, "mode": sig["mode"], "mode_label": sig.get("mode_label", sig["mode"]),
        "vhsi": sig["vhsi"], "hsi": sig["hsi"],
        "K": sig["strikes"].get("K"), "L": sig["strikes"].get("L"),
        "premium_pts": prem, "bs_winprob": sig.get("bs_winprob"), "dte": sig.get("dte", 5),
    })
    save_track(t)


_TRADING_DAYS_CACHE = {"days": None}


def _hk_trading_days():
    """港股交易日（YYYY-MM-DD 字串，升序）。用 OpenD request_trading_days，cache。"""
    if _TRADING_DAYS_CACHE["days"] is not None:
        return _TRADING_DAYS_CACHE["days"]
    days = []
    try:
        ctx = quote_ctx()
        ret, df = ctx.request_trading_days(futu.Market.HK, start="2025-01-01",
                                           end=hs.now_hkt().strftime("%Y-%m-%d"))
        if ret == RET_OK and df is not None and len(df):
            for t in df.get("time", []):
                days.append(str(t)[:10])
    except Exception:
        pass
    if not days:
        d = datetime(2025, 1, 1)
        end = hs.now_hkt().replace(tzinfo=None)
        while d <= end:
            if d.weekday() < 5:
                days.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
    _TRADING_DAYS_CACHE["days"] = sorted(set(days))
    return _TRADING_DAYS_CACHE["days"]


def _trading_days_elapsed(d0_str, d1_str):
    """d0 之後到 d1 之間嘅交易日數（唔含 d0，含 d1）。"""
    ds = _hk_trading_days()
    if not ds:
        return (datetime.strptime(d1_str, "%Y-%m-%d") - datetime.strptime(d0_str, "%Y-%m-%d")).days
    return sum(1 for d in ds if d0_str < d <= d1_str)


def resolve_track(hsi_now):
    t = load_track()
    resolved = t.setdefault("resolved", [])
    preds = t.get("predictions", [])
    keep = []
    today = hs.now_hkt().strftime("%Y-%m-%d")
    for p in preds:
        elapsed = _trading_days_elapsed(p["date"], today)
        if elapsed < p.get("dte", 5):
            keep.append(p)
            continue
        if p["mode"] in ("Full", "HighVol"):
            win = (p["L"] < hsi_now < p["K"])
            intrinsic = max(0.0, hsi_now - p["K"]) + max(0.0, p["L"] - hsi_now)
        else:
            win = (hsi_now > p["L"])
            intrinsic = max(0.0, p["L"] - hsi_now)
        pnl = round((p.get("premium_pts", 0) - intrinsic) * 50, 0)
        resolved.append({**p, "resolved_hsi": round(hsi_now, 2), "win": bool(win), "pnl": pnl})
    t["predictions"] = keep
    save_track(t)
    return len(resolved)


def _strangle_intrinsic(pos, hsi_now):
    """到期內在值（點）。Full = call + put；PutOnly = put。"""
    intrinsic = 0.0
    for leg in pos.get("legs", []):
        if leg["cp"] == "CALL":
            intrinsic += max(0.0, hsi_now - leg["strike"])
        elif leg["cp"] == "PUT":
            intrinsic += max(0.0, leg["strike"] - hsi_now)
    return intrinsic


def settle_position(pos, hsi_now, reason):
    """結算（平倉）持倉：計最終 P&L、mark CLOSED、記低實際戰績。"""
    prem = pos.get("premium_pts", 0)
    intrinsic = _strangle_intrinsic(pos, hsi_now)
    pnl_pts = round(prem - intrinsic, 1)
    mult = pos.get("mult") or (MHI_MULT if pos.get("instrument") == "MHI" else HK_PER_PT)
    lots = pos.get("lots", 1)
    pnl_hkd = round(pnl_pts * mult * lots, 0)
    win = pnl_hkd > 0
    pos["status"] = "CLOSED"
    pos["closed_at"] = _now_iso()
    pos["close_reason"] = reason
    pos["close_hsi"] = round(hsi_now, 2)
    pos["pnl_pts"] = pnl_pts
    pos["pnl_hkd"] = pnl_hkd
    pos["win"] = win
    t = load_track()
    t.setdefault("actual", []).append({
        "id": pos["id"], "date": pos.get("opened_at", "")[:10],
        "mode": pos.get("mode"),
        "K": next((l["strike"] for l in pos.get("legs", []) if l["cp"] == "CALL"), None),
        "L": next((l["strike"] for l in pos.get("legs", []) if l["cp"] == "PUT"), None),
        "premium_pts": prem, "pnl_hkd": pnl_hkd, "win": win,
        "close_reason": reason, "close_hsi": round(hsi_now, 2),
    })
    save_track(t)
    return pos


def auto_close_decision(pos, hsi_now, phase):
    """全自動平倉判斷：回 None（持有）或原因字串（平倉）。"""
    expiry = pos.get("expiry")
    if expiry:
        try:
            ed = datetime.strptime(expiry, "%Y-%m-%d")
            today = hs.now_hkt().replace(tzinfo=None)
            if phase == "close":
                hit = today.date() >= ed.date()
            else:
                hit = today.date() > ed.date()
            if hit:
                return "到期結算"
        except Exception:
            pass
    for leg in pos.get("legs", []):
        if leg["cp"] == "CALL" and hsi_now >= leg["strike"]:
            return "止蝕-升穿上沿"
        if leg["cp"] == "PUT" and hsi_now <= leg["strike"]:
            return "止蝕-跌破下沿"
    return None


def _first_trading_day_of_week(today_str):
    """今日所屬週嘅首個交易日（YYYY-MM-DD），用交易日曆；fallback 星期一。"""
    try:
        d0 = datetime.strptime(today_str, "%Y-%m-%d")
    except Exception:
        return today_str
    monday = d0 - timedelta(days=d0.weekday())
    days = _hk_trading_days()
    for off in range(0, 5):
        cand = (monday + timedelta(days=off)).strftime("%Y-%m-%d")
        if cand in days:
            return cand
    return monday.strftime("%Y-%m-%d")


def queue_weekly(approved, real):
    """儲存用戶星期五批准嘅下週開倉決定。"""
    st = load_state()
    st["pending_weekly_order"] = {
        "approved": bool(approved),
        "real": bool(real),
        "queued_at": _now_iso(),
    }
    save_state(st)
    return {"success": True, "pending": st["pending_weekly_order"]}


def pending_weekly_view():
    return load_state().get("pending_weekly_order")


def auto_open_weekly(st, cfg):
    """週版開倉：只喺「星期五批准咗」嘅情況下，週一/週首交易日執行。
    預設 paper；批准時揀咗 real 而且有 FUTU_TRADE_PWD 先真盤。"""
    has_weekly = any(p.get("version") == "weekly" and p.get("status") == "OPEN"
                     for p in st.get("positions", []))
    if has_weekly:
        return None
    pending = st.get("pending_weekly_order") or {}
    if not pending.get("approved"):
        return None  # 未批准，唔開
    wd = hs.now_hkt().weekday()  # 0=Mon
    today_str = hs.now_hkt().strftime("%Y-%m-%d")
    first_td = _first_trading_day_of_week(today_str)
    is_week_start = (wd <= 1) or (first_td == today_str)
    if not is_week_start:
        return None  # 等到週一/週首交易日先執行
    # 清除批准（無論成功與否，只用一次，避免重複開）
    st["pending_weekly_order"] = None
    ws = compute_weekly_signal()
    if not ws.get("band") or not ws.get("contracts") or not ws["contracts"].get("put"):
        return {"weekly_open_error": "攞唔到週期權合約"}
    sig = {
        "mode": "Full",
        "hsi": ws["hsi"], "vhsi": ws["vhsi"],
        "expiry": ws.get("expiry"),
        "strikes": {"K": ws["band"]["upper"], "L": ws["band"]["lower"], "em": ws["band"]["em"]},
        "contracts": ws["contracts"],
    }
    lots = int(cfg.get("lots") or 1)
    want_real = bool(pending.get("real"))
    use_real = want_real and bool(os.environ.get("FUTU_TRADE_PWD"))
    res = open_position(sig, lots, real=use_real, version="weekly", instrument="HSI")
    if res.get("success"):
        res["position"]["real"] = use_real
        st.setdefault("positions", []).append(res["position"])
        return res["position"]
    return {"weekly_open_error": res.get("error")}


def auto_tick(open_new):
    """全自動 tick：結算到期預測 → 自動平倉 → （可選）自動開倉。"""
    phase = "close" if not open_new else "morning"
    st = load_state()
    cfg = st.get("config", {})
    auto = cfg.get("auto_mode") == "full"
    enabled = cfg.get("enabled", True)
    lots = int(cfg.get("lots") or 1)
    out = {"time": _now_iso(), "phase": phase, "auto_mode": cfg.get("auto_mode", "semi"),
           "enabled": bool(enabled), "opened": None, "closed": []}
    try:
        ctx = quote_ctx()
        _, hsi_now = hs.get_index_snapshot(ctx)
    except Exception:
        hsi_now = None
    if hsi_now is None:
        out["error"] = "攞唔到恒指即市價"
        return out

    out["resolved_n"] = resolve_track(hsi_now)

    open_pos = [x for x in st.get("positions", []) if x.get("status") == "OPEN"]
    for pos in open_pos:
        reason = auto_close_decision(pos, hsi_now, phase)
        if reason:
            settle_position(pos, hsi_now, reason)
            out["closed"].append({"id": pos["id"], "reason": reason,
                                  "pnl_hkd": pos.get("pnl_hkd"), "win": pos.get("win")})
    save_state(st)

    if open_new and auto and enabled:
        sig = compute_signal()
        track_today(sig)
        today = hs.now_hkt().strftime("%Y-%m-%d")
        last = st.get("last_auto_open")
        if sig["mode"] != "SKIP" and not sig.get("friday") and last != today:
            inst = cfg.get("instrument") or "HSI"
            res = open_position(sig, lots, real=False, version="daily", instrument=inst)
            if res.get("success"):
                st.setdefault("positions", []).append(res["position"])
                st["last_auto_open"] = today
                save_state(st)
                out["opened"] = {"id": res["position"]["id"], "mode": sig["mode"],
                                 "K": sig["strikes"].get("K"), "L": sig["strikes"].get("L"),
                                 "premium_pts": res["position"]["premium_pts"], "instrument": inst}
            else:
                out["open_error"] = res.get("error")
        # 週版全自動開倉（週一/二、VHSI≥20、本週未開）
        wpos = auto_open_weekly(st, cfg)
        if wpos:
            save_state(st)
            out["opened_weekly"] = {"id": wpos["id"],
                                    "K": next((l["strike"] for l in wpos["legs"] if l["cp"] == "CALL"), None),
                                    "L": next((l["strike"] for l in wpos["legs"] if l["cp"] == "PUT"), None),
                                    "premium_pts": wpos["premium_pts"]}
    return out


def track_view():
    t = load_track()
    res = t.get("resolved", [])
    wins = sum(1 for r in res if r.get("win"))
    bt = _load(BACKTEST_PATH, {})
    d5 = bt.get("results", {}).get("dte5", {}).get("gated", {})
    return {
        "backtest": {"win_rate_pct": d5.get("win_rate_pct"), "avg_pnl_hkd": d5.get("avg_pnl_hkd"),
                     "entered": d5.get("entered"), "by_mode": d5.get("by_mode", {})},
        "forward": {"n": len(res), "wins": wins,
                    "win_rate_pct": round(wins / len(res) * 100, 1) if res else None,
                    "pending": len(t.get("predictions", []))},
        "resolved": res[-30:],
        "actual": t.get("actual", []),
    }


def _trade_summary(trades):
    n = len(trades)
    if not n:
        return {"n": 0, "win_rate_pct": None, "avg_pnl_hkd": None, "worst_hkd": None, "total_pnl_hkd": None}
    pnls = [t["pnl_hkd"] for t in trades]
    wins = sum(1 for t in trades if t["win"])
    return {
        "n": n,
        "win_rate_pct": round(wins / n * 100, 1),
        "avg_pnl_hkd": round(sum(pnls) / n, 0),
        "worst_hkd": round(min(pnls), 0),
        "total_pnl_hkd": round(sum(pnls), 0),
    }


def history_view(limit=60):
    log = _load("hsi_trade_log.json", {})
    daily = log.get("daily", [])
    weekly = log.get("weekly", [])
    t = load_track()
    preds = t.get("predictions", [])
    resolved = t.get("resolved", [])
    # 前向 paper 補多幾個欄位，同回測 trade 對齊
    def norm(r):
        return {
            "date": r.get("date") or r.get("entry_date"),
            "mode": r.get("mode") or r.get("mode_label"),
            "K": r.get("K"),
            "L": r.get("L"),
            "prem_pts": r.get("prem_pts"),
            "pnl_hkd": r.get("pnl_hkd"),
            "win": r.get("win"),
        }
    fw_resolved = [norm(r) for r in resolved]
    fw_pending = [norm(r) for r in preds]
    return {
        "data": log.get("data", {}),
        "daily": {"summary": _trade_summary(daily), "trades": list(reversed(daily))[:limit]},
        "weekly": {"summary": _trade_summary(weekly), "trades": list(reversed(weekly))[:limit]},
        "forward": {
            "resolved": list(reversed(fw_resolved))[:limit],
            "pending": fw_pending,
            "summary": _trade_summary([r for r in fw_resolved if r.get("win") is not None]),
        },
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _json(h, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            p = urlparse(self.path).path
            if p == "/health":
                return _json(self, {"ok": True, "time": _now_iso()})
            if p == "/signal":
                sig = compute_signal()
                track_today(sig)
                resolve_track(sig["hsi"])
                return _json(self, sig)
            if p == "/signal/weekly":
                return _json(self, compute_weekly_signal())
            if p == "/preview_weekly":
                return _json(self, preview_next_week())
            if p == "/pending_weekly":
                return _json(self, {"pending": pending_weekly_view()})
            if p == "/signal/full":
                return _json(self, compute_full_signal(force=parse_qs(urlparse(self.path).query).get("force",[""])[0]=="1"))
            if p == "/positions":
                return _json(self, positions_view())
            if p == "/monitor":
                return _json(self, monitor_view())
            if p == "/track":
                return _json(self, track_view())
            if p == "/history":
                q = parse_qs(urlparse(self.path).query)
                try:
                    lim = int(q.get("limit", ["60"])[0])
                except Exception:
                    lim = 60
                return _json(self, history_view(limit=lim))
            if p == "/config":
                return _json(self, load_state().get("config", {}))
            return _json(self, {"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            return _json(self, {"error": str(e)}, 500)

    def do_POST(self):
        try:
            p = urlparse(self.path).path
            body = self._body()
            if p == "/tick":
                open_new = bool(body.get("open", True))
                return _json(self, auto_tick(open_new))
            if p == "/unlock":
                ok, err = verify_trade_pw(body.get("password"))
                return _json(self, {"ok": ok} if ok else {"ok": False, "error": err}, 200 if ok else 403)
            if p == "/queue_weekly":
                return _json(self, queue_weekly(bool(body.get("approved")), bool(body.get("real", False))))
            if p == "/config":
                st = load_state()
                st.setdefault("config", {}).update({k: body[k] for k in ("auto_mode", "enabled", "lots") if k in body})
                save_state(st)
                return _json(self, {"ok": True, "config": st["config"]})
            if p == "/order":
                version = body.get("version", "daily")
                instrument = body.get("instrument", "HSI")
                if version == "weekly":
                    ws = compute_weekly_signal()
                    if not ws.get("band"):
                        return _json(self, {"success": False, "error": "冇本週 band 數據"}, 400)
                    sig = {
                        "mode": "Full",
                        "hsi": ws["hsi"], "vhsi": ws["vhsi"],
                        "expiry": ws.get("expiry"),
                        "strikes": {"K": ws["band"]["upper"], "L": ws["band"]["lower"], "em": ws["band"]["em"]},
                        "contracts": ws.get("contracts") or {},
                    }
                else:
                    force = bool(body.get("force"))
                    sig = compute_signal(force=force)
                    if sig["mode"] == "SKIP" and not force:
                        return _json(self, {"success": False, "error": "今日 VHSI 分級 SKIP，唔應該入場（可剔強制照落）"}, 400)
                    if instrument == "MHI":
                        mhi = None
                        try:
                            ctx = quote_ctx()
                            mexp = pick_mhi_expiry(ctx)
                            if mexp is not None:
                                cm = pull_mhi_chain(ctx, mexp)
                                mhi = {"expiry": mexp.strftime("%Y-%m-%d"),
                                       **_chain_contracts(cm, sig["strikes"]["K"], sig["strikes"]["L"])}
                        finally:
                            try:
                                ctx.close()
                            except Exception:
                                pass
                        if not mhi or not mhi.get("put"):
                            return _json(self, {"success": False, "error": "攞唔到 MHI 月期權合約"}, 400)
                        sig["expiry"] = mhi["expiry"]
                        sig["contracts"] = {"call": mhi.get("call"), "put": mhi.get("put")}
                real = bool(body.get("real"))
                if real:
                    if not body.get("confirm"):
                        return _json(self, {"success": False, "need_confirm": True,
                                            "error": "真盤要 confirm=true 先會落單"}, 400)
                    # 實盤閘門：面板流程要入對交易密碼先過到呢度
                    ok, err = verify_trade_pw(body.get("trade_pw"))
                    if not ok:
                        return _json(self, {"success": False, "need_pw": True, "error": err}, 403)
                lots = int(body.get("lots") or load_state()["config"].get("lots", 1))
                res = open_position(sig, lots, real, version=version, instrument=instrument)
                if res.get("success"):
                    st = load_state()
                    st.setdefault("positions", []).append(res["position"])
                    save_state(st)
                return _json(self, res, 200 if res.get("success") else 400)
            if p == "/close":
                pid = body.get("position_id")
                st = load_state()
                pos = next((x for x in st.get("positions", []) if x.get("id") == pid), None)
                if not pos:
                    return _json(self, {"success": False, "error": "搵唔到持倉"}, 404)
                real = bool(pos.get("real"))
                if real and not body.get("confirm"):
                    return _json(self, {"success": False, "need_confirm": True, "error": "真盤平倉要 confirm=true"}, 400)
                if real:
                    for leg in pos["legs"]:
                        r = place_real_order(leg["code"], pos.get("lots", 1), "BUY", 0)
                        if not r["success"]:
                            return _json(self, {"success": False, "error": f"平倉失敗 {leg['code']}: {r['error']}"}, 400)
                        time.sleep(2.2)
                pos["status"] = "CLOSED"
                pos["closed_at"] = _now_iso()
                save_state(st)
                return _json(self, {"success": True, "position": pos})
            return _json(self, {"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            return _json(self, {"error": str(e)}, 500)


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"[hsi-strangle] listening :{LISTEN_PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
