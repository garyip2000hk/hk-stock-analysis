# stock-analysis — 財技股分析系統

## ⚠️ CCASS 數據最重要嘅一件事

`holdings.parquet` / `parthold.parquet` / `incremental/holdings_*.parquet`
**係「變動紀錄」(change log)，唔係每日全體持倉快照。**

一個參與者只會喺佢**餘額有變動嘅日子**出現一行。所以：

- 直接 `WHERE at_date = '2026-07-31'` 拎到嘅係「當日有買賣嘅人」，
  **唔係「當日持股嘅人」**。
- 例：01241 喺 2026-07-31 只有 **2 行**變動，但實際有 **105 個**持倉參與者。

正確做法（`ccass_snapshot.py` 已實現）：對每個 `part_id` 取
`at_date <= 目標日` 嘅**最後一筆餘額**（forward-fill），`holding = 0` 當作真正撤倉。

## 曾經出現過嘅三個 bug（已修，勿重犯）

1. **當變動紀錄係快照** → 只見 2-3 個持倉人，
   頭5大／頭10大／頭20大全部變 100%。
2. **用「當日可見行數之和」做分母** → 分母錯，
   所以先會出現「3 個 CHINA ALUMCAN 加起來 = 100%」。
   正確分母係**已發行股數** (`issued_shares.parquet`)。
3. **日期對唔齊** → 之前用「該股最後有變動嘅日子」做快照日
   (例 08152 = 07-29、00374 = 07-15)，
   結果永遠對唔上 `dailylog` 嘅 07-31，白白放棄權威數字。
   現在一律對齊 `market_latest_date()`。

## 為何 `名稱可見持倉人數` < `CCASS 公佈人數`

本地 change-log 由 **2023-01-03** 開始。若某參與者最後一次變動早於
呢個日子，佢嘅餘額喺我哋窗口內完全睇唔到 → 重建會漏。

所以 **top_5 / top_10 一律優先採用 `dailylog` 嘅 `c5` / `c10`**
（CCASS 自己公佈嘅權威 aggregate），只有喺 `dailylog` 冇覆蓋
同一日時才用重建值。`concentration_source` 欄會標明用邊個：
`ccass_dailylog`（權威）或 `reconstructed`（推算）。

`completeness.unattributed_pct` 表示有幾多 % 係「知道有人持有、
但叫唔出名字」——UI 應該照實顯示，唔好隱藏。

## 檔案角色

- `ccass_snapshot.py` — **CCASS 事實來源**。`snapshot()` / `movements()` /
  `trading_dates()` / `market_latest_date()`。所有新功能應該用呢個。
- `ccass_api.py` — 薄 CLI wrapper，供 zo.space API route 用
  （`snapshot` / `movements` 子命令）。
- `hybrid_ccass_pipeline.py` — 舊介面，內部已改為呼叫 `ccass_snapshot`，
  保持 `--range` / `--local-only` 等參數向後兼容。
- `ccass_local.py` — 舊版逐日查詢（**有第 1 個 bug**，勿再用嚟做快照）。
- `validate_ccass.py` — 拿 40 隻股票對 `dailylog` 公佈值做回歸測試。
  改動 CCASS 邏輯後**必須跑一次**，目標 40/40。

## 驗證基準（2026-07-31，對過 check-site.ai）

| 股票 | 頭5大 | 頭10大 | 第一大倉 |
|---|---|---|---|
| 01241 | 85.06% | 91.36% | CITIC SECURITIES BROKERAGE 70.1145% |
| 06898 | 35.29% | 43.61% | UBS SECURITIES HONG KONG 13.4343% |
| 01428 | 79.51% | 87.89% | BRIGHT SMART SECURITIES 54.6992% |

01241 嘅 70.11% 同 check-site.ai 完全一致（455,744,000 / 650,000,000）。

## 常用指令

```bash
python3 ccass_snapshot.py 01241                          # 最新快照
python3 ccass_snapshot.py 01241 --as-of 2026-06-30        # 指定日快照
python3 ccass_snapshot.py 01241 --from 2025-08-01 --to 2026-07-31   # 加減倉對比
python3 validate_ccass.py                                # 回歸測試（必跑）
```

