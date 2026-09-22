#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""
SPY 週 Iron Condor 執行服務 — 富途 OpenD 真報價 + 可落盤（paper / sim / real）

同 us_condor_gate.py（SPX 研究版）同一套方法論：
  - 逢週五美盤收市前入場，下週五到期
  - band = 1.15 × EM，EM = SPY × VIX/100 × √(DTE/365)；wings = 2.2 × EM
  - VIX regime 注碼：normal ×1 / caution・too_low ×0.5 / stress ×0（唔開倉）
分別：
  - 用 SPY（ETF 期權，富途支援報價＋落盤；SPX 指數期權富途唔支援）
  - 期權金用富途即市 bid/ask（唔再係 BS 估計）
  - sim 模式落富途美股模擬盤（10576499），real 落實盤保證金戶口（要 FUTU_TRADE_PWD）

端點：
  GET  /health /signal /positions /track /config
  POST /tick          結算到期倉（paper touch-scan）＋週五數據日開新倉
  POST /open          即刻用即市數據開倉（monitor 週五自動／手動）
  POST /close {id, reason}  平倉（sim/real 逐腿買回）
  POST /config {mode, enabled, base_qty}

狀態：options_data/us_spy_condor_state.json
"""

import json
import math
import os
import threading
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cross_market as cmx

LISTEN_PORT = int(os.environ.get("PORT", "8894"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "options_data", "us_spy_condor_state.json")
BACKTEST_PATH = os.path.join(BASE_DIR, "options_data", "us_condor_gate.json")

SPY = "US.SPY"
USD_PER_PT = 100.0      # SPY 期權 1 張 = 100 股，每 $1 期權金 = $100
MULT, WING = 1.15, 2.2  # 同回測一致
SIM_ACC = 10576499
REAL_ACC = 281756478612908634
TP_RATIO = 0.5          # 買回成本 ≤ 50% 權金 → 止賺（monitor 用）

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 狀態
# ---------------------------------------------------------------------------

def _default_state():
    return {"mode": "paper", "enabled": True, "base_qty": 1,
            "positions": [], "settled": [], "cycles": [], "log": []}


def load_state():
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
        for k, v in _default_state().items():
            st.setdefault(k, v)
        return st
    except Exception:
        return _default_state()


def save_state(st):
    st["log"] = st.get("log", [])[-300:]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def _log(st, msg):
    st.setdefault("log", []).append(
        {"t": datetime.now().isoformat(timespec="seconds"), "msg": msg})


# ---------------------------------------------------------------------------
# 報價 / 期權鏈（富途）
# ---------------------------------------------------------------------------

def futu_chain(expiry: str):
    from futu import OpenQuoteContext, RET_OK, IndexOptionType
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, data = ctx.get_option_chain(SPY, index_option_type=IndexOptionType.NORMAL,
                                         start=expiry, end=expiry)
        if ret != RET_OK:
            raise RuntimeError(f"get_option_chain fail: {data}")
        return data
    finally:
        ctx.close()


def futu_weekly_expiries(limit=12):
    from futu import OpenQuoteContext, RET_OK
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, data = ctx.get_option_expiration_date(SPY)
        if ret != RET_OK:
            raise RuntimeError(f"get_option_expiration_date fail: {data}")
        wk = data[data["expiration_cycle"] == "WEEK"]["strike_time"].tolist()
        return sorted(wk)[:limit]
    finally:
        ctx.close()


def futu_snapshot(codes):
    from futu import OpenQuoteContext, RET_OK
    if not codes:
        return {}
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        out = {}
        for i in range(0, len(codes), 100):
            batch = codes[i:i + 100]
            ret, data = ctx.get_market_snapshot(batch)
            if ret != RET_OK:
                raise RuntimeError(f"snapshot fail: {data}")
            for _, r in data.iterrows():
                out[r["code"]] = r.to_dict()
        return out
    finally:
        ctx.close()


def live_spot():
    snap = futu_snapshot([SPY]).get(SPY)
    if not snap:
        return None
    return float(snap.get("last_price") or 0) or None


# ---------------------------------------------------------------------------
# Ticket（真報價版）
# ---------------------------------------------------------------------------

def _snap_strike(raw, strikes, side):
    """揀最接近 raw 嘅行使價；put 唔高過現價、call 唔低過現價由调用方保證順序。"""
    return min(strikes, key=lambda s: abs(s - raw))


def build_ticket(spot=None, vix=None):
    """用即市 SPY + 最新 VIX 收市 + 富途鏈砌下週票。"""
    data = cmx.load_ib()
    spy_df, vix_df = data["SPY"], data["VIX"]
    if spot is None:
        spot = live_spot() or float(spy_df["close"].iloc[-1])
    if vix is None:
        vix = float(vix_df["close"].iloc[-1])
    reg = cmx.vix_regime(vix)
    sizing = {"normal": 1.0, "caution": 0.5, "too_low": 0.5,
              "stress": 0.0, "unknown": 0.0}.get(reg["regime"], 0.0)

    today = datetime.utcnow() + timedelta(hours=8)
    fridays = [e for e in futu_weekly_expiries(limit=24)
               if e > today.strftime("%Y-%m-%d")
               and datetime.strptime(e, "%Y-%m-%d").weekday() == 4]
    if not fridays:
        raise RuntimeError("富途攞唔到 SPY 週五到期日")
    expiry = fridays[0]
    dte = max(1, (datetime.strptime(expiry, "%Y-%m-%d") - today).days)

    em = spot * vix / 100 * math.sqrt(dte / 365)
    chain = futu_chain(expiry)
    strikes = sorted(set(chain["strike_price"].astype(float)))
    k_put = _snap_strike(spot - MULT * em, strikes, "P")
    k_call = _snap_strike(spot + MULT * em, strikes, "C")
    w_put = _snap_strike(spot - WING * em, [s for s in strikes if s < k_put] or strikes, "P")
    w_call = _snap_strike(spot + WING * em, [s for s in strikes if s > k_call] or strikes, "C")
    if not (w_put < k_put < spot < k_call < w_call):
        raise RuntimeError(f"行使價順序錯亂: {w_put}/{k_put}/{spot}/{k_call}/{w_call}")

    def pick(strike, opt):
        rows = chain[(chain["strike_price"].astype(float) == float(strike)) &
                     (chain["option_type"] == opt)]
        if rows.empty:
            raise RuntimeError(f"鏈上搵唔到 {opt} {strike}")
        return rows.iloc[0]["code"]

    legs = {
        "w_put": {"code": pick(w_put, "PUT"), "strike": float(w_put), "side": "buy"},
        "k_put": {"code": pick(k_put, "PUT"), "strike": float(k_put), "side": "sell"},
        "k_call": {"code": pick(k_call, "CALL"), "strike": float(k_call), "side": "sell"},
        "w_call": {"code": pick(w_call, "CALL"), "strike": float(w_call), "side": "buy"},
    }
    snap = futu_snapshot([v["code"] for v in legs.values()])
    for leg in legs.values():
        q = snap.get(leg["code"], {})
        leg["bid"] = float(q.get("bid_price") or 0)
        leg["ask"] = float(q.get("ask_price") or 0)
        leg["last"] = float(q.get("last_price") or 0)
    credit = (legs["k_put"]["bid"] + legs["k_call"]["bid"]
              - legs["w_put"]["ask"] - legs["w_call"]["ask"])
    wing_width = min(k_put - w_put, w_call - k_call)
    return {
        "as_of": today.strftime("%Y-%m-%d %H:%M HKT"),
        "vehicle": "SPY", "spot": round(spot, 2), "vix": round(vix, 2),
        "regime": reg, "size_mult": sizing, "allow_new": sizing > 0,
        "expiry": expiry, "dte": dte,
        "legs": legs,
        "strikes": {"put_wing": w_put, "put": k_put, "call": k_call, "call_wing": w_call},
        "wing_width_pts": round(wing_width, 1),
        "credit_pts": round(credit, 2),
        "credit_usd_per_contract": round(credit * USD_PER_PT, 0),
        "notes": [
            "期權金係富途即市 bid/ask（賣腿用 bid、買翼用 ask，保守可執行價）",
            "VIX regime 注碼同 SPX 回測一致：stress 唔開、caution/too_low 半注",
        ],
    }


# ---------------------------------------------------------------------------
# 落盤（富途）
# ---------------------------------------------------------------------------

def _trade_ctx(mode):
    from futu import OpenSecTradeContext, TrdMarket, TrdEnv, SecurityFirm
    env = TrdEnv.SIMULATE if mode == "sim" else TrdEnv.REAL
    ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host="127.0.0.1",
                              port=11111, security_firm=SecurityFirm.FUTUSECURITIES)
    acc = SIM_ACC if mode == "sim" else REAL_ACC
    if mode == "real":
        pwd = os.environ.get("FUTU_TRADE_PWD")
        if not pwd:
            raise RuntimeError("real 模式要 FUTU_TRADE_PWD（Settings → Advanced Secrets）")
        ret, msg = ctx.unlock_trade(password=pwd)
        if ret != 0:
            raise RuntimeError(f"unlock_trade fail: {msg}")
    return ctx, env, acc


def place_leg_orders(mode, legs, qty):
    """開倉：賣兩條短腿、買兩條翼。回 [{code, action, price, qty, order_id}]。"""
    from futu import TrdSide, OrderType
    ctx, env, acc = _trade_ctx(mode)
    orders = []
    try:
        for key in ("k_put", "k_call", "w_put", "w_call"):
            leg = legs[key]
            if leg["side"] == "sell":
                side, price = TrdSide.SELL, leg["bid"]
            else:
                side, price = TrdSide.BUY, leg["ask"]
            price = round(max(price, 0.01), 2)
            ret, data = ctx.place_order(price=price, qty=qty, code=leg["code"],
                                        trd_side=side, order_type=OrderType.NORMAL,
                                        trd_env=env, acc_id=acc,
                                        remark=f"USC-{key}")
            oid = str(data["order_id"].iloc[0]) if ret == 0 else None
            orders.append({"key": key, "code": leg["code"], "action": leg["side"],
                           "price": price, "qty": qty, "order_id": oid,
                           "ok": ret == 0, "msg": None if ret == 0 else str(data)})
            if ret != 0:
                raise RuntimeError(f"place_order {key} fail: {data}")
    finally:
        ctx.close()
    return orders


def close_leg_orders(mode, pos):
    """平倉：逐腿買回（賣出翼就賣回）。回買回成本（期權金點數）。"""
    from futu import TrdSide, OrderType
    snap = futu_snapshot([l["code"] for l in pos["legs"].values()])
    ctx, env, acc = _trade_ctx(mode)
    cost_pts = 0.0
    orders = []
    try:
        for key, leg in pos["legs"].items():
            q = snap.get(leg["code"], {})
            if leg["side"] == "sell":            # 短腿買回
                side, price = TrdSide.BUY, float(q.get("ask_price") or 0.01)
            else:                                # 翼賣出
                side, price = TrdSide.SELL, float(q.get("bid_price") or 0.01)
            price = round(max(price, 0.01), 2)
            qty = int(pos.get("qty", 1))
            ret, data = ctx.place_order(price=price, qty=qty, code=leg["code"],
                                        trd_side=side, order_type=OrderType.NORMAL,
                                        trd_env=env, acc_id=acc,
                                        remark=f"USC-close-{key}")
            oid = str(data["order_id"].iloc[0]) if ret == 0 else None
            orders.append({"key": key, "code": leg["code"], "price": price,
                           "qty": qty, "order_id": oid, "ok": ret == 0,
                           "msg": None if ret == 0 else str(data)})
            if ret != 0:
                raise RuntimeError(f"close {key} fail: {data}")
            cost_pts += price if leg["side"] == "sell" else -price
    finally:
        ctx.close()
    return round(cost_pts, 2), orders


def buyback_cost_pts(pos):
    """即市買回全部短腿＋賣出翼嘅淨成本（點數），唔使落單。"""
    snap = futu_snapshot([l["code"] for l in pos["legs"].values()])
    cost = 0.0
    for leg in pos["legs"].values():
        q = snap.get(leg["code"], {})
        if leg["side"] == "sell":
            cost += float(q.get("ask_price") or 0)
        else:
            cost -= float(q.get("bid_price") or 0)
    return round(cost, 2)


# ---------------------------------------------------------------------------
# 開倉 / 平倉 / 結算
# ---------------------------------------------------------------------------

def open_position(st, tk, source):
    qty = max(1, round(st["base_qty"] * tk["size_mult"]))
    mode = st["mode"]
    orders = []
    if mode in ("sim", "real") and tk["credit_pts"] > 0:
        orders = place_leg_orders(mode, tk["legs"], qty)
    pos = {
        "id": f"USC-{tk['expiry']}",
        "entry": datetime.now().isoformat(timespec="seconds"),
        "expiry": tk["expiry"], "dte": tk["dte"],
        "spot_entry": tk["spot"], "vix_entry": tk["vix"],
        "regime": tk["regime"]["regime"], "size_mult": tk["size_mult"],
        "qty": qty, "mode": mode, "source": source,
        "k_put": tk["strikes"]["put"], "k_call": tk["strikes"]["call"],
        "w_put": tk["strikes"]["put_wing"], "w_call": tk["strikes"]["call_wing"],
        "wing": tk["wing_width_pts"],
        "legs": tk["legs"],
        "credit_pts": tk["credit_pts"],
        "margin_usd_est": round(tk["wing_width_pts"] * USD_PER_PT * qty, 0),
        "orders": orders,
        "status": "open",
    }
    st["positions"].append(pos)
    st["cycles"].append(tk["expiry"])
    _log(st, f"開倉 {pos['id']}（{mode}/{source}）：{pos['w_put']}/{pos['k_put']}P "
             f"{pos['k_call']}/{pos['w_call']}C credit {pos['credit_pts']}pts ×{qty} "
             f"VIX {pos['vix_entry']} ({pos['regime']})")
    return pos


def close_position(st, pos, reason):
    mode = st["mode"]
    if mode in ("sim", "real"):
        cost_pts, orders = close_leg_orders(mode, pos)
    else:
        cost_pts, orders = buyback_cost_pts(pos), []
    pnl_pts = round(pos["credit_pts"] - cost_pts, 2)
    pos.update({
        "status": "closed", "closed_at": datetime.now().isoformat(timespec="seconds"),
        "close_reason": reason, "buyback_cost_pts": cost_pts,
        "close_orders": orders,
        "pnl_pts": pnl_pts,
        "pnl_usd": round(pnl_pts * USD_PER_PT * pos.get("qty", 1), 0),
        "win": pnl_pts > 0,
    })
    st["positions"] = [p for p in st["positions"] if p["id"] != pos["id"]]
    st["settled"].append(pos)
    _log(st, f"平倉 {pos['id']}（{reason}）：買回 {cost_pts}pts → P&L {pnl_pts:+.2f}pts "
             f"US${pos['pnl_usd']:+,.0f}")
    return pos


def _touch_scan(spy, pos, up_to):
    tc = tp = 0.0
    for _, row in spy.iterrows():
        d = row["date"]
        if pos.get("entry_date", pos["expiry"][:10]) < d <= up_to or pos.get("entry_date") is None and d <= up_to:
            pass
    # 用 entry 日（date 欄）做基準
    entry_d = pos.get("entry_date") or pos["entry"][:10]
    for _, row in spy.iterrows():
        d = row["date"]
        if entry_d < d <= up_to:
            if float(row["high"]) >= pos["k_call"]:
                tc = max(tc, float(row["high"]) - pos["k_call"])
            if float(row["low"]) <= pos["k_put"]:
                tp = max(tp, pos["k_put"] - float(row["low"]))
    return tc, tp


def settle_expired(st):
    """到期結算（保守：用每日 high/low 最大觸及，同回測同一條數）。"""
    data = cmx.load_ib()
    spy = data["SPY"]
    last_date = str(spy["date"].iloc[-1])
    actions = []
    for pos in list(st["positions"]):
        if pos["expiry"] <= last_date:
            tc, tp = _touch_scan(spy, pos, pos["expiry"])
            loss = min(max(tc, tp), pos["wing"])
            pnl_pts = round(pos["credit_pts"] - loss, 2)
            pos.update({
                "status": "settled", "settled_at": datetime.now().isoformat(timespec="seconds"),
                "touch_call_pts": round(tc, 2), "touch_put_pts": round(tp, 2),
                "close_reason": "expiry_touch",
                "pnl_pts": pnl_pts,
                "pnl_usd": round(pnl_pts * USD_PER_PT * pos.get("qty", 1), 0),
                "win": pnl_pts > 0,
            })
            st["positions"] = [p for p in st["positions"] if p["id"] != pos["id"]]
            st["settled"].append(pos)
            actions.append({"action": "settle", "id": pos["id"], "pnl_pts": pnl_pts,
                            "pnl_usd": pos["pnl_usd"], "win": pos["win"]})
            _log(st, f"結算 {pos['id']}：觸及 C{tc:.1f}/P{tp:.1f} → {pnl_pts:+.2f}pts")
    return actions, last_date


def tick():
    with _lock:
        st = load_state()
        actions, last_date = settle_expired(st)
        # 週五收市數據日、冇持倉、未開過下週倉 → 自動開（同回測節奏）
        if not st["positions"] and datetime.strptime(last_date, "%Y-%m-%d").weekday() == 4:
            if not st.get("enabled", True):
                actions.append({"action": "skip", "reason": "enabled=false"})
            else:
                try:
                    tk = build_ticket(spot=float(cmx.load_ib()["SPY"]["close"].iloc[-1]))
                    if tk["expiry"] in st["cycles"]:
                        actions.append({"action": "skip", "reason": f"週期 {tk['expiry']} 已開過"})
                    elif tk["size_mult"] <= 0:
                        actions.append({"action": "skip",
                                        "reason": f"regime {tk['regime']['regime']}（VIX {tk['vix']}）"})
                        _log(st, f"跳過開倉：VIX {tk['vix']} {tk['regime']['regime']}")
                    else:
                        pos = open_position(st, tk, "tick_friday")
                        actions.append({"action": "open", "id": pos["id"],
                                        "expiry": pos["expiry"], "credit_pts": pos["credit_pts"]})
                except Exception as e:
                    actions.append({"action": "error", "reason": str(e)})
        save_state(st)
        return {"last_bar": last_date, "actions": actions}


def open_now(source="manual"):
    with _lock:
        st = load_state()
        if not st.get("enabled", True):
            save_state(st)
            return {"ok": False, "reason": "enabled=false"}
        if st["positions"]:
            save_state(st)
            return {"ok": False, "reason": "已有持倉 " + st["positions"][0]["id"]}
        tk = build_ticket()
        if tk["expiry"] in st["cycles"]:
            save_state(st)
            return {"ok": False, "reason": f"週期 {tk['expiry']} 已開過"}
        if tk["size_mult"] <= 0:
            save_state(st)
            return {"ok": False, "reason": f"VIX {tk['vix']} regime {tk['regime']['regime']} 唔開倉",
                    "ticket": tk}
        pos = open_position(st, tk, source)
        save_state(st)
        return {"ok": True, "position": pos, "ticket": tk}


# ---------------------------------------------------------------------------
# 視圖
# ---------------------------------------------------------------------------

def positions_view():
    st = load_state()
    out = []
    try:
        spot = live_spot()
    except Exception:
        spot = None
    for pos in st["positions"]:
        p = dict(pos)
        if spot:
            p["spot_now"] = spot
            p["breached_call"] = spot >= pos["k_call"]
            p["breached_put"] = spot <= pos["k_put"]
            try:
                p["buyback_cost_pts"] = buyback_cost_pts(pos)
                p["unreal_pts"] = round(pos["credit_pts"] - p["buyback_cost_pts"], 2)
                p["unreal_usd"] = round(p["unreal_pts"] * USD_PER_PT * pos.get("qty", 1), 0)
            except Exception:
                pass
        out.append(p)
    return {"mode": st["mode"], "enabled": st.get("enabled", True),
            "base_qty": st.get("base_qty", 1), "positions": out}


def track_view():
    st = load_state()
    settled = sorted(st["settled"], key=lambda p: p["expiry"], reverse=True)
    pts = [p.get("pnl_pts", 0) for p in settled]
    wins = sum(1 for p in settled if p.get("win"))
    agg = {"n": len(settled),
           "win_rate_pct": round(100 * wins / len(settled), 1) if settled else None,
           "total_pts": round(sum(pts), 1) if pts else 0,
           "total_usd": round(sum(p.get("pnl_usd", 0) for p in settled), 0) if settled else 0,
           "worst_pts": round(min(pts), 1) if pts else None}
    return {"aggregate": agg, "settled": settled, "log": st.get("log", [])[-40:][::-1]}


def signal_view():
    tk = None
    err = None
    try:
        tk = build_ticket()
    except Exception as e:
        err = str(e)
    bt = None
    try:
        with open(BACKTEST_PATH) as f:
            bt = json.load(f)["backtest"]["variants"]["gated_no_stress"]["condor"]
    except Exception:
        pass
    st = load_state()
    return {"ticket": tk, "error": err, "mode": st["mode"],
            "open_positions": len(st["positions"]),
            "backtest_ref": bt,
            "annual_estimate": {
                "avg_pts_per_week": 3.0, "win_rate_pct": 80.6, "n_weeks": 67,
                "note": "回測原始年化 ~156pts；實際權金低 10-20% 後淨 ~40-60pts/年/SPX 倉，"
                        "SPY 每張 = 1/10，約 $400-600 USD/年，按保證金計 ~25-40%",
            }}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/health":
                return self._json({"ok": True, "service": "us-spy-condor-api",
                                   "time": datetime.now().isoformat(timespec="seconds")})
            if path == "/signal":
                return self._json(signal_view())
            if path == "/positions":
                return self._json(positions_view())
            if path == "/track":
                return self._json(track_view())
            if path == "/config":
                st = load_state()
                return self._json({"mode": st["mode"], "enabled": st.get("enabled", True),
                                   "base_qty": st.get("base_qty", 1),
                                   "tp_ratio": TP_RATIO,
                                   "note": "paper=只記錄；sim=富途模擬盤；real=實盤（要 FUTU_TRADE_PWD）"})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._body()
            if path == "/tick":
                return self._json(tick())
            if path == "/open":
                return self._json(open_now(body.get("source", "manual")))
            if path == "/close":
                with _lock:
                    st = load_state()
                    pos = next((p for p in st["positions"]
                                if p["id"] == body.get("id") or not body.get("id")), None)
                    if not pos:
                        return self._json({"error": "冇持倉"}, 400)
                    pos = close_position(st, pos, body.get("reason", "manual"))
                    save_state(st)
                    return self._json({"ok": True, "closed": pos})
            if path == "/config":
                with _lock:
                    st = load_state()
                    if body.get("mode") in ("paper", "sim", "real"):
                        st["mode"] = body["mode"]
                    if "enabled" in body:
                        st["enabled"] = bool(body["enabled"])
                    if "base_qty" in body:
                        st["base_qty"] = max(1, int(body["base_qty"]))
                    _log(st, f"config: mode={st['mode']} enabled={st['enabled']} qty={st['base_qty']}")
                    save_state(st)
                return self._json({"ok": True, "mode": st["mode"],
                                   "enabled": st["enabled"], "base_qty": st["base_qty"]})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"us-spy-condor-api listening :{LISTEN_PORT}", flush=True)
    srv.serve_forever()
