"""
Futu 取數健康檢查 — 每日 08:00 HKT（取數窗口 01:00–07:30 之後）由 scheduled agent 跑。

做三件事：
  1. 睇 OpenD 生死（socket 127.0.0.1:11111）
  2. OpenD 死 → 自動 supervisorctl restart（裝置已受信任，唔需要短訊驗證碼）
     每日最多重試 MAX_RESTARTS 次，避免撞富途「驗證碼過於頻繁」鎖
  3. 核對今日數據有冇真正落地（K線 + 快照），只喺交易日要求

輸出：最後一行係 machine-readable 判決
    HEALTH_OK: <一句總結>          → 冇事，唔需要通知
    HEALTH_ALERT: <一句總結>       → 有事，要通知
    HEALTH_FIXED: <一句總結>       → 曾經死過但已自動救返，值得知
"""
import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HKT = timezone(timedelta(hours=8))
BASE = Path("/home/workspace/stock-analysis")
KLINE = Path("/home/workspace/Desktop/db/Futu/Kline/kline_day.parquet")
SNAP_DIR = Path("/home/workspace/Desktop/db/Futu/Snapshot")
STATE_PATH = BASE / "futu_health_state.json"
SUPERVISOR_CONF = "/etc/zo/supervisord-user.conf"
PY = "/usr/local/bin/python3"

MAX_RESTARTS = 3
notes = []


def log(msg):
    print(f"[{datetime.now(HKT):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def opend_alive(timeout=5):
    try:
        with socket.create_connection(("127.0.0.1", 11111), timeout=timeout):
            return True
    except Exception:
        return False


def restart_opend():
    try:
        proc = subprocess.run(
            ["supervisorctl", "-c", SUPERVISOR_CONF, "restart", "futu-opend"],
            capture_output=True, text=True, timeout=120,
        )
        log(f"supervisorctl restart → {proc.stdout.strip() or proc.stderr.strip()}")
    except Exception as e:
        log(f"supervisorctl restart 失敗: {e}")
        return False
    for _ in range(12):
        time.sleep(5)
        if opend_alive():
            return True
    return False


def login_ok_in_log():
    logs = sorted(Path("/root/.com.futunn.FutuOpenD/Log").glob("GTWLog_0_*.log"))
    if not logs:
        return None
    txt = logs[-1].read_text(errors="ignore")
    if "登录成功" in txt:
        return True
    for bad in ("频繁", "頻繁", "登录失败", "验证"):
        if bad in txt:
            return False
    return None


def trading_day_info():
    """回傳 (今日係唔係交易日, 最近一個已收市交易日 date)。OpenD 要生。"""
    code = (
        "import json,datetime\n"
        "from futu import OpenQuoteContext\n"
        "q=OpenQuoteContext(host='127.0.0.1',port=11111)\n"
        "today=datetime.date.today()\n"
        "start=(today-datetime.timedelta(days=20)).isoformat()\n"
        "r,d=q.request_trading_days(market='HK',start=start,end=today.isoformat())\n"
        "q.close()\n"
        "days=[x['time'] for x in d] if r==0 else []\n"
        "print('TRADEDAYS='+json.dumps(days))\n"
    )
    try:
        proc = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=120)
        line = [l for l in proc.stdout.splitlines() if l.startswith("TRADEDAYS=")]
        if not line:
            return None, None
        days = json.loads(line[0].split("=", 1)[1])
        if not days:
            return None, None
        today = datetime.now(HKT).strftime("%Y-%m-%d")
        is_today = today in days
        closed = [d for d in days if d < today] or days
        return is_today, closed[-1]
    except Exception as e:
        log(f"取交易日失敗: {e}")
        return None, None


def kline_latest():
    code = (
        "import pandas as pd\n"
        f"df=pd.read_parquet(r'{KLINE}')\n"
        "print('KLINE='+str(df['time_key'].max())[:10]+'|'+str(len(df))+'|'+str(df['code'].nunique()))\n"
    )
    try:
        proc = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=180)
        line = [l for l in proc.stdout.splitlines() if l.startswith("KLINE=")]
        if not line:
            return None, None, None
        d, rows, stocks = line[0].split("=", 1)[1].split("|")
        return d, int(rows), int(stocks)
    except Exception as e:
        log(f"讀 K 線失敗: {e}")
        return None, None, None


def main():
    now = datetime.now(HKT)
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    day = state.setdefault(today, {"restarts": 0})

    alive = opend_alive()
    was_dead = not alive
    log(f"OpenD: {'生' if alive else '死'}")

    if not alive:
        if day["restarts"] >= MAX_RESTARTS:
            save_state(state)
            print(f"HEALTH_ALERT: OpenD 死咗，今日已自動重開 {day['restarts']} 次都唔得，"
                  f"要人手查（可能富途帳號被鎖，唔好再試，等一日）")
            return
        day["restarts"] += 1
        save_state(state)
        log(f"嘗試自動重開 OpenD（今日第 {day['restarts']} 次）")
        alive = restart_opend()
        lg = login_ok_in_log()
        if not alive:
            reason = "登入被拒（可能驗證碼過於頻繁被鎖）" if lg is False else "起唔到"
            print(f"HEALTH_ALERT: OpenD 死咗，自動重開失敗 — {reason}。今日數據會漏，要人手查。")
            return
        notes.append(f"OpenD 曾經死咗，已自動重開成功（今日第 {day['restarts']} 次）")

    is_trading_today, last_closed = trading_day_info()
    if is_trading_today is None:
        print("HEALTH_ALERT: OpenD 生，但問唔到富途交易日曆（API 可能未登入好），要人手查。")
        return

    kdate, krows, kstocks = kline_latest()
    if kdate is None:
        print("HEALTH_ALERT: 讀唔到 K 線檔案 kline_day.parquet，要人手查。")
        return

    problems = []
    # K 線應該追到最近一個已收市交易日
    if kdate < last_closed:
        problems.append(f"K 線落後：最新 {kdate}，應該有 {last_closed}")

    # 快照：只喺交易日要求今日有檔
    snap_today = SNAP_DIR / f"snapshot_{now:%Y%m%d}.parquet"
    if is_trading_today and not snap_today.exists():
        problems.append(f"今日快照未落地（搵唔到 {snap_today.name}）")

    summary = f"K線 {kdate}（{krows:,} 行 / {kstocks} 隻）"
    if is_trading_today:
        summary += f"、快照 {'✓' if snap_today.exists() else '✗'}"
    else:
        summary += "、今日非交易日"

    if problems:
        print(f"HEALTH_ALERT: {'；'.join(problems)}。（{summary}）")
    elif notes:
        print(f"HEALTH_FIXED: {'；'.join(notes)}。數據正常：{summary}")
    else:
        print(f"HEALTH_OK: {summary}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"HEALTH_ALERT: 健康檢查本身出錯 — {type(e).__name__}: {e}")
        sys.exit(1)
