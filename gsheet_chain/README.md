# HKEX 期權鏈日報 → Google Sheet（Apps Script）

每日收市期權鏈由 Zo automation `agt_2d1d3400`（21:00 HKT）上傳 CSV 去 Google Drive
資料夾「HKEX 期權鏈日報」（id `15WEwSFqEaQdAzFH3Gq4wjJHqc-Dt8rVR`）。
本目錄嘅 `Code.gs` 係一段 Apps Script，貼入下面個 Sheet 嘅 script editor 之後，
每小時自動檢查資料夾、把 CSV 入表（冪等，唔會重複）。

- Sheet：`HKEX 期權鏈日報（每日自動更新）`
  id `1p8I9uljTfRnFhljm7X7dNDU4JyTSIPkNlZ4qTP9XjvE`（喺同一個 Drive 資料夾內）
- 工作表：`每日總覽`（append-only 歷史，每日 148 行）、`期權鏈_當日`（最新一日，約 6,700 行，每日覆蓋）、`說明`
- 全鏈 CSV（約 42,000 行／日）**唔入 Sheet**（會撞 Google Sheet 1,000 萬格上限），只留喺 Drive 資料夾。

## 安裝（一次性，約 3 分鐘）

1. 開 Sheet → 功能表「擴充功能」→「Apps Script」
2. 刪除編輯器入面預設嘅 `function myFunction() {}`，將 `Code.gs` 全文貼入去，撳「儲存」（💾）
3. 上面個函式下拉選 `setup` → 撳「執行」→ 出現授權畫面就揀你個 Google 帳戶
   →「進階」→「前往 HKEX 期權鏈日報（未經驗證）」→「允許」
4. 等佢跑完（第一次會回填所有歷史，約 1–3 分鐘）。「說明」工作表會顯示最後更新時間。

之後每小時自動跑一次；冇新數據就即刻結束，唔會影響配額。

## 驗證

已用 node stub harness（`/tmp/gas_stub.js`）對真實 CSV 跑過 `setup()` + `dailyUpdate()`：
11 個交易日 × 148 行 = 1,628 行總覽、6,680 行當日鏈，第二次執行冇新增（IDEMPOTENT ✅），
股票代號保留前導 0（`09988`）、數值欄真係 number（可排序篩選）、日期欄保持文字。

## 注意

- Apps Script 用你（Gary）嘅 Google 帳戶權限讀 Drive 資料夾；朋友只係 Sheet 嘅讀者，掂唔到資料夾。
- 改過 `Code.gs` 要重新貼入 script editor 先生效（script editor 唔會自動同步 workspace 檔案）。
