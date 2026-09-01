"""direction_advisor_service.py — 「方向揀策略」HTTP 服務（port 8895）。

端點：
  GET  /health
  GET  /stocks                 → 期權標的名單（代號＋名）
  GET  /analyze?code=00700&dir=up|flat|down
  POST /live_quote             {codes:[...]} → OpenD 即市 bid/ask
  POST /order_spec             {code, dir, idx, qty}
                               → 純訂單規格，交由嵌入父頁預覽及確認
  POST /order                  相容別名，亦只返回訂單規格，不會直接落單
  GET  /orders?limit=20        → 最近落單記錄

結果 cache 1 小時（HKEX 日報每日更新一次，唔使重算）。
用 /usr/local/bin/python3 跑。真盤解鎖靠環境變數 FUTU_TRADE_PWD（Secrets）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import direction_advisor as da

PORT = 8895
CACHE: dict[tuple, tuple[float, dict]] = {}
CACHE_TTL = 3600.0
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ORDERS_PATH = os.path.join(BASE_DIR, "options_data", "option_advisor_orders.json")
OPEND_HOST = os.environ.get("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.environ.get("OPEND_PORT", "11111"))
ORDER_THROTTLE_S = 2.2   # 富途落單限頻：每 30 秒最多 15 次

# ---------------------------------------------------------------- 分析（有 cache）

def _norm_code(code: str, market: str) -> str:
    code = (code or "").strip()
    if market == "hk_stock":
        return code.zfill(5)
    return code.upper()


def _analyze(code: str, direction: str, market: str = "hk_stock",
             instrument: str = "HSI") -> dict:
    """cache：港股期權用日報（1 小時）；期指／美股即市數據（5 分鐘）。"""
    code = _norm_code(code, market)
    key = (code, direction, market, instrument)
    ttl = CACHE_TTL if market == "hk_stock" else 300.0
    hit = CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    res = da.advise(code, direction, market=market, instrument=instrument)
    CACHE[key] = (time.time(), res)
    return res


# ---------------------------------------------------------------- OpenD（lazy）

_quote_ctx = None
_trade_ctx = None
_trade_lock = threading.Lock()
_unlocked = {"REAL": False}


def quote_ctx():
    global _quote_ctx
    from futu import OpenQuoteContext
    if _quote_ctx is None:
        _quote_ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    return _quote_ctx


def trade_ctx():
    global _trade_ctx
    from futu import OpenSecTradeContext, SecurityFirm
    with _trade_lock:
        if _trade_ctx is None:
            _trade_ctx = OpenSecTradeContext(
                host=OPEND_HOST, port=OPEND_PORT,
                security_firm=SecurityFirm.FUTUSECURITIES)
        return _trade_ctx


def ensure_unlocked():
    if _unlocked["REAL"]:
        return
    pwd = os.environ.get("FUTU_TRADE_PWD")
    if not pwd:
        raise RuntimeError("FUTU_TRADE_PWD 未設定（去 Zo Secrets 加返先可以真落單）")
    from futu import RET_OK
    ret, data = trade_ctx().unlock_trade(pwd)
    if ret != RET_OK:
        raise RuntimeError(f"解鎖交易失敗: {data}")
    _unlocked["REAL"] = True


def real_account() -> str:
    """賣期權要保證金戶口（naked short 唔可以用 CASH），優先 MARGIN。"""
    from futu import RET_OK
    ret, df = trade_ctx().get_acc_list()
    if ret != RET_OK:
        raise RuntimeError(f"攞唔到戶口: {df}")
    for _, r in df.iterrows():
        if (str(r.get("trd_env", "")).upper() == "REAL"
                and str(r.get("acc_type", "")).upper() == "MARGIN"
                and str(r.get("acc_status", "")).upper() == "ACTIVE"):
            return str(r["acc_id"])
    for _, r in df.iterrows():
        if (str(r.get("trd_env", "")).upper() == "REAL"
                and str(r.get("acc_status", "")).upper() == "ACTIVE"):
            return str(r["acc_id"])
    raise RuntimeError("冇 ACTIVE 嘅 REAL 戶口")


def live_quotes(codes: list[str]) -> dict[str, dict]:
    """一次過攞期權即市報價（每批 ≤200）。"""
    from futu import RET_OK
    out: dict[str, dict] = {}
    ctx = quote_ctx()
    for i in range(0, len(codes), 200):
        batch = codes[i:i + 200]
        ret, data = ctx.get_market_snapshot(batch)
        if ret != RET_OK:
            raise RuntimeError(f"snapshot 失敗: {data}")
        for _, r in data.iterrows():
            code = str(r.get("code", ""))

            def fnum(k):
                try:
                    v = float(r.get(k))
                    return None if v != v or v == 0 else v
                except Exception:
                    return None

            out[code] = {
                "name": str(r.get("name") or code),
                "last": fnum("last_price"),
                "bid": fnum("bid_price"),
                "ask": fnum("ask_price"),
            }
    return out


def _order_log_append(entry: dict):
    try:
        with open(ORDERS_PATH, encoding="utf-8") as f:
            log = json.load(f)
    except Exception:
        log = []
    log.append(entry)
    log = log[-500:]
    tmp = ORDERS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ORDERS_PATH)


def _order_log_read(limit: int = 20) -> list[dict]:
    try:
        with open(ORDERS_PATH, encoding="utf-8") as f:
            return json.load(f)[-limit:]
    except Exception:
        return []


def do_order(body: dict) -> dict:
    """建立訂單規格，不直接發出任何 paper 或 REAL 交易。

    真盤控制 token 只留在 gsmart-box 父頁。iframe 前端取得本回應後，必須以
    ``advisor_place_order`` postMessage 交回父頁，再由父頁 modal 顯示報價預覽及
    要求使用者逐筆確認。
    """
    market = str(body.get("market") or "hk_stock").strip().lower()
    if market not in ("hk_stock", "hk_index", "us_stock"):
        return {"ok": False, "error": "market 必須係 hk_stock / hk_index / us_stock"}
    code = str(body.get("code") or "").strip()
    instrument = str(body.get("instrument") or "").strip().upper() or None
    direction = str(body.get("dir") or "").strip().lower()
    try:
        idx = int(body.get("idx", 0))
        qty = int(body.get("qty", 1) or 1)
    except (TypeError, ValueError):
        return {"ok": False, "error": "策略編號及張數必須係整數"}
    if market == "hk_stock":
        code = code.zfill(5)
        if not code.isdigit() or len(code) != 5:
            return {"ok": False, "error": "請輸入 5 位股票代號"}
    elif market == "hk_index":
        code = (instrument or code).upper()
        if code not in ("HSI", "MHI"):
            return {"ok": False, "error": "指數期權代號要係 HSI 或 MHI"}
        instrument = code
    else:
        code = code.upper()
        if not code or len(code) > 8 or not code.isalnum():
            return {"ok": False, "error": "美股代號無效"}
    if direction not in ("up", "flat", "down"):
        return {"ok": False, "error": "dir 必須係 up / flat / down"}
    if qty <= 0 or qty > 500:
        return {"ok": False, "error": "張數要係 1-500"}

    res = _analyze(code, direction, market, instrument or "HSI")
    if not res.get("ok"):
        return res
    options = [res.get("best")] + (res.get("alternatives") or [])
    if idx < 0 or idx >= len(options) or not options[idx]:
        return {"ok": False, "error": f"策略編號 {idx} 超出範圍"}
    strat = options[idx]
    legs = []
    for leg in strat.get("legs") or []:
        futu_code = str(leg.get("futu_code") or "").strip().upper()
        action = str(leg.get("action") or "").strip()
        if not futu_code or action not in ("買入", "賣出"):
            return {"ok": False, "error": "策略腿資料不完整，無法建立訂單規格"}
        output_leg = {"futu_code": futu_code, "action": action}
        if leg.get("cp") in ("C", "P"):
            output_leg["cp"] = leg["cp"]
        if isinstance(leg.get("strike"), (int, float)):
            output_leg["strike"] = leg["strike"]
        if isinstance(leg.get("price"), (int, float)):
            output_leg["price"] = leg["price"]
        legs.append(output_leg)
    if not legs:
        return {"ok": False, "error": "策略沒有有效交易腿"}

    strategy_name = str(strat.get("strategy") or "三方向期權策略")
    expiry = str(strat.get("expiry") or "")
    spec = {
        "title": f"{res.get('name') or code}｜{strategy_name}",
        "subtitle": expiry or None,
        "legs": legs,
        "stock": code,
        "name": res.get("name"),
        "market": market,
        "direction": direction,
        "strategyName": strategy_name,
        "expiry": expiry or None,
        "dte": strat.get("dte"),
        "contractSize": res.get("contract_size") or 1,
        "winRate": strat.get("win_rate", res.get("win_rate")),
        "qty": qty,
        "qtyStep": 1,
        "qtyMax": 500,
        "qtyUnit": "張",
    }
    return {"ok": True, "type": "advisor_place_order", "spec": spec}



# ---------------------------------------------------------------- 持倉 + 止賺止蝕監控
# 港交所／OpenD 冇原生條件盤（期權冇止賺止蝕掛單），所以由本服務 24/7 盯市：
# 組合淨權金 P&L 到 ±設定百分比 → 自動落反向單平倉（paper 記帳／real 經 OpenD）。

POS_PATH = os.path.join(BASE_DIR, "options_data", "option_advisor_positions.json")
_pos_lock = threading.Lock()
MONITOR_INTERVAL_S = 20


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pos_read() -> dict:
    try:
        with open(POS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _pos_write(d: dict):
    tmp = POS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, POS_PATH)


def open_position(entry: dict, contract_size) -> str:
    pid = "P" + re.sub(r"[^0-9]", "", entry["time"])[:14] + entry["stock"]
    pos = {
        "id": pid, "opened_at": entry["time"], "stock": entry["stock"],
        "name": entry.get("name"), "market": entry.get("market", "hk_stock"),
        "direction": entry["direction"],
        "strategy": entry["strategy"], "expiry": entry.get("expiry"),
        "dte": entry.get("dte"), "mode": entry.get("mode", "paper"),
        "contract_size": int(contract_size or 1),
        "legs": [
            {"futu_code": l["futu_code"], "side": l["side"], "cp": l.get("cp"),
             "strike": l.get("strike"), "qty": l["qty"], "open_px": l["price"]}
            for l in entry["legs"]],
        "tp_pct": 50.0, "sl_pct": 50.0, "tp_sl_enabled": True,
        "status": "open", "auto_closed": False,
        "closed_at": None, "close_reason": None, "close_cash": None,
        "pnl_hkd": None, "close_result": None,
    }
    with _pos_lock:
        d = _pos_read()
        d[pid] = pos
        _pos_write(d)
    return pid


def _leg_cash(l: dict, px, closing: bool) -> float:
    sign = 1.0 if l["side"] == "SELL" else -1.0
    if closing:
        sign = -sign
    return sign * float(px) * l["qty"]


def mark_position(pos: dict, quotes: dict) -> dict:
    """計實時組合 P&L（平倉口價：長腿 bid、短腿 ask）＋止賺止蝕觸發狀態。"""
    cs = pos.get("contract_size") or 1
    entry_cash = sum(_leg_cash(l, l["open_px"], False) for l in pos["legs"]) * cs
    close_cash, ok = 0.0, True
    marks = []
    for l in pos["legs"]:
        q = quotes.get(l["futu_code"]) or {}
        px = (q.get("bid") if l["side"] == "BUY" else q.get("ask")) or q.get("last")
        if px is None:
            ok = False
            px = l["open_px"]
        close_cash += _leg_cash(l, px, True) * cs
        marks.append({"futu_code": l["futu_code"], "mark_px": round(float(px), 3),
                      "bid": q.get("bid"), "ask": q.get("ask")})
    pnl = entry_cash + close_cash
    base = abs(entry_cash) or 1.0
    tp_lvl = pos["tp_pct"] / 100.0 * base
    sl_lvl = -pos["sl_pct"] / 100.0 * base
    return {"entry_cash": round(entry_cash, 2), "close_cash": round(close_cash, 2),
            "pnl_hkd": round(pnl, 2), "base": round(base, 2),
            "tp_hkd": round(tp_lvl, 2), "sl_hkd": round(sl_lvl, 2),
            "quotes_ok": ok, "marks": marks,
            "tp_hit": pnl >= tp_lvl, "sl_hit": pnl <= sl_lvl}


def close_position(pid: str, reason: str, auto: bool) -> dict:
    """平倉：paper＝按市值記帳；real＝OpenD 落反向限價單（2.2s/腿）。"""
    from futu import OrderType, RET_OK, TrdEnv, TrdSide

    with _pos_lock:
        d = _pos_read()
        pos = d.get(pid)
        if not pos or pos["status"] != "open":
            return {"ok": False, "error": "搵唔到呢個持倉（或者已經平咗）"}
        pos["status"] = "closing"
        d[pid] = pos
        _pos_write(d)

    codes = [l["futu_code"] for l in pos["legs"]]
    try:
        quotes = live_quotes(codes)
    except Exception:  # noqa: BLE001
        quotes = {}

    if pos["mode"] != "real":
        mk = mark_position(pos, quotes)
        with _pos_lock:
            d = _pos_read()
            p = d[pid]
            p.update(status="closed", closed_at=utcnow_iso(), close_reason=reason,
                     auto_closed=auto, close_cash=mk["close_cash"], pnl_hkd=mk["pnl_hkd"])
            d[pid] = p
            _pos_write(d)
        _order_log_append({"time": utcnow_iso(), "stock": pos["stock"],
                           "type": "close(paper)", "pos_id": pid, "reason": reason,
                           "pnl_hkd": mk["pnl_hkd"]})
        return {"ok": True, "mode": "paper", "pnl_hkd": mk["pnl_hkd"], "reason": reason}

    ensure_unlocked()
    acc = real_account()
    ctx = trade_ctx()
    results = []
    for i, l in enumerate(pos["legs"]):
        if i:
            time.sleep(ORDER_THROTTLE_S)
        q = quotes.get(l["futu_code"]) or {}
        if l["side"] == "BUY":   # 平長倉 → 賣
            side, px = TrdSide.SELL, q.get("bid") or q.get("last") or l["open_px"]
        else:                    # 平短倉 → 買回
            side, px = TrdSide.BUY, q.get("ask") or q.get("last") or l["open_px"]
        ret, data = ctx.place_order(
            price=float(px), qty=l["qty"], code=l["futu_code"], trd_side=side,
            order_type=OrderType.NORMAL, trd_env=TrdEnv.REAL, acc_id=acc,
            remark="advisor-tpsl")
        if ret == RET_OK:
            try:
                oid = str(data["orderid"].iloc[0])
            except Exception:
                oid = str(data)
            results.append({"futu_code": l["futu_code"], "success": True, "order_id": oid})
        else:
            results.append({"futu_code": l["futu_code"], "success": False, "error": str(data)})

    okk = all(r["success"] for r in results)
    mk = mark_position(pos, quotes)
    with _pos_lock:
        d = _pos_read()
        p = d[pid]
        p.update(status="closed" if okk else "close_failed", closed_at=utcnow_iso(),
                 close_reason=reason, auto_closed=auto, close_cash=mk["close_cash"],
                 pnl_hkd=mk["pnl_hkd"], close_result=results)
        d[pid] = p
        _pos_write(d)
    _order_log_append({"time": utcnow_iso(), "stock": pos["stock"],
                       "type": "close(real)", "pos_id": pid, "reason": reason,
                       "result": results})
    return {"ok": okk, "mode": "real", "result": results, "reason": reason}


def positions_view(limit: int = 30) -> list[dict]:
    """全部持倉＋已平倉，新→舊；open 嘅附實時 P&L。"""
    d = _pos_read()
    items = sorted(d.values(), key=lambda p: p.get("opened_at") or "", reverse=True)[:limit]
    open_items = [p for p in items if p["status"] == "open"]
    quotes = {}
    if open_items:
        codes = sorted({l["futu_code"] for p in open_items for l in p["legs"]})
        try:
            quotes = live_quotes(codes)
        except Exception:  # noqa: BLE001
            quotes = {}
    out = []
    for p in items:
        v = dict(p)
        if p["status"] == "open":
            mk = mark_position(p, quotes)
            if not quotes:
                mk["quotes_ok"] = False
            v["mark"] = mk
        out.append(v)
    return out


def set_tp_sl(body: dict) -> dict:
    pid = str(body.get("pos_id") or "").strip()
    with _pos_lock:
        d = _pos_read()
        pos = d.get(pid)
        if not pos or pos["status"] != "open":
            return {"ok": False, "error": "搵唔到呢個持倉（或者已經平咗）"}
        tp = float(body.get("tp_pct", pos["tp_pct"]))
        sl = float(body.get("sl_pct", pos["sl_pct"]))
        if not (1 <= tp <= 500) or not (1 <= sl <= 500):
            return {"ok": False, "error": "止賺／止蝕百分比要係 1-500"}
        pos["tp_pct"] = round(tp, 1)
        pos["sl_pct"] = round(sl, 1)
        pos["tp_sl_enabled"] = bool(body.get("enabled", True))
        d[pid] = pos
        _pos_write(d)
    return {"ok": True, "pos_id": pid, "tp_pct": pos["tp_pct"], "sl_pct": pos["sl_pct"],
            "enabled": pos["tp_sl_enabled"]}


def _hkt_now():
    from datetime import timedelta
    return datetime.now(timezone.utc) + timedelta(hours=8)


def _in_trading_hours() -> bool:
    t = _hkt_now()
    if t.weekday() >= 5:
        return False
    hm = t.hour * 60 + t.minute
    return (9 * 60 + 15) <= hm <= (16 * 60 + 10)


def _in_us_trading_hours() -> bool:
    """美股時段（HKT）：夏令 21:30-04:00／冬令 22:30-05:00，跨日處理。"""
    t = _hkt_now()
    hm = t.hour * 60 + t.minute
    wd = t.weekday()
    if hm >= 21 * 60 + 15 and wd <= 3:      # 週一至四晚（開市段）
        return True
    if hm <= 5 * 60 + 15 and 1 <= wd <= 4:  # 週二至五朝早（收市段）
        return True
    return False


def _monitor_loop():
    """每 20 秒掃一次：open 倉實時 P&L 掂到止賺／止蝕就自動平倉；DTE ≤ 1 強制平。"""
    while True:
        try:
            time.sleep(MONITOR_INTERVAL_S)
            hk_on, us_on = _in_trading_hours(), _in_us_trading_hours()
            if not hk_on and not us_on:
                continue
            d = _pos_read()
            open_ids = []
            for pid, p in d.items():
                if p["status"] != "open" or not p.get("tp_sl_enabled"):
                    continue
                if p.get("market", "hk_stock") == "us_stock":
                    if us_on:
                        open_ids.append(pid)
                elif hk_on:
                    open_ids.append(pid)
            if not open_ids:
                continue
            codes = sorted({l["futu_code"] for pid in open_ids for l in d[pid]["legs"]})
            quotes = live_quotes(codes)
            for pid in open_ids:
                pos = d[pid]
                mk = mark_position(pos, quotes)
                if not mk["quotes_ok"]:
                    continue
                if mk["tp_hit"]:
                    close_position(pid, f"止賺觸發：P&L {mk['pnl_hkd']:+,.0f} ≥ "
                                        f"+{pos['tp_pct']:.0f}% 淨權金（+{mk['tp_hkd']:,.0f}）", True)
                elif mk["sl_hit"]:
                    close_position(pid, f"止蝕觸發：P&L {mk['pnl_hkd']:+,.0f} ≤ "
                                        f"−{pos['sl_pct']:.0f}% 淨權金（{mk['sl_hkd']:,.0f}）", True)
                elif pos.get("dte") is not None and pos["dte"] <= 1:
                    close_position(pid, "臨近到期（DTE ≤ 1）自動平倉", True)
        except Exception as e:  # noqa: BLE001
            print("monitor error:", repr(e), flush=True)


# ---------------------------------------------------------------- HTTP

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Ext-Token")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/health":
                return self._json({"ok": True, "service": "option-advisor",
                                   "cached": len(CACHE)})
            if u.path == "/stocks":
                m = (q.get("market") or "hk_stock").strip().lower()
                return self._json({"ok": True, "market": m,
                                   "stocks": da.stocks(m)})
            if u.path == "/orders":
                return self._json({"ok": True,
                                   "orders": _order_log_read(int(q.get("limit", 20)))})
            if u.path == "/positions":
                return self._json({"ok": True,
                                   "positions": positions_view(int(q.get("limit", 30))),
                                   "monitor_interval_s": MONITOR_INTERVAL_S,
                                   "trading_hours": _in_trading_hours(),
                                   "us_trading_hours": _in_us_trading_hours()})
            if u.path == "/analyze":
                code = (q.get("code") or "").strip()
                d = (q.get("dir") or "").strip().lower()
                m = (q.get("market") or "hk_stock").strip().lower()
                inst = (q.get("instrument") or "HSI").strip().upper()
                if not code:
                    return self._json({"ok": False, "error": "請輸入股票代號"}, 400)
                if d not in ("up", "flat", "down"):
                    return self._json({"ok": False,
                                       "error": "dir 必須係 up / flat / down"}, 400)
                if m not in ("hk_stock", "hk_index", "us_stock"):
                    return self._json({"ok": False,
                                       "error": "market 必須係 hk_stock / hk_index / us_stock"}, 400)
                return self._json(_analyze(code, d, m, inst))
            return self._json({"ok": False, "error": "唔識嘅路徑"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": f"server error: {e}"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        try:
            if u.path == "/live_quote":
                codes = [str(c).strip().upper() for c in (body.get("codes") or [])]
                codes = [c for c in codes if c][:200]
                if not codes:
                    return self._json({"ok": False, "error": "冇期權代號"}, 400)
                return self._json({"ok": True, "quotes": live_quotes(codes)})
            if u.path in ("/order", "/order_spec"):
                return self._json(do_order(body))
            if u.path == "/tp_sl":
                return self._json(set_tp_sl(body))
            if u.path == "/close_pos":
                if not body.get("confirm"):
                    return self._json({"ok": False,
                                       "error": "需要 confirm=true（前端兩步確認）"}, 400)
                return self._json(close_position(str(body.get("pos_id") or ""),
                                                 str(body.get("reason") or "手動平倉"),
                                                 auto=False))
            return self._json({"ok": False, "error": "唔識嘅路徑"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": f"server error: {e}"}, 500)


if __name__ == "__main__":
    threading.Thread(target=_monitor_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"option-advisor listening on :{PORT}", flush=True)
    srv.serve_forever()