## 資料覆蓋

- 本地 change-log：2023-01-03 → 2026-07-31
- `issued_shares.parquet`：3,170 隻股票已發行股數（由 `Desktop/db/HKEXequity/equity/` 抽取）
- 超出本地範圍時 `ccass_scraper.py` 會上網補抓（慢，~19s）

## imported/ 快取同步（2026-08-07 修好）

`imported/quotes.json` 同 `imported/announcements.json` 之前落後 backend 兩個多月，原因唔係「未跑 pipeline」，而係兩個 parser 本身壞：

1. **quotes 舊 parser 只讀到「市場摘要」段**（正則 `^\s*(\d{5})\s+…` 只中到權證行），所以每日淨得 10 個假代號。修正：`sync_quotes_cache.py` 先切 `<a name="quotations">` → `<a name="sales_all">` 段，再按 HKEX 兩行式排版解析（第一行 prev_close/ask/high/vol、第二行 close/bid/low/turnover），日期由檔案內文 `DATE: 31 JUL 2026` 抽（檔名唔可靠，`2026_web/` 有一批 909-byte 壞檔要按 size 篩走）。HTM 只落地到 07-31，最後 28 日由 `Desktop/db/CCASS/quotes.parquet` 經 `shortnames.parquet` 映射補上。
2. **announcements 只掃 `Desktop/db/DoD/`**，而 Windows scraper 停咗，DoD 最新只有 2026-05-22。修正：`sync_announcements_cache.py` 由 HKEXnews `titleSearchServlet` 逐日補抓，用 `news_id` 去重，保留原 schema 再加 `source` / `news_id` / `file_link` / `doc_type` / `datetime`。

`desktop_data_bridge.py` 嘅 `load_quotes()` / `load_announcements()` 現在委派去呢兩個 sync 腳本，失敗才 fallback 去 `*_legacy()`。所以直接跑 `run_full_import()` 唔會再覆寫成壞數據。

**quotes.json 新格式**（舊格式仍然兼容，`short_analyzer._load_quotes()` 兩者都食）：
```
{"meta": {...}, "names": {"00700": "TENCENT"}, "quotes": {"2026-08-05": {"00700": {close,high,low,vol,turnover}}}}
```

現狀：quotes 306 日（2025-05-12 → 2026-08-05）3,265 隻；announcements 46,300 條（→ 2026-08-07）。
`doc_type` 令財技篩選準得多，可直接用 HKEXnews 分類：供股 / 發行可轉換證券 / 《收購守則》公告 / 私有化撤銷上市 / 發售以供認購 / 配售 / 一般性授權 / 根據一般性授權發行股份。

## 期權 IV 貴／平（2026-08-07 新增）

HKEX 每日《Stock Options Daily Market Report》係唯一官方、免費、逐隻股票嘅
ATM 隱含波幅來源：`https://www.hkex.com.hk/eng/stat/dmstat/dayrpt/dqeYYMMDD.htm`
（8MB HTML，class summary 每行一隻標的：成交／Call／Put／OI／Call OI／Put OI／IV%）。

**HKEX 只保留約一年**，所以 `options_scraper.py` 係 append-only：
原始報告壓縮存 `options_data/raw/dqeYYMMDD.txt.gz`，解析結果併入
`options_data/iv_history.parquet`。跑得愈久，歷史愈值錢（IV Rank 要一年數據）。

- `options_scraper.py` — 抓 + 解析 + 併入 parquet。
  `--date YYYY-MM-DD` 單日；`--backfill N` 由今日倒數 N 個日曆日。
- `iv_analyzer.py` — 貴／平判斷。三條數：
  1. `HV20` = 過去 20 日對數回報 std × √252 × 100（收市價由 `imported/quotes.json`）
  2. `IV/HV20` ≥ 1.3 貴、≤ 0.9 平
  3. `IV Rank` = (IV − 一年最低) / (一年最高 − 一年最低) × 100；另有 percentile 同 z-score
  綜合 score：正數＝貴（利賣方），負數＝平（利買方）。
