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
