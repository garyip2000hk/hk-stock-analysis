#!/usr/bin/env python3
"""
批量上傳量化日報策略 Markdown 到 Google Drive 量化日報資料夾。
使用 Zo /zo/ask API 並行上傳（每批 5 個）。
"""
import os, sys, json, time, asyncio, aiohttp

API_URL = "https://api.zo.computer/zo/ask"
TOKEN = os.environ.get("ZO_CLIENT_IDENTITY_TOKEN", "")
MODEL = "byok:7494fd95-55d9-4af3-b40c-88ad720c5043"
LOCAL_DIR = "/home/workspace/Desktop/Garysir/量化日報"
FOLDER_ID = "18Ad0HCklngwraOT33J4sxZI0LJh9JBjD"
STATE_FILE = os.path.join(LOCAL_DIR, "gdrive_upload_state.json")
BATCH = 5


def load_state():
    if os.path.exists(STATE_FILE):
        return set(json.load(open(STATE_FILE)))
    return set()


def save_state(done):
    json.dump(sorted(done), open(STATE_FILE, "w"), ensure_ascii=False, indent=0)


async def upload_one(session, sem, filename, content):
    """用 Zo ask API 上傳一個檔案到 Google Drive"""
    async with sem:
        prompt = (
            "Use the use_app_google_drive tool to upload a file to Google Drive.\n"
            f"Tool: google_drive-upload-file\n"
            f"configured_props: {{\n"
            f'  "name": "{filename}",\n'
            f'  "content": """{content}""",\n'
            f'  "parentId": "{FOLDER_ID}",\n'
            f'  "mimeType": "text/markdown"\n'
            f"}}\n"
            "Just run the tool call and report success or failure. Do not explain."
        )
        body = {
            "input": prompt,
            "model_name": MODEL,
            "output_format": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "fileId": {"type": "string"},
                    "error": {"type": "string"}
                },
                "required": ["success"]
            }
        }
        headers = {
            "authorization": TOKEN,
            "content-type": "application/json"
        }
        for attempt in range(3):
            try:
                async with session.post(API_URL, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    result = await resp.json()
                    output = result.get("output", {})
                    if isinstance(output, dict) and output.get("success"):
                        return filename, True, output.get("fileId", "")
                    return filename, False, str(output)
            except Exception as e:
                if attempt == 2:
                    return filename, False, str(e)
                await asyncio.sleep(2 ** attempt)
        return filename, False, "max retries"


async def main():
    if not TOKEN:
        print("ERROR: ZO_CLIENT_IDENTITY_TOKEN not set")
        sys.exit(1)

    done = load_state()
    all_files = sorted([f for f in os.listdir(LOCAL_DIR) if f.endswith(".md")])
    todo = [f for f in all_files if f not in done]
    print(f"Total: {len(all_files)}, Already uploaded: {len(done)}, To upload: {len(todo)}")

    if not todo:
        print("All files already uploaded!")
        return

    sem = asyncio.Semaphore(BATCH)
    uploaded = 0
    failed = 0

    async with aiohttp.ClientSession() as session:
        # Process in chunks of 50 to save state periodically
        for chunk_start in range(0, len(todo), 50):
            chunk = todo[chunk_start:chunk_start + 50]
            tasks = []
            for f in chunk:
                filepath = os.path.join(LOCAL_DIR, f)
                content = open(filepath, "r", encoding="utf-8").read()
                tasks.append(upload_one(session, sem, f, content))

            results = await asyncio.gather(*tasks)
            for filename, success, info in results:
                if success:
                    done.add(filename)
                    uploaded += 1
                    print(f"  ✓ {filename}")
                else:
                    failed += 1
                    print(f"  ✗ {filename}: {info[:80]}")

            save_state(done)
            print(f"Progress: {uploaded} uploaded, {failed} failed, {len(done)}/{len(all_files)} total done")

            if chunk_start + 50 < len(todo):
                await asyncio.sleep(1)

    print(f"\nDone! Uploaded: {uploaded}, Failed: {failed}, Total on Drive: {len(done)}")


if __name__ == "__main__":
    asyncio.run(main())