- API `/api/options-iv`（10 分鐘 cache）→ 前端 `/stock-analysis/options`。
- `daily_pipeline.py` 第 7 步會自動補抓最近 4 日 + 寫入 `daily_report.json`
  嘅 `sections.options_iv`。

覆蓋：2025-10-02 → 2026-08-06，206 個交易日、141 隻標的、28,204 行。
2025-10-02 之前 HKEX 已經清走，永遠補唔到——所以千萬唔好刪 `options_data/`。

## CCASS × 期權資金流交叉（2026-08-07 新增，第 3 項）

`ccass_options_cross.py` — 將 CCASS 歸邊變化同期權成交／未平倉異動交叉，
搵「大戶暗中收貨 + Call 有不尋常大單」呢類共振訊號。

數據來源：
- 期權側 `options_data/iv_history.parquet`（Call/Put 成交、Call/Put OI、ATM IV）
- CCASS 側 CCASS `dailylog` parquet 嘅 `c5` / `c10` / `intermed_hldg` / `intermed_cnt`，
  一次讀全市場再按 issue_id 對號（唔用 `snapshot()` 逐隻，快 40 倍）。
  `shortnames.parquet` 有啲已失效嘅 issue（例：00011、00489 已 delist／改代號）對唔到，會跳過。

指標：
- `Δc10(5d/20d)` 前 10 大 CCASS 佔比變化（百分點）。正 = 歸邊收貨。
- `CallZ` / `PutZ` 今日 Call／Put 成交對比自身 20 日嘅 z-score。≥ 2 為異動。
  冷門合約（今日成交 < `MIN_CONTRACTS` = 200 張）唔計 z，避免 0→少量成交爆天文數字；
  z 值另設上限 `Z_CAP` = 12。
- `ΔOI 5日` 未平倉 5 日變化 %。OI 升 = 真係開新倉（唔係日內炒），比成交更硬。
- `IVR` IV Rank（一年區間位置）；`ΔIV5d` IV 5 日變化點數。

訊號規則（`classify()`）：
- 收貨 + Call 熱 → 看多 4 分；若 IVR ≤ 40 再加 2（「期權未反映」＝最有價值嘅一類）
- 派貨 + Put 熱 → 看空 4 分
- 收貨但只有 Put 熱 → 分歧（鎖倉對沖）1.5 分
- 派貨但 Call 熱 → 看空 2.5 分（Call 陷阱）
- 收貨 + IVR ≤ 30 + Call OI 升 → 看多 3 分（靜吸未反映）
- 只有一邊有異動 → 1 分以下
- 加分：z ≥ 3 極端成交、街貨轉入大戶手、持股人數收窄洗籌、IV 靜靜升

CLI：`python3 ccass_options_cross.py [--stock 00700] [--bullish|--bearish] [--json]`
API `/api/options-flow`（10 分鐘 cache）→ 前端 `https://garysir.zo.space/stock-analysis/flow`
`daily_pipeline.py` 第 8 步自動跑，寫入 `daily_report.json` 嘅 `sections.ccass_options_cross`。

注意：CCASS 一般比期權報告遲一日（期權 08-06 / CCASS 08-05），所以兩個日期唔會一樣。
集中度只反映券商層面持倉，唔等於實益擁有人；期權異動亦可能係造市商對沖，唔係方向性下注。

## 異動事件 → 期權策略自動生成（2026-08-07 新增，第 1 項）

三個新模組：

**`bs.py`** — Black-Scholes 定價／greeks／impl-vol back-out。無風險利率 3.5%，
歐式近似（港股單股期權實際係美式，價外影響細，價內深嘅要打折看）。

**`earnings_calendar.py`** — 業績日曆。HKEXnews 公告標題只講「董事會會議」，
真正業績日藏喺 PDF 內文（「將於 2026 年 8 月 13 日舉行，以審批中期業績」）。
所以要落載 PDF 再用 `pypdf` 抽中文日期。
- cache：`options_data/board_meetings/` 存原始 PDF 文字（唔重複下載）
- 輸出：`options_data/earnings_calendar.json`
- 部分 PDF 用非標準 CID 字型，抽出係亂碼 → 跳過，唔會當成錯
- `event_map(days)` 只回 141 隻期權標的入面有未來業績日嘅（現時 20 隻）
- CLI：`python3 earnings_calendar.py --days 60`；`--rebuild` 重掃 PDF

