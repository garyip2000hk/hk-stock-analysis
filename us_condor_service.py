#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""
SPX 週 Iron Condor 實戰（paper）服務 — VIX regime 閘門 + 注碼管理

方法論（同 us_condor_gate.py 回測完全一致）：
  - 逢週五收市入場，下週五到期（DTE5）
  - band = 1.15 × EM，EM = SPX × VIX/100 × √(DTE/365)；wings = 2.2 × EM
  - VIX regime 注碼：normal ×1 / caution・too_low ×0.5 / stress ×0（唔開倉）
  - 結算用每日 high/low 保守觸及（同回測同一條數），期權金係 BS 估計（無 OPRA）

端點：
  GET  /health
  GET  /signal       本週票（spot/VIX/regime/strikes/估計權金）+ 回測摘要
  GET  /positions    持倉 + 即市觸及狀態 + 浮動 P&L
  GET  /track        已結算戰績 + 累計 + 回測對照
  POST /tick         結算到期倉 + 週五開新倉（paper 全自動）
  GET  /config
  POST /config       {enabled}

狀態：options_data/us_condor_state.json
真盤落單未接（IB 無 OPRA 報價、期權下單流程未建）——本服務只做 paper 記錄。
"""

import json
import math
import os
import threading
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bs
import cross_market as cmx
import us_condor_gate as gate

LISTEN_PORT = int(os.environ.get("PORT", "8893"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "options_data", "us_condor_state.json")
BACKTEST_PATH = os.path.join(BASE_DIR, "options_data", "us_condor_gate.json")
USD_PER_PT = 100.0   # SPX 期權每點 $100 美元

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 狀態
# ---------------------------------------------------------------------------

def _default_state():
    return {"mode": "paper", "enabled": True, "positions": [], "settled": [], "log": []}


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
    st["log"] = st.get("log", [])[-200:]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def _log(st, msg):
    st.setdefault("log", []).append({"t": datetime.now().isoformat(timespec="seconds"), "msg": msg})


def _ticket():
    data = cmx.load_ib()
    return gate.ticket(data["SPX"], data["VIX"])


def _bars():
    data = cmx.load_ib()
    return data["SPX"].set_index("date"), data["VIX"]


# ---------------------------------------------------------------------------
# 結算 / 開倉（同回測同一套觸及邏輯）
# ---------------------------------------------------------------------------

def _touch_scan(spx, pos, up_to):
    """entry 之後、up_to 之前（含）嘅最大觸及點數。"""
    tc = tp = 0.0
    for d in spx.index:
        if pos["entry"] < d <= up_to:
            hi = float(spx.loc[d, "high"])
            lo = float(spx.loc[d, "low"])
            if hi >= pos["k_call"]:
                tc = max(tc, hi - pos["k_call"])
            if lo <= pos["k_put"]:
                tp = max(tp, pos["k_put"] - lo)
    return tc, tp


def _settle(pos, spx):
    tc, tp = _touch_scan(spx, pos, pos["expiry"])
    wing = pos["wing"]
    loss = min(max(tc, tp), wing)
    pnl_pts = round(pos["credit_pts"] - loss, 1)
    pnl_usd = round(pnl_pts * USD_PER_PT * pos["size_mult"], 0)
    pos.update({
        "status": "settled", "settled_at": datetime.now().isoformat(timespec="seconds"),
        "touch_call_pts": round(tc, 1), "touch_put_pts": round(tp, 1),
        "exit_reason": "expiry", "win": pnl_pts > 0,
        "pnl_pts": pnl_pts, "pnl_usd": pnl_usd,
        "result": {
            "touched": loss > 0,
            "loss_pts": round(loss, 1),
            "pnl_pts": pnl_pts, "pnl_usd": pnl_usd,
            "win": pnl_pts > 0,
            "settled_at": datetime.now().isoformat(timespec="seconds"),
            "reason": "expiry",
        },
    })
    return pos


def _open_from_ticket(st, tk, spx_last_date):
    wing_width = max(tk["condor"]["call_wing"] - tk["condor"]["call"],
                     tk["condor"]["put"] - tk["condor"]["put_wing"])
    pos = {
        "id": f"UC-{tk['expiry']}",
        "entry": spx_last_date, "expiry": tk["expiry"],
        "spot_entry": tk["spot"], "vix_entry": tk["vix"],
        "regime": tk["regime"]["regime"], "size_mult": tk["size_mult"],
        "k_put": tk["condor"]["put"], "k_call": tk["condor"]["call"],
        "w_put": tk["condor"]["put_wing"], "w_call": tk["condor"]["call_wing"],
        "wing": wing_width,
        "credit_pts": tk["est_credit_pts"],
        "margin_usd_est": round(wing_width * USD_PER_PT * tk["size_mult"], 0),
        "status": "open", "opened_at": datetime.now().isoformat(timespec="seconds"),
    }
    st["positions"].append(pos)
    _log(st, f"開倉 {pos['id']}：{pos['k_put']}/{pos['k_call']} credit {pos['credit_pts']}pts "
             f"VIX {pos['vix_entry']} ({pos['regime']}) ×{pos['size_mult']}")
    return pos


def tick():
    """結算到期倉＋週五開新倉。回傳動作摘要。"""
    with _lock:
        st = load_state()
        actions = []
        spx, _ = _bars()
        last_date = str(spx.index[-1])

        # 1) 結算
        still_open = []
        for pos in st["positions"]:
            if pos["expiry"] <= last_date:
                _settle(pos, spx)
                st["settled"].append(pos)
                actions.append({"action": "settle", "id": pos["id"], "pnl_pts": pos["pnl_pts"],
                                "pnl_usd": pos["pnl_usd"], "win": pos["win"]})
                _log(st, f"結算 {pos['id']}：{pos['pnl_pts']}pts（{'贏' if pos['win'] else '輸'}）"
                         f" US${pos['pnl_usd']:+,.0f}")
            else:
                still_open.append(pos)
        st["positions"] = still_open

        # 2) 開新倉（只喺週五收市數據日、冇持倉時）
        tk = None
        if not st["positions"] and datetime.strptime(last_date, "%Y-%m-%d").weekday() == 4:
            tk = _ticket()
            if not st.get("enabled", True):
                actions.append({"action": "skip", "reason": "enabled=false"})
            elif tk["size_mult"] <= 0:
                actions.append({"action": "skip", "reason": f"regime {tk['regime']['regime']}（VIX {tk['vix']}）唔開倉"})
                _log(st, f"跳過開倉：VIX {tk['vix']} regime {tk['regime']['regime']}")
            else:
                pos = _open_from_ticket(st, tk, last_date)
                actions.append({"action": "open", "id": pos["id"], "expiry": pos["expiry"],
                                "strikes": f"{pos['w_put']}/{pos['k_put']}P {pos['k_call']}/{pos['w_call']}C",
                                "credit_pts": pos["credit_pts"], "size_mult": pos["size_mult"]})
        save_state(st)
        return {"last_bar": last_date, "actions": actions, "ticket": tk}


# ---------------------------------------------------------------------------
# 視圖
# ---------------------------------------------------------------------------

def positions_view():
    st = load_state()
    out = []
    try:
        spx, _ = _bars()
        last_date = str(spx.index[-1])
        spot_now = float(spx["close"].iloc[-1])
    except Exception:
        spx, last_date, spot_now = None, None, None
    for pos in st["positions"]:
        p = dict(pos)
        p["strikes"] = {"put_wing": pos["w_put"], "put": pos["k_put"],
                        "call": pos["k_call"], "call_wing": pos["w_call"]}
        p["spot_at_entry"] = pos["spot_entry"]
        if spx is not None:
            tc, tp = _touch_scan(spx, pos, last_date)
            loss = min(max(tc, tp), pos["wing"])
            p["touch_call_pts"] = round(tc, 1)
            p["touch_put_pts"] = round(tp, 1)
            p["breached"] = loss > 0
            p["unreal_pts"] = round(pos["credit_pts"] - loss, 1)
            p["unreal_usd"] = round(p["unreal_pts"] * USD_PER_PT * pos["size_mult"], 0)
            days = [d for d in spx.index if pos["entry"] < d <= last_date]
            p["days_elapsed"] = len(days)
        p["spot_now"] = spot_now
        p["spot_at_entry"] = pos["spot_entry"]
        p["vix_at_entry"] = pos["vix_entry"]
        if "touch_call_pts" in p:
            p["unrealized"] = {
                "touched": p["breached"],
                "max_intrusion_pts": round(max(p["touch_call_pts"], p["touch_put_pts"]), 1),
                "pnl_pts": p["unreal_pts"],
                "pnl_usd": p["unreal_usd"],
                "bars_seen": p["days_elapsed"],
            }
        out.append(p)
    return {"mode": st["mode"], "enabled": st.get("enabled", True), "last_bar": last_date,
            "positions": out}


def track_view():
    st = load_state()
    settled = sorted(st["settled"], key=lambda p: p["expiry"], reverse=True)
    pts = [p["pnl_pts"] for p in settled]
    usd = [p["pnl_usd"] for p in settled]
    wins = sum(1 for p in settled if p.get("win"))
    agg = {
        "n": len(settled),
        "win_rate_pct": round(100 * wins / len(settled), 1) if settled else None,
        "total_pts": round(sum(pts), 1) if pts else 0,
        "total_usd": round(sum(usd), 0) if usd else 0,
        "avg_pts": round(sum(pts) / len(pts), 1) if pts else None,
        "worst_pts": round(min(pts), 1) if pts else None,
    }
    bt = {}
    try:
        with open(BACKTEST_PATH) as f:
            bt = json.load(f)["backtest"]["variants"]["gated_no_stress"]["condor"]
    except Exception:
        pass
    return {"aggregate": agg, "backtest_ref": bt, "settled": settled,
            "log": st.get("log", [])[-30:][::-1]}


def signal_view():
    tk = _ticket()
    bt_summary = None
    try:
        with open(BACKTEST_PATH) as f:
            v = json.load(f)["backtest"]["variants"]
        bt_summary = {k: (s.get("condor") or s) for k, s in v.items()}
    except Exception:
        pass
    pos = positions_view()
    return {"ticket": tk, "backtest": bt_summary,
            "backtest_ref": (bt_summary or {}).get("gated_no_stress"),
            "open_positions": len(pos["positions"]),
            "last_bar": pos["last_bar"]}


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
                return self._json({"ok": True, "service": "us-condor-api",
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
                                   "note": "paper only：IB 無 OPRA 報價、期權落單流程未建"})
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
            if path == "/config":
                with _lock:
                    st = load_state()
                    if "enabled" in body:
                        st["enabled"] = bool(body["enabled"])
                        _log(st, f"enabled={st['enabled']}")
                    save_state(st)
                return self._json({"ok": True, "enabled": st["enabled"]})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"us-condor-api listening :{LISTEN_PORT} (paper mode)", flush=True)
    srv.serve_forever()
