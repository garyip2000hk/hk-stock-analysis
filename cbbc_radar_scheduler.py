#!/usr/bin/env python3
"""
牛熊波幅雷達排程 — 每個交易日 08:00 (HKT) 生成 dataset 並推上 gsmart-box

  08:00  跑 cbbc_radar_builder.py（當日 08:00 結算牛熊數據判當日「後」方向；「先」方向用前一晚夜期+ADR）
  失敗 08:40 / 09:10 重試兩次，之後收工等聽朝
  狀態存 cbbc_radar_scheduler_state.json；log 去 /dev/shm/cbbc-radar-scheduler.log
  唔燒 AI 額度。牛熊證街貨喺朝早 09:00 前後先由發行商更新，
  所以夜晚跑已經係完整一日數據。
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HKT = ZoneInfo("Asia/Hong_Kong")
BASE = Path("/home/workspace/stock-analysis")
BUILDER = BASE / "cbbc_radar_builder.py"
STATE_PATH = BASE / "cbbc_radar_scheduler_state.json"
PY = "/usr/local/bin/python3"

RUN_AT = (8, 0)
RETRY_EVERY_MIN = 30
LAST_RETRY = (9, 10)
TIMEOUT_SEC = 15 * 60
# Manus 每日 08:40 會用舊 pipeline 嘅數據覆蓋我哋 08:00 推上 gsmart-box 嘅版本，
# 所以 08:45 要原檔重推一次（唔重建），確保用戶見到嘅係 Zo 版（新日期語義 + 開市前預判）。
REPUSH_AT = (8, 45)


def log(msg):
    print(f"[{datetime.now(HKT):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def run_repush():
    try:
        proc = subprocess.run(
            [PY, str(BUILDER), "--push-only"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        log("✗ 重推超時")
        return False
    out = (proc.stdout or "").strip()
    for line in [l for l in out.splitlines() if l.strip()][-4:]:
        log(f"    {line}")
    return proc.returncode == 0 and "PUSH_OK" in out


def run_builder():
    try:
        proc = subprocess.run(
            [PY, str(BUILDER)],
            capture_output=True, text=True, timeout=TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        log("✗ builder 超時 (15 分鐘)")
        return False
    out = (proc.stdout or "").strip()
    for line in [l for l in out.splitlines() if l.strip()][-8:]:
        log(f"    {line}")
    if proc.returncode == 0 and ("BUILD_OK" in out or "PUSH_OK" in out or "PUSH_SKIP" in out):
        return True
    if proc.returncode == 0 and "SKIP" in out:
        return True  # 非交易日，正常跳過
    log(f"✗ builder 失敗 (returncode={proc.returncode})")
    if proc.stderr:
        for line in proc.stderr.strip().splitlines()[-3:]:
            log(f"    stderr: {line}")
    return False


def main():
    log("牛熊雷達排程啟動 — 每個交易日 08:00 HKT")
    while True:
        now = datetime.now(HKT)
        today = now.strftime("%Y-%m-%d")
        state = load_state()
        day = state.setdefault(today, {"done": False, "attempts": 0})

        due = now.replace(hour=RUN_AT[0], minute=RUN_AT[1], second=0, microsecond=0)
        last = now.replace(hour=LAST_RETRY[0], minute=LAST_RETRY[1], second=0, microsecond=0)
        repush_due = now.replace(hour=REPUSH_AT[0], minute=REPUSH_AT[1], second=0, microsecond=0)

        # 08:45 重推（Manus 08:40 覆蓋後奪回控制權）；前提是今日已成功 build
        if day["done"] and not day.get("repushed") and now >= repush_due and now <= last:
            log("▶ 08:45 重推 dataset（防 Manus 覆蓋）")
            day["repushed"] = run_repush()
            log("✓ 重推完成" if day["repushed"] else "✗ 重推失敗（下輪再試）")
            save_state(state)
            time.sleep(300)
            continue

        if day["done"] or now < due or now > last:
            # 清舊 state（留 14 日）
            for key in sorted(state)[:-14]:
                state.pop(key, None)
            save_state(state)
            time.sleep(300)
            continue

        log("▶ 開始生成牛熊雷達 dataset")
        day["attempts"] += 1
        day["last_try"] = now.isoformat()
        ok = run_builder()
        if ok:
            day["done"] = True
            log("✓ 完成")
        else:
            log(f"✗ 失敗，{RETRY_EVERY_MIN} 分鐘後重試" if now < last else "✗ 失敗，今日收工")
        save_state(state)
        time.sleep(RETRY_EVERY_MIN * 60 if not ok else 300)


if __name__ == "__main__":
    main()
