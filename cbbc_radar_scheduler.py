#!/usr/bin/env python3
"""
牛熊波幅雷達排程 — 每個交易日 08:00 (HKT) 生成 dataset 並推上 gsmart-box

  08:00  跑 cbbc_radar_builder.py（當日 08:00 結算牛熊數據判當日「後」方向；「先」方向用前一晚夜期+ADR）
         —— 判定每日只做呢一次，其他時間唔重新判（2026-09-09 用戶規則）
  08:45 / 08:55  純重推（--push-only，只重貼同一份 dataset，唔重算唔重新判定），防 Manus 08:40 舊數據覆蓋
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
# Manus 每日 08:40 會用舊 pipeline 嘅數據覆蓋我哋 08:00 推上 gsmart-box 嘅版本
# （2026-09-10 實測：08:05 我哋推 → 08:40:45 Manus 覆蓋 → 08:45 我哋奪回），
# 所以純重推兩次（--push-only：只重貼同一份 dataset，唔重算 premarket、唔重新判定）。
# 08:45 奪回一次；08:55 再兜底一次，防 Manus push 遲到／重試。
# 判定每日只做一次（08:00）—— 2026-09-09 用戶規則：其他時間唔再重新判定。
REPUSH_ATS = ((8, 45), (8, 55))


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
            capture_output=True, text=True, timeout=180,
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


CATCHUP_LIMIT = (16, 0)


def catchup_if_missed():
    """主機喺 08:00–09:10 窗口之後先開機／重啟時，補跑今日 build 一次。
    16:00 後唔補（盤前判定太遲無意義），交返聽日。非交易日 builder 自己 SKIP。"""
    now = datetime.now(HKT)
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    day = state.setdefault(today, {"done": False, "attempts": 0})
    due = now.replace(hour=RUN_AT[0], minute=RUN_AT[1], second=0, microsecond=0)
    limit = now.replace(hour=CATCHUP_LIMIT[0], minute=CATCHUP_LIMIT[1], second=0, microsecond=0)
    if day["done"] or now < due or now > limit:
        return
    log("▶ 啟動 catch-up：今日 08:00 build 未跑，而家補跑一次（當日唯一判定）")
    day["attempts"] += 1
    day["last_try"] = now.isoformat()
    if run_builder():
        day["done"] = True
        log("✓ catch-up 完成")
    else:
        log("✗ catch-up 失敗")
    save_state(state)


def main():
    log("牛熊雷達排程啟動 — 每個交易日 08:00 HKT")
    catchup_if_missed()
    while True:
        now = datetime.now(HKT)
        today = now.strftime("%Y-%m-%d")
        state = load_state()
        day = state.setdefault(today, {"done": False, "attempts": 0})
        if day.get("repushed") and "repush_count" not in day:
            day["repush_count"] = 1  # 舊 state 遷移（2026-09-10 前只重推一次）

        due = now.replace(hour=RUN_AT[0], minute=RUN_AT[1], second=0, microsecond=0)
        last = now.replace(hour=LAST_RETRY[0], minute=LAST_RETRY[1], second=0, microsecond=0)
        # 純重推（Manus 08:40 覆蓋後奪回控制權）；前提是今日已成功 build。
        # 08:45 奪回 + 08:55 兜底，兩次都只重貼同一份 dataset，唔重新判定。
        if day["done"] and now <= last:
            done_n = int(day.get("repush_count", 0))
            pending = [
                (i, hh, mm)
                for i, (hh, mm) in enumerate(REPUSH_ATS)
                if i >= done_n
                and now >= now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            ]
            if pending:
                i, hh, mm = pending[0]
                log(f"▶ {hh:02d}:{mm:02d} 純重推 dataset（防 Manus 覆蓋，唔重新判定）")
                if run_repush():
                    day["repush_count"] = i + 1
                    log("✓ 重推完成")
                else:
                    log("✗ 重推失敗（下輪再試）")
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