**`options_chain.py`** — 逐個行使價嘅期權鏈。同 `options_scraper.py` 用同一份
HKEX 日報，但 scraper 只讀 class summary（ATM IV 一行），chain 讀明細行
（到期月／行使價／C-P／結算價／IV／成交／未平倉）。由 `options_data/raw/*.gz`
直接讀，唔會再上網。`chain(code)` 回 DataFrame；`expiries(code)` 列可用到期月。

**`strategy_engine.py`** — 主引擎。流程：
1. **事件** — `earnings_calendar.event_map()`（未來業績）+ `announcement_indexer`
   近 10 日財技公告（配售／供股／回購／盈警／要約／內幕消息）
2. **方向** — 事件本身偏好（回購＝看升、配售＝看跌、盈警＝看跌、業績＝賭波幅）
   再夾 `ccass_options_cross` 嘅 bias／score 加權
3. **波幅貴平** — `iv_analyzer` 嘅 IV/HV20 + IV Rank
4. **配策略** — 貴＋中性 → Iron Condor／Short Strangle；平＋中性 → Long Straddle／
   Strangle；有方向＋貴 → Credit Spread；有方向＋平 → Debit Spread
5. **揀腳** — 用 BS delta 對目標 delta 揀行使價（只揀 OI > 0 嘅真實合約），
   到期月一定要蓋過事件日
6. **勝率／期望值** — 用 **HV20 唔用 IV** 做對數常態積分。呢個係關鍵：
   如果用 IV 計，賣貴 IV 嘅優勢會自己抵銷，永遠見唔到 edge

輸出 `options_data/strategies.json`（38 條策略）。
CLI：`python3 strategy_engine.py [--stock 01113] [--limit 25] [--json]`
API `/api/options-strategy`（10 分鐘 cache）→ 前端 `https://garysir.zo.space/stock-analysis/strategy`
`daily_pipeline.py` 第 9 步自動跑（先 rebuild 業績日曆再出策略），寫入
`daily_report.json` 嘅 `sections.options_strategy`。

限制：結算價 ≠ 可成交價，闊價位遠價外合約實際成本高過表上數字；
唔計佣金、印花稅、買賣價差；勝率係 HV20 統計模擬，唔係預測。

## 期權內容自動化（2026-08-07 新增，第 5 項）

`content_feed.py` — 將 `iv_analyzer` / `ccass_options_cross` / `strategy_engine`
三套結果轉成「內容簡報」(content brief)，唔生成文案，只負責出**事實 + 編輯角度 +
必寫限制**，交畀 `fin-content-auto` 寫廣東話文案同出圖卡。

每條 brief 有：`kind`（iv / flow / strategy）、`category`、`headline`、`angle`、
`facts`（一條條數字事實）、`takeaway`（必須寫入圖卡嘅限制／風險）、`disclaimer`。
設計上寧可唔出帖都唔堆砌——冇料就回空 list。

輸出 `options_data/content_briefs.json`。
CLI：`python3 content_feed.py [--kind iv|flow|strategy] [--json] [--write]`
`daily_pipeline.py` 第 10 步自動 rebuild。

配套喺 `fin-content-auto/`（詳見該項目 AGENTS.md）：
`backend/options_content.ts` 讀 brief → Gemini 寫圖卡文字 + FB 貼文 → 出 1080×1350 圖卡。
CLI：`cd /home/workspace/fin-content-auto && bun run_options_content.ts`（預覽，唔發佈）

## 期權分析 → 內容簡報（2026-08-07 新增，第 5 項）

`content_feed.py` — 唔生成文案，只把 `iv_analyzer` / `ccass_options_cross` /
`strategy_engine` 三套結果整理成結構化「內容簡報」(brief)，交畀
`fin-content-auto` 嘅 Gemini 寫廣東話文案 + 出圖卡。

