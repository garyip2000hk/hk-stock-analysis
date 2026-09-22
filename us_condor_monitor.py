#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""
SPY Iron Condor 自動平倉監視器（常駐服務，唔燒 AI 額度）

只喺美股交易時段做嘢（ET 09:30–16:05，週一至五）：
  - 每 60 秒攞一次 US.SPY 即市價 + 持倉四腿買回價
  - 止蝕：SPY 升穿 short call K 或 跌穿 short put L → 即刻平倉
  - 止賺：四腿買回總價 ≤ 50% 入場權金 → 平倉袋住
  - 到期：到期日 ET 15:30 自動平倉（唔等結算，避 pin risk）
  - 開倉：週五 ET 15:50 冇倉 → call /open 自動開下週新倉（VIX stress 服務會自己擋）

所有落單決定經 us-spy-condor-api（localhost:8894）——mode paper/sim/real 由服務 state 決定。
"""

import json
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

API = "http://localhost:8894"
NY = ZoneInfo("America/New_York")
TP_RATIO = 0.5          # 買回價 ≤ 50% credit 就止賺
EXPIRY_CLOSE_ET = (15, 30)   # 到期日呢個時間平倉
ENTRY_ET = (15, 50)          # 週五呢個時間開新倉


def log(msg):
    print(f"[{datetime.now(NY).strftime('%Y-%m-%d %H:%M:%S')} ET] {msg}", flush=True)


def api_get(path):
    with urllib.request.urlopen(API + path, timeout=30) as r:
        return json.loads(r.read())


def api_post(path, payload=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def in_session(now):
    return now.weekday() < 5 and (now.hour, now.minute) >= (9, 30) and (now.hour, now.minute) <= (16, 5)


def leg_quotes(codes):
    """回 {code: ask}，用嚟計買回成本。"""
    from futu import OpenQuoteContext, RET_OK
    out = {}
    with OpenQuoteContext(host="127.0.0.1", port=11111) as ctx:
        ret, data = ctx.get_market_snapshot(codes)
        if ret == RET_OK:
            for _, r in data.iterrows():
                out[r["code"]] = float(r["ask_price"] or 0.0)
    return out


def spy_spot():
    from futu import OpenQuoteContext, RET_OK
    with OpenQuoteContext(host="127.0.0.1", port=11111) as ctx:
        ret, data = ctx.get_market_snapshot(["US.SPY"])
        if ret == RET_OK:
            return float(data.iloc[0]["last_price"])
    return None


def check_once():
    pos_view = api_get("/positions")
    now = datetime.now(NY)
    positions = pos_view.get("positions", [])

    if positions:
        pos = positions[0]
        spot = spy_spot()
        if spot is None:
            log("攞唔到 SPY 即市價，跳過本輪")
            return
        legs = [l["code"] for l in pos["legs"]]
        asks = leg_quotes(legs)
        buyback_pts = round(sum(asks.get(c, 0.0) for c in legs), 2)

        # 1) 到期平倉
        if str(now.date()) == pos["expiry"] and (now.hour, now.minute) >= EXPIRY_CLOSE_ET:
            log(f"到期日 {pos['expiry']} ET15:30 → 自動平倉 {pos['id']}")
            r = api_post("/close", {"id": pos["id"], "reason": "expiry"})
            log(f"平倉結果: {json.dumps(r, ensure_ascii=False)[:300]}")
            return
        # 2) 止蝕（SPY 升穿 call 或 跌穿 put）
        if spot >= pos["k_call"] or spot <= pos["k_put"]:
            side = "升穿 Call" if spot >= pos["k_call"] else "跌穿 Put"
            log(f"⚠️ 止蝕觸發：SPY {spot:.2f} {side}（{pos['k_put']}/{pos['k_call']}）→ 平倉 {pos['id']}")
            r = api_post("/close", {"id": pos["id"], "reason": "stop_loss"})
            log(f"平倉結果: {json.dumps(r, ensure_ascii=False)[:300]}")
            return
        # 3) 止賺（買回成本 ≤ 50% credit）
        if buyback_pts <= TP_RATIO * pos["credit_pts"] and buyback_pts >= 0:
            log(f"✅ 止賺觸發：買回 {buyback_pts:.2f} ≤ {TP_RATIO*pos['credit_pts']:.2f}（50% × credit {pos['credit_pts']}）→ 平倉 {pos['id']}")
            r = api_post("/close", {"id": pos["id"], "reason": "take_profit"})
            log(f"平倉結果: {json.dumps(r, ensure_ascii=False)[:300]}")
            return
        return

    # 冇倉：週五收市前自動開新倉
    if now.weekday() == 4 and (now.hour, now.minute) >= ENTRY_ET:
        log("週五 ET15:50 冇持倉 → 自動開新倉")
        r = api_post("/open", {"source": "monitor_friday"})
        log(f"開倉結果: {json.dumps(r, ensure_ascii=False)[:400]}")


def main():
    log("us-condor-monitor 啟動（只喺美股時段做嘢：ET 09:30–16:05 週一至五）")
    while True:
        now = datetime.now(NY)
        if not in_session(now):
            time.sleep(300)
            continue
        try:
            check_once()
        except Exception as e:
            log(f"ERROR: {type(e).__name__}: {e}")
        time.sleep(60)


if __name__ == "__main__":
    main()
