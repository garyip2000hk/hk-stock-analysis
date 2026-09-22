#!/usr/local/bin/python3
"""IB Gateway watchdog: monitors port 4001 + login state, notifies via Telegram
when the gateway disconnects or is stuck at 2FA. Never restarts the gateway
(restarts trigger 2FA push loops; the user decides when to reconnect)."""
import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

HOST = "127.0.0.1"
PORT = int(os.environ.get("IB_WATCH_PORT", "4001"))
CHECK_INTERVAL = 600          # 10 min
RENOTIFY_HOURS = 3            # remind again if still down
LOG_DIR = Path("/root/ibc/logs")
STATE_FILE = Path(__file__).parent / "ib_watchdog_state.json"
ZO_API = "https://api.zo.computer/zo/ask"
MODEL = "byok:b3c03552-cb9a-4add-8229-2a9d12801693"
DRY_RUN = os.environ.get("IB_WATCH_DRY_RUN") == "1"

HKT = timezone(timedelta(hours=8))


def now_hkt():
    return datetime.now(HKT)


def log(msg):
    print(f"[{now_hkt().strftime('%Y-%m-%d %H:%M:%S')} HKT] {msg}", flush=True)


def port_open():
    s = socket.socket()
    s.settimeout(5)
    try:
        s.connect((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def gateway_process_alive():
    r = subprocess.run(["pgrep", "-f", "ibcalpha.ibc.IbcGateway"],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def recent_2fa_activity(minutes=30):
    """True if IBC started a 2FA prompt within the last N minutes."""
    try:
        latest = max(LOG_DIR.glob("ibc-*.txt"), key=lambda p: p.stat().st_mtime)
    except ValueError:
        return False
    cutoff = time.time() - minutes * 60
    if latest.stat().st_mtime < cutoff:
        return False
    tail = subprocess.run(["tail", "-30", str(latest)],
                          capture_output=True, text=True).stdout
    return "Second Factor Authentication initiated" in tail


def classify():
    if not gateway_process_alive():
        return ("process_dead",
                "🔴 IB Gateway 斷咗線：個 process 死咗。\n"
                "我唔會自動重啟（會觸發 2FA push）。你覆我一聲，我會重啟並通知你去做 2FA。")
    if recent_2fa_activity():
        return ("waiting_2fa",
                "⚠️ IB Gateway 卡咗喺 2FA：而家有個批准通知喺你部手機等緊。\n"
                "請喺 IBKR Mobile 撳批准，批准完覆我一聲。")
    return ("login_stuck",
            "🔴 IB Gateway 斷咗線：登入卡住咗（冇 2FA 彈窗）。\n"
            "你覆我一聲，我會重新啟動登入並通知你去做 2FA。")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"healthy": True, "episode_start": None,
                "last_notified_ts": 0, "notified_count": 0}


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, indent=1))


def send_telegram(text):
    if DRY_RUN:
        log(f"DRY_RUN would send:\n{text}")
        return True
    token = os.environ.get("ZO_CLIENT_IDENTITY_TOKEN", "")
    if not token:
        log("ERROR: ZO_CLIENT_IDENTITY_TOKEN missing")
        return False
    prompt = (
        "請用 send_telegram_message 工具發送以下訊息給用戶，"
        "發送後只需回覆『已發送』，唔好做任何其他事、唔好重啟任何服務：\n\n" + text
    )
    try:
        r = requests.post(ZO_API, timeout=180,
                          headers={"authorization": token,
                                   "content-type": "application/json"},
                          json={"input": prompt, "model_name": MODEL})
        ok = r.status_code == 200
        if not ok:
            log(f"zo/ask failed: {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        log(f"zo/ask exception: {e}")
        return False


def main():
    log(f"ib_watchdog started: watching {HOST}:{PORT} every {CHECK_INTERVAL}s")
    while True:
        st = load_state()
        healthy = port_open()
        if healthy:
            if not st["healthy"] and st.get("notified_count"):
                send_telegram(
                    "✅ IB Gateway 已恢復連線（port 4001 正常），數據管道恢復運作。")
            st.update({"healthy": True, "episode_start": None,
                       "last_notified_ts": 0, "notified_count": 0})
            save_state(st)
        else:
            kind, msg = classify()
            if st["healthy"]:
                st.update({"healthy": False,
                           "episode_start": now_hkt().isoformat()})
                st["notified_count"] = 0
            down_since = st.get("episode_start", "?")
            hours_down = (now_hkt() - datetime.fromisoformat(down_since)).total_seconds() / 3600 if down_since != "?" else 0
            if st["notified_count"] == 0 or \
               (time.time() - st["last_notified_ts"]) > RENOTIFY_HOURS * 3600:
                full = msg + f"\n（已斷線 {hours_down:.1f} 小時）"
                if send_telegram(full):
                    st["last_notified_ts"] = time.time()
                    st["notified_count"] += 1
                log(f"down: {kind} (episode {down_since}, notified x{st['notified_count']})")
            else:
                log(f"still down: {kind}, next reminder in "
                    f"{RENOTIFY_HOURS - (time.time() - st['last_notified_ts'])/3600:.1f}h")
            save_state(st)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