每條 brief 嘅 schema：
```
{kind: "iv"|"flow"|"strategy", category, date, date_zh, headline,
 angle,          # 編輯角度（一句，講呢啲數字為何值得睇）
 facts: [{...}], # 純數字事實，key 已中文化，文案要照抄唔可以改數
 takeaway,       # 限制／風險，文案一定要寫入去
 disclaimer}
```

三種 brief：
- `strategy` — 只出 `ev_hkd > 0` 而且事件日**未過**嘅策略（`_still_ahead()`）
- `flow` — CCASS × 期權共振，優先「已歸邊但 IV 仍低」
- `iv` — 分「極貴・偏賣方」同「極平・偏買方」兩條

設計原則：只出數字事實，唔講買賣、唔講目標價；冇料就回空 list，
寧可唔出帖都唔好堆砌流水帳。

CLI：`python3 content_feed.py [--kind iv|flow|strategy] [--json] [--write]`
輸出 `options_data/content_briefs.json`。
`daily_pipeline.py` 第 10 步自動跑，寫入 `daily_report.json` 嘅
`sections.content_briefs`。

## Futu OpenD 數據匯入（2026-08-15 新增）

`futu_data_importer.py` — 由 OpenD (127.0.0.1:11111) 拉數據，輸出去
`Desktop/db/Futu/`：
- `Snapshot/snapshot_YYYYMMDD.parquet` — 期權標的即時快照（142 欄：沽空可否、
  沽空率、融資狀態、抵押率、買賣價差、委比、波幅、每手股數…）
- `Kline/kline_day.parquet` — append-only 日K，去重；含 PE、換手率、成交量、漲跌幅

CLI：`--snapshot-only` / `--kline-only` / `--all-market`（快照掃全港股）/ `--days N`

**配額規則**：歷史K線受 300 隻 / 30 日滾動限制。默認只做 135 隻**期權標的**
（`options_data/contract_specs.json`），正好喺額度內。新股拉全年、已有股只拉最近
10 日增量 — 同一隻股 30 日內重複拉唔再扣額，所以增量更新免費。
`--all-market` 只影響快照（快照無配額限制），唔會碰 K線配額。

`futu_scheduler.py` — 常駐 process 服務 `futu-data-scheduler`，每日 01:00–07:30 HKT
取數窗口：01:00 跑 K線增量、01:30 跑快照，失敗每 30 分鐘重試，07:30 收工
（唔撞 07:30 嘅 daily_pipeline）。狀態存 `futu_scheduler_state.json`，
log 去 `/dev/shm/futu-data-scheduler.log`。唔燒 AI 額度。

⚠️ **OpenD 登入要小心**：連續登入失敗會鎖帳號 27 分鐘。排程器只會檢查
11111 端口通唔通，**唔會自動重登**；OpenD 死咗就跳過同記錄，唔會重試登入。
手動登入要用 tmux（避免 supervisor 無限重啟撞鎖）：
`tmux new-session -d -s futu '/opt/FutuOpenD/FutuOpenD -login_account=... -login_pwd=... -api_port=11111 -websocket_port=11112 -console=1'`
然後 `tmux send-keys -t futu "req_phone_verify_code" C-m`，收到短信驗證碼後
`tmux send-keys -t futu "input_phone_verify_code -code=XXXXXX" C-m`。

⚠️ **快照檔名用香港時間（HKT）日期**（`futu_data_importer.py` 嘅 `now_hkt()`，08-18 修）。
之前用 `datetime.now()`（伺服器 UTC）—— 凌晨 01:30 HKT 嘅快照會寫落**尋日**嘅檔名
（覆蓋舊檔之餘，健康檢查搵唔到今日檔 → 假警報）。log 時間戳都係 HKT。

## 牛熊波幅雷達重建（2026-08-18，OpenD 版取代 Manus 私有 repo）

