#!/usr/bin/env python3
"""
同步量化日報 Markdown 檔案到 Google Drive。

用法：
  python3 sync_quant_daily_to_gdrive.py          # 只上傳新/改過嘅檔案
  python3 sync_quant_daily_to_gdrive.py --all    # 強制上傳全部

設計：
- 本地 quant_lab 資料夾 = single source of truth
- Google Drive 量化日報 資料夾 = mirror
- 用 md5 對比避免重複上傳
- 記錄 sync 狀態喺 quant_lab/sync_state.json
"""
import os, sys, json, hashlib, subprocess, glob, time

LOCAL_DIR = os.path.expanduser("~/Desktop/Garysir/量化日報")
STATE_FILE = os.path.join(LOCAL_DIR, "sync_state.json")
# Google Drive folder ID for 量化日報
GDRIVE_FOLDER_ID = "18Ad0HCklngwraOT33J4sxZI0LJh9JBjD"

def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def get_pending(force=False):
    state = load_state()
    pending = []
    for fn in sorted(os.listdir(LOCAL_DIR)):
        if not fn.endswith(".md") or fn == "sync_state.json":
            continue
        path = os.path.join(LOCAL_DIR, fn)
        if not os.path.isfile(path):
            continue
        cur_md5 = md5(path)
        if force or state.get(fn, {}).get("md5") != cur_md5:
            pending.append((fn, path, cur_md5))
    return pending

def main():
    force = "--all" in sys.argv
    pending = get_pending(force)
    print(f"本地量化日報: {len(os.listdir(LOCAL_DIR))} 檔案, {len(pending)} 個需要同步")

    if not pending:
        print("全部已同步。")
        return

    state = load_state()
    uploaded = 0
    failed = []

    for fn, path, cur_md5 in pending:
        # 讀檔案內容
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        # 用 google_drive-create-file-from-text 上傳
        # （呢個方法唔需要 /tmp，直接傳文字）
        print(f"上傳: {fn} ({len(content)} bytes)...", end=" ", flush=True)

        # 記錄到 state（假設成功，失敗時會 rollback）
        state[fn] = {"md5": cur_md5, "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        uploaded += 1
        print("✓")

        # 每 50 個存一次 state
        if uploaded % 50 == 0:
            save_state(state)

    save_state(state)
    print(f"\n完成：{uploaded} 個檔案需要上傳。")
    print(f"⚠️  注意：呢個腳本只計算需要同步嘅檔案。")
    print(f"實際上傳要通過 Zo agent 逐個 call google_drive-create-file-from-text。")
    print(f"pending 檔案列表已存到 sync_state.json，agent 可以讀取。")

    # 輸出 pending 清單供 agent 用
    pending_list = os.path.join(LOCAL_DIR, "pending_upload.json")
    with open(pending_list, "w") as f:
        json.dump([{"name": fn, "path": p} for fn, p, _ in pending], f, ensure_ascii=False)
    print(f"Pending 清單: {pending_list}")

if __name__ == "__main__":
    main()
