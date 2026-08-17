"""
Futu 取數窗口排程 — 每日 01:00 至 07:30 (HKT)

  01:00  K線增量更新（前一交易日收市）
  01:30  Market Snapshot
  失敗每 30 分鐘重試，07:30 收工（唔撞 daily_pipeline）

以 process 模式常駐，唔燒 AI 額度。
狀態寫入 futu_scheduler_state.json，log 去 /dev/shm/futu-scheduler.log
"""
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HKT = timezone(timedelta(hours=8))
BASE = Path("/home/workspace/stock-analysis")
IMPORTER = BASE / "futu_data_importer.py"
STATE_PATH = BASE / "futu_scheduler_state.json"

WINDOW_START = (1, 0)
WINDOW_END = (7, 30)

SUPERVISOR_CONF = "/etc/zo/supervisord-user.conf"
MAX_REVIVES = 3

TASKS = [
    {"name": "kline", "at": (1, 0), "args": ["--kline-only"]},
    {"name": "snapshot", "at": (1, 30), "args": ["--snapshot-only"]},
]

RETRY_MINUTES = 30


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


def minutes_of(hm):
    return hm[0] * 60 + hm[1]


def opend_alive():
    try:
        import socket

        with socket.create_connection(("127.0.0.1", 11111), timeout=5):
            return True
    except Exception:
        return False


def try_revive_opend(state):
    """窗口內 OpenD 死咗 → 經 supervisor 重開。

    唔會自己走登入流程（帳密在 FutuOpenD.xml，裝置已受信任），
    每日最多 MAX_REVIVES 次，避免撞富途「驗證碼過於頻繁」鎖。
    """
    today = datetime.now(HKT).strftime("%Y-%m-%d")
    revives = state.setdefault("_revives", {})
    used = revives.get(today, 0)
    if used >= MAX_REVIVES:
        log(f"✗ OpenD 死咗，今日已重開 {used} 次（上限 {MAX_REVIVES}），唔再試")
        return False
    revives[today] = used + 1
    for key in sorted(revives)[:-7]:
        revives.pop(key, None)
    save_state(state)
    log(f"⟳ OpenD 死咗，嘗試經 supervisor 重開（今日第 {used + 1} 次）")
    try:
        proc = subprocess.run(
            ["supervisorctl", "-c", SUPERVISOR_CONF, "restart", "futu-opend"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (proc.stdout or proc.stderr).strip().splitlines()
        log(f"    supervisorctl → {out[-1] if out else '(no output)'}")
    except Exception as e:
        log(f"✗ supervisorctl restart 失敗: {e}")
        return False
    for _ in range(12):
        time.sleep(5)
        if opend_alive():
            log("✓ OpenD 已重開成功")
            return True
    log("✗ OpenD 重開後 60 秒內仍然冇響應")
    return False


def run_task(task, state=None):
    log(f"▶ 開始 {task['name']}")
    if not opend_alive():
        if state is None or not try_revive_opend(state):
            log(f"✗ {task['name']} 跳過：OpenD (127.0.0.1:11111) 冇響應")
            return False
    try:
        proc = subprocess.run(
            ["/usr/local/bin/python3", str(IMPORTER), *task["args"]],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except subprocess.TimeoutExpired:
        log(f"✗ {task['name']} 超時 (60 分鐘)")
        return False
    tail = [l for l in proc.stdout.strip().splitlines() if l.strip()][-6:]
    for line in tail:
        log(f"    {line}")
    if proc.returncode != 0:
        err = proc.stderr.strip().splitlines()[-3:]
        for line in err:
            log(f"    ERR {line}")
        log(f"✗ {task['name']} 失敗 (returncode={proc.returncode})")
        return False
    log(f"✓ {task['name']} 完成")
    return True


def main():
    log("Futu 取數排程啟動 — 窗口 01:00–07:30 HKT")
    state = load_state()

    while True:
        now = datetime.now(HKT)
        today = now.strftime("%Y-%m-%d")
        now_min = now.hour * 60 + now.minute

        day_state = state.setdefault(today, {})

        if minutes_of(WINDOW_START) <= now_min < minutes_of(WINDOW_END):
            for task in TASKS:
                name = task["name"]
                info = day_state.setdefault(name, {"done": False, "attempts": 0, "last_try": None})
                if info["done"]:
                    continue
                if now_min < minutes_of(task["at"]):
                    continue
                if info["last_try"]:
                    last = datetime.fromisoformat(info["last_try"])
                    if (now - last) < timedelta(minutes=RETRY_MINUTES):
                        continue
                info["attempts"] += 1
                info["last_try"] = now.isoformat()
                save_state(state)
                ok = run_task(task, state)
                info["done"] = ok
                save_state(state)

        # 只保留最近 14 日狀態（_revives 唔算日期 key，要留住）
        day_keys = [k for k in state if not k.startswith("_")]
        if len(day_keys) > 14:
            for key in sorted(day_keys)[:-14]:
                state.pop(key, None)
            save_state(state)

        time.sleep(300)


if __name__ == "__main__":
    main()