`cbbc_radar_builder.py` — 每晚生成 gsmart-box「牛熊波幅雷達」Dataset JSON：
- HSI/VHSI 用 OpenD **快照**（`HK.800000` / `HK.800125`，唔用 kline 配額）
- 恒指牛熊證名單**直接由 OpenD `get_warrant`（HK.800000 全批 BULL+BEAR，status==NORMAL，200/頁分頁）**——唔再依賴本地 scrape 檔／Manus Google Sheets；本地 `Desktop/db/CBBC/cbbc_*.parquet` 只做後備（get_warrant 失敗先用）（2026-08-20 改）
  - 語義同舊版一致：朝早 08:00 跑時 NORMAL 名單 = 當時仲 live 嘅牛熊證（夜間跑會少咗當日已被收回嗰啲，屬預期）
  - `strategy_lab.py`（立體策略）**已轉 OpenD 主源（2026-08-20）**：`cbbc_opend_fetcher.py` 用 get_warrant（HSI+135 期權標的 BULL/BEAR）+ batch snapshot（現價）+ request_history_kline（14日 ATR）重現舊 scrape schema；⚠️ hket `qu` 係正規化街貨量 = `street_vol ÷ (conversion_ratio×100)`，唔係 raw 百萬份。scrape 檔降級做（a）外國指數 SPX/DJI/NDX 補位（futu get_warrant 唔支援美股指數）（b）OpenD 全掛時後備。`data_freshness.cbbc_source` 顯示 'OpenD' 或 'scrape-fallback'
  - 輸出真路徑係 `options_data/strategy_lab.json`（根目錄同名舊殘留已刪）；gsmart-box `strategyLab.ts` tRPC router 直接讀呢個路徑，OpenD 攞完即刻生效。健康總覽有「牛熊主源」檢查（非 OpenD = 自動重跑 strategy_lab.py）；scrape 後備檔過期唔再響警報
- batch snapshot（200/批，~8 秒）→ `wrt_recovery_price` + `wrt_street_vol`
- 每個交易日嘅區間聚合存 `cbbc_radar/days/<date>.json`（append-only），
  `days[]` = 最近 5 日；輸出 `cbbc_radar/dataset_latest.json`
- 設咗 `CBBC_RADAR_UPDATE_URL` + `CBBC_RADAR_UPDATE_SECRET`（Zo Secrets）
  就自動 POST 上 `https://gsmart-box.manus.space/api/cbbc/update`

**公式（對過舊 bundled dataset 逐項重現，勿改）**：200 點區間；
`EM = close × VHSI/√252`；upper/lower zone 嘅 `overlapFraction` = 區間同
`[close,upperDay]`／`[lowerDay,close]` 交集比例（分母 hi−lo=199）；
`coveredAmount = outstanding × overlap`；`distanceWeight = max(0.15, 1−|mid−close|/EM)`；
`weightedContribution = covered × distanceWeight`；
scores=(bear−bull)/sum；verdict 用 weightedScore ±0.25 定型（唔再靠 AI）。
zone 只留 ±3000 點內或 overlap>0。`outstanding` 單位 = 百萬份（street_vol/1e6）。
`premarket`「先」向判定規則（2026-08-21 用戶定）：夜期（HSImain 夜市收 vs 日市收）同 ADR 代理**各自**睇——任何一邊 |升跌| > 100 點先有方向訊號（偏向該邊方向），兩邊都唔夠 100 點 = 「先窄幅波動」（initialDirection=null）；兩邊都 >100 同向 = isConfirmed；兩邊 >100 但相反 = 取幅度較大一方、唔確認。`adjustment` 跟訊號邊（唔夠 100 點嗰邊唔用）。舊邏輯「夜期 -2 點都當同向確認」係錯，已廢。**ADR 代理必須按恒指權重加權**（阿里8%/匯控7.5%/網易1.5%…美股日K收市計，覆蓋約20%恒指權重）——等權中概籃會俾細權重股拖歪（08-21 教訓：網易-5.9%/B站-3.8% 拖到等權 -1.02% → 假 -262 點「先跌」；加權後同一晚 +5 點）；非恒指成分（PDD/NIO/BEKE）唔入籃。

`cbbc_radar_scheduler.py` — 常駐服務 `cbbc-radar-scheduler`，每日 21:30 HKT 跑，
失敗 22:00/22:30 重試。entrypoint 用 `bash -c 'source /root/.zo_secrets; ...'`
去食 Secrets。

### 牛熊波幅雷達（gsmart-box，OpenD 重建版，08-18）

- `cbbc_radar_builder.py` — 每日生成 dataset JSON：`HK.800000`(恒指) + `HK.800125`(VHSI) 快照 + 本地 CBBC scrape 名單逐隻 batch snapshot 攞 `wrt_recovery_price` / `wrt_street_vol`；200 點 zone 聚合；EM=close×VHSI/√252；verdict 用 weightedScore ±0.25；歷史日 append 存 `cbbc_radar/days/<date>.json`
- `cbbc_radar_scheduler.py` — 常駐服務 `cbbc-radar-scheduler`，每個交易日 08:00 HKT 跑（失敗 08:40/09:10 重試），成功自動 POST 上 gsmart-box `/api/cbbc/update`；**08:45 重算 premarket（`--refresh-premarket`：夜期/ADR 朝早未齊就補，覆蓋變好先改 dataset 同 verdict）再重推一次**—— Manus 舊 pipeline 仍每日 08:40 推舊格式數據覆蓋，重推保證我哋係最終版本；根治要喺 Manus 停咗佢個每日推送任務
- **密鑰喺 `stock-analysis/.cbbc_radar_secrets`**（chmod 600，已 gitignore）——平台 Secrets 同步有延遲/唔可靠，排程器 entrypoint 係 source `/root/.zo_secrets` 再 source 呢個檔
- **⚠️ Manus edge WAF 會 403 擋 Python-urllib 預設 UA**（唔係密鑰錯！密鑰錯係 401）——push 一定要帶 browser UA header
- 盤前 premarket（夜期+ADR，2026-08-21 定案）：**夜期用 HSImain 快照**（朝早 08:00 跑時 `last_price`=尋晚 T+1 段收、`prev_close`=昨日日市收；時間窗驗證用**結算日 17:00～翌日 03:05** 做基準——週五晚嗰節夜市收喺週六 03:00，用 prediction_date 做基準會喺週一漏咗；開市後 live 價亦會被呢個窗擋走）——⚠️ 唔好用 futu 期貨歷史 K線，T+1 夜期段延遲入庫，朝早跑根本未有尋晚 bars（曾因此攞錯舊夜期 → 假 −256.9）。**ADR 代理用恒指權重加權美股 ADR 籃**（阿里8/匯控7.5/網易1.5/JD1/百度0.8/理想0.4/B站0.3/小鵬0.2/中通0.2/攜程0.2，`ADR_BASKET` dict，權重係約數要季度檢視；美股**日K收市**計，日K攞唔到先後備快照；覆蓋權重<15% 當無數據）——⚠️ 唔可以等權（08-21 教訓：等權中概籃俾網易-5.9%/B站-3.8% 拖到 -262 點假「先跌」，加權後同晚 +5 點；美股快照 update_time 個 field 係壞嘅唔好信）。方向規則：任何一邊 |升跌| >100 點先算訊號，兩邊都唔夠 = 先窄幅波動。HSI open/high/low 一樣由日 K 攞（快照開市前未更新、開市後歸零）
- ⚠️ **close 用 HSI 歷史日 K 攞 trade_dt 收市，唔用快照 `prev_close_price`**（朝早 08:00 快照 prev_close 可能仲係前日，08-21 實測攞到 25,495 而真實 08-20 收係 25,698，相差 200 點）；快照 prev_close 只做後備。open/high/low 仍用快照（朝早跑時係昨日全日數值）

## 期權策略真回測 vs 舊代理／舊鐵鷹回測（2026-08-17 對帳）

同一個「鐵鷹」出現兩組完全相反嘅數字，**唔係隨機差異，係三個唔同嘅計法**。
以後引用回測結果前，必須先講明用邊個引擎。

| 引擎 | 檔案 | 買賣咩 | 鐵鷹結果 |
|---|---|---|---|
| 代理（股票） | `quant_engine/batch_backtest_results.json` | **股票**，用技術指標當期權訊號 | 無鐵鷹（29 條全部股票策略，28C+1B） |
| 舊鐵鷹 | `condor_backtest.json` | 期權，但**到期前 14 日就套用到期損益公式** | 勝率 79.5%、平均 +17.4% 風險回報 |
| 真期權 | `quant_engine/options_backtest_results.json` | 期權，**持到到期用內在值結算** | 勝率 55.7%、期望 **−$3,629** |

**舊鐵鷹（`condor_engine.py`）為何偏高 —— 呢個係缺陷，唔係口味問題：**
`backtest()` 喺 `FORCE_EXIT_DTE = 14` 日嘅股價上直接叫 `_payoff()`，
而 `_payoff()` 係**到期損益公式**（只計內在值）。即係假設「剩 14 日嘅倉
已經冇時間值」。實際剩 14 日平倉，賣方要付返嘅價**高過**內在值，
所以舊數字系統性高估賣方。另外舊引擎亦唔扣任何佣金／交易所費。
→ `condor_backtest.json` 嘅 79.5% 勝率**唔可以當實盤預期**。

**真引擎（`options_backtester.py`）修過嘅兩個 bug（08-17）：**
1. **`MIN_LEG_OI = 100` 把合理行使價篩走後,揀到深價內垃圾腳。**
   例：03993 賣「delta 0.25 價外 Call」實際揀到行使價 8.0（spot 15.89，
   delta 0.998）。加 `MAX_DELTA_DRIFT = 0.15`：揀到嘅腳 |delta| 偏離
   目標超過 0.15 就唔開倉。
2. **裸賣倉位大小計爆。** `strike*0.20 − net` 喺深價內變負數 → 跌到地板
   `contract_size*0.01` → `qty` 撞 `MAX_CONTRACTS = 50`。加保證金地板
   `spot * 0.10 * contract_size`。
   修正後賣 Call 期望值由 −$982 收窄到 −$234，最壞單筆由 −$1,490,660
   降到 −$132,203。
3. 收 credit 嘅有翼結構加 `MIN_CREDIT_RATIO = 0.12`（同 `condor_engine`
   一致），淨收入唔夠翼寬 12% 唔開倉。

**現時真回測結論（2025-10-02 → 2026-08-14、129 隻標的、212 個交易日、
6,710,162 條真實結算價）：**
- 期權**買方**賺（買勒式 +$2,997／筆 B 級、買跨式 +$1,762），
  **賣方全部負期望**（賣 Call −$234、賣 Put −$1,160、鐵鷹 −$3,629）。
- 呢個內部一致：代表期內**實際已實現波幅大過期權隱含波幅**，
  即係呢段期間港股期權**偏平（賣方冇溢價）**。同 `vrp_engine.py` 嘅
  VRP 結論應該互相對得上——對唔上就要查。
- 賣方勝率仍然高（賣 Call 81.5%）但期望值負 = **典型「贏多次細錢、
  輸一次大錢」**。所以睇期權賣方策略**唔可以只睇勝率**，一定要睇
  期望值同最壞單筆。
- 唯一 S 級 `parity_arb`（Conversion／Reversal 三腳鎖死）勝率 99.2%、
  最壞單筆只 −$105，結構上係真套利。**但 357 筆全部基於 HKEX 結算價,
  而結算價 ≠ 可成交價**；真實買賣差價下大部分 parity 偏離會消失。
  當佢係「數據上存在嘅偏離」，唔係「可落盤嘅利潤」。

**共同限制（兩個引擎都有）：** 結算價唔等於可成交價；闊價位遠價外合約
實際成本高過表上數字；唔計買賣差價同滑價。

**接線（08-17 完成）：**
- `daily_pipeline.py` 第 14 步：先 `chain_history.py --update` 補新交易日鏈，
  再跑 `quant_engine/options_backtester.py`，摘要寫入 `daily_report.json`
  嘅 `sections.options_backtest`（tier_counts + top 10）。
- API `/api/options-backtest` 直讀 `quant_engine/options_backtest_results.json`。
- 前端 `https://garysir.zo.space/stock-analysis/options-backtest`（私人）
  按類別（真套利／期權買方／期權賣方／方向性價差／波幅套利）篩選，
  每條顯示期望值／勝率／Sharpe／盈虧比／最壞單筆，展開見成交樣本。
  頁頂固定警示：期權賣方唔可以只睇勝率。

