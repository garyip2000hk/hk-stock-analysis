# 波幅交易系統（VRP + Term Structure + Iron Condor 三合一）

> ## ⚠ 2026-08-17 Stage 0 之後：本文件下面「今日實測結果」嘅數字已作廢
>
> 審查發現舊回測有五個令數字系統性偏樂觀嘅缺陷（詳見 [STAGE0.md](STAGE0.md)）：
>
> 1. 平倉價用**到期內在值**餵 14-DTE 現價 → 等於假設剩 14 日嘅時間價值為零
> 2. `PROFIT_TARGET = 0.50` 定義咗但**從來冇被引用**，止損完全冇實現
> 3. 零滑價假設 —— 4 腳 × 3% ≈ 12%，**剛好等於 `MIN_CREDIT_RATIO` 全部門檻**
> 4. `MIN_HISTORY = 40` 配 21 日重疊窗 → 實質只有 ~2 個獨立樣本
> 5. 用**全樣本**回測結果揀股再用同一批樣本報告表現（data snooping）
>
> 所以下面「勝率 90% / Sharpe 1.68」一類數字**唔可以用嚟做決策**。
> 要重新產生可信數字：
>
> ```bash
> python3 condor_engine.py --backtest-all --sensitivity
> python3 walkforward.py --slippage 0.03
> ```
>
> 放行條件：`walkforward.json` 嘅 `verdict.pass` 為 `true`
> （3% 每腳滑價下正期望 **且** 樣本外勝率 ≥ 60%）。

一套建基於 HKEX 期權每日報告嘅完整波幅賣方系統。核心邏輯：**只喺三個獨立訊號同時confirm嘅時候落注**，其餘時間唔做。

## 系統架構

```
options_data/raw/dqe*.txt.gz  (HKEX 每日報告，212 日)
        │
        ├─ options_chain.py      逐個行使價拆鏈（settle / IV / OI / volume）
        │
        ├─ atm_history.py   ①   重建乾淨 ATM IV 歷史 → atm_iv_history.parquet
        │                        193,190 行 · 212 日 · 141 隻
        │
        ├─ vol_surface.py   ②   期限結構（term structure）+ skew + 前向波幅
        │
        ├─ vrp_engine.py    ③   波幅風險溢價（前瞻已實現波幅 + block bootstrap CI）
        │
        ├─ chain_cache.py       期權鏈快取（優先讀 chain_history.parquet）
        │
        ├─ costs.py             滑價／佣金模型 + 0/1/3/5% 敏感度
        │
        ├─ condor_engine.py ④   Iron Condor 建構 + **逐日 MTM** 回測（止賺／止損）
        │
        ├─ portfolio.py         逐日組合權益曲線 → 真 Sharpe / 最大回撤 / 水下期
        │
        ├─ walkforward.py   ⑤   走前式樣本外驗證 → walkforward.json  ← 放行關卡
        │
        └─ vol_system.py    ⑥   三合一決策引擎 → vol_system.json
                                （優先讀 walkforward 嘅樣本外選股）

futu_option_chain.py            由 Futu OpenD 收期權鏈**真實 bid/ask**
                                → options_data/chain_live.parquet
                                （HKEX 只有結算價，冇 bid/ask）
```

## 為咩要重建 IV 歷史（atm_history.py）

原本 `iv_history.parquet` 嘅 IV 係由 HKEX class summary 直接抄落嚟，污染嚴重：

| 股票 | class summary IV | 真實 ATM IV |
|---|---|---|
| 09618 JD | 116% | 32% |
| 00981 SMIC | 121% | 55% |

原因係 summary 嗰個數混雜咗深價外／流動性極差嘅合約。系統改用逐個行使價重算：
揀最接近現價嘅 call/put，取兩者 IV 平均，並要求 OI ≥ 500 或有成交。

**重建歷史（一次性，約 12 分鐘）：**
```bash
python3 atm_history.py --build
```
**每日增量（daily_pipeline 應該接呢個）：**
```bash
python3 atm_history.py            # 只補新日期
```

## ① 期限結構 vol_surface.py

```bash
python3 vol_surface.py 00700          # 單股完整曲面
python3 vol_surface.py --scan         # 全市場斜率排行
```

**前向係數（forward factor, FF）** = 前向波幅 ÷ 近月 IV：
- **FF < 0.85** → 近月被搶貴（通常有事件）→ 賣近月／買遠月（日曆價差）
- **FF > 1.15** → 遠月貴 → 反向日曆
- 0.85–1.15 → 期限結構合理，冇日曆機會

## ② VRP 引擎 vrp_engine.py

關鍵設計：用**前瞻已實現波幅**（forward realized vol），唔係 HV20 回望。
即係喺第 T 日睇 IV，然後同 T→T+21 日**之後真正發生**嘅波幅比較。呢個才係賣方真正賺蝕嘅嘢。

```bash
python3 vrp_engine.py --stock 00700   # 單股
python3 vrp_engine.py --limit 25      # 全市場排行
```

**評分邏輯（0–10）：** VRP 均值 + 勝率 + 信噪比 + IV Rank 位置。
（原本叫「Sharpe」，但佢冇無風險利率、冇時間加權，唔係 Sharpe。已改名
「信噪比（非 Sharpe）」。真 Sharpe 由 `portfolio.py` 用組合權益曲線出。）

**過濾：** OI ≥ 3000、股票日均成交 ≥ 3M、期權鏈日成交 ≥ 200、IV ≤ 90%、報價唔可以舊過 5 日。

**樣本量（Stage 0 改動）：** `MIN_HISTORY` 由 40 提到 **500**，另加
`MIN_INDEPENDENT = 20`。原因：21 日前瞻窗逐日滾動，相鄰觀測重疊 20/21 日，
40 個「觀測」實質只有 ~2 個獨立樣本。均值同勝率現在會連 block bootstrap
置信區間一齊出（`mean_ci` / `win_rate_ci`），區間夠闊就代表你唔應該相信點估計。

最後一條 filter 好重要——SUNAC（IV 117%）同 COUNTRY GARDEN（IV 258%）本來排 A 級，但佢哋今日連期權鏈都冇，係停牌／重組殘留報價。加咗 filter 之後就自動剔走。

## ③ Iron Condor condor_engine.py

用 Black-Scholes delta 揀腳（預設 short delta 0.20），翼寬用真實可交易行使價。

```bash
python3 condor_engine.py --stock 09626      # 建構今日一張
python3 condor_engine.py --backtest 09626   # 單股真回測
python3 condor_engine.py --backtest-all     # 全市場回測（約 10 分鐘）
```

```bash
python3 condor_engine.py --backtest 09626 --sensitivity   # 0/1/3/5% 滑價網格
python3 condor_engine.py --backtest-all --slippage 0.03   # 落單前用呢個
```

**回測設定（Stage 0 之後）：** 每 5 日開一張、DTE 25–55、**逐日 MTM**：
賺 ≥ 50% credit 平倉、蝕 ≥ 2 × credit 止損、剩 14 日強制平。
平倉價用**當日真實期權鏈重估 4 條腳**（搵唔到才用 BS 回退，並記錄
`exit_px_source`）。每腳滑價預設 3%，可用 `--use-measured-slippage`
由 Futu 實測 bid/ask 反推。

**輸出：** `condor_backtest.json`。每張倉多咗 `mtm`（逐日累計損益）、
`exit_reason`（target / stop / time）、`exit_px_source`、`touch_basis`；
樣本不足時會回報 `insufficient` + `skipped` 原因，唔再靜靜返 `None`。

## ④ 三合一決策 vol_system.py

```bash
python3 vol_system.py                 # 今日訊號表
python3 vol_system.py --stock 02020   # 單股完整報告
python3 vol_system.py --all           # 連迴避／觀察都列
```

**決策矩陣：**

決策用嘅回測勝率**優先讀 `walkforward.json` 嘅樣本外結果**；只有冇
walkforward 時才回退 `condor_backtest.json`（全樣本），而且會印警告 banner。

| VRP | FF | 回測勝率 | 決策 |
|---|---|---|---|
| ≥ 6 | 0.85–1.15 | ≥ 75% | **Iron Condor** |
| ≥ 6 | < 0.85 | ≥ 75% | Iron Condor + 日曆腿 |
| ≥ 6 | > 1.15 | ≥ 75% | Iron Condor（減碼） |
| < 0 | — | — | **迴避**（IV 平過實際波幅） |
| 6–7 | — | < 75% | 觀察 |

IV Rank < 30% 會自動標「減碼」——即使 VRP 好，自身歷史平嘅時候唔應該重注。

## 今日實測結果（2026-08-14）— ⚠ Stage 0 之前，已作廢

> 以下數字係用「到期內在值平倉 + 零滑價 + 全樣本揀股 + 40 個重疊觀測」
> 產生嘅，**保留只作對照**。重新跑過先可以引用。

全市場 129 隻，通過全部三重確認只有 **7 隻**：

| 代號 | 名稱 | IV | IVR | VRP | FF | 回測勝率 | 回報 | Sharpe |
|---|---|---|---|---|---|---|---|---|
| 02020 | ANTA SPORTS | 35.5 | 69% | 7.9 | 0.75 | 90% | 31% | 1.68 |
| 09626 | BILIBILI | 58.5 | 55% | 8.2 | 0.80 | 86% | 32% | 0.65 |
| 06060 | ZA ONLINE | 42.5 | 32% | 7.7 | 0.88 | 90% | 51% | 0.44 |
| 02015 | LI AUTO | 51.5 | 73% | 7.2 | 0.80 | 90% | 34% | 0.84 |
| 00285 | BYD ELECTRONIC | 40.5 | 38% | 6.7 | 1.10 | 88% | 35% | 1.05 |
| 02601 | CPIC | 31.0 | 16% | 6.9 | 0.95 | 85% | 30% | 0.99 |
| 00017 | NEW WORLD DEV | 46.0 | 25% | 6.8 | 1.25 | 85% | 17% | 0.38 |

**首選 02020 ANTA SPORTS：** Iron Condor + 日曆腿。回測 40 筆勝率 90%、Sharpe 1.68、觸價率只有 22.5%。今日可落：買 Put 65 / 賣 Put 67.5 / 賣 Call 80 / 買 Call 82.5（到期 2026-09-29），淨收 0.91、最大蝕 1.59、盈虧平衡 66.59–80.91（±19.9%）。

**騰訊被剔走**：IV 28% 但預測未來 21 日波幅 41%，VRP 負值 −5.54、勝率只有 24%。系統標「不宜賣方」——業績前後真實波幅長期高過 IV。呢個正係前瞻 RV 設計嘅價值：回望 HV 會誤導你以為騰訊平。

## 已知限制

**Stage 0 已修：**

- ~~回測冇計交易成本同滑價~~ → `costs.py` + `--sensitivity` 0/1/3/5%。
  唔再「粗略打七折」—— 打幾折係要量化嘅，唔係猜。
- ~~持到期假設~~ → 逐日 MTM，真正執行 50% 止賺 / 2× 止損 / 14 日強制平。
- ~~平倉用到期內在值~~ → 用當日真實期權鏈重估。
- ~~樣本量 40~~ → 500 + 20 個獨立樣本 + bootstrap 置信區間。
- ~~全樣本揀股~~ → `walkforward.py` 樣本外驗證。

**仍然未處理（實盤前必須睇）：**

- **冇合約乘數 / lot size / 保證金 / 倉位大小。** 全部金額係「每股」。
  賣期權嘅保證金會隨 IV 上升而擴大 —— 即係最蝕嘅時候同時被追保證金，
  呢個係最真實嘅爆倉路徑，系統完全冇模擬。
- **模型勝率用 Black-Scholes，`q = 0.0` 忽略股息。** 港股個股期權係美式
  **加實物交收**，除息前深度 ITM 短 call 被提早行使係實際會發生嘅事。
- **冇業績期 / 事件 filter。** 賣期權最大單筆損失通常喺業績、供股、私有化、
  突發監管消息。冇事件日曆等於明知有炸彈照開倉。
- **wing 距離固定 `spot × 5%`。** IV 60% 同 IV 20% 用同一個翼寬，
  風險差天共地。應該係 `k × IV × √T`（Stage 1）。
- **`skew_25d` 計咗但未用。** `p_win_model` 仍然用單一 ATM IV 假設對稱分佈
  → 系統性低估下跌尾部。
- **冇 HSI beta / 相關性上限。** 同時開 5-11 張港股 condor，實質係一個
  放大嘅「沽 HSI 波幅」單一注碼。
- **`step = 5` + DTE 25-55 → 同時 5-11 張未平倉。** 逐張勝率唔等於資金曝險，
  睇 `portfolio.py` 嘅組合曲線先準。
- **真實 bid/ask 只有前瞻資料。** `futu_option_chain.py` 由開始快照嗰日
  收起，歷史回不了，所以歷史回測永遠只能「結算價 + 假設滑價」。
- **樣本只有 10 個月**（2025-10 → 2026-08），未經歷完整熊市。2020 年 3 月式嘅波幅爆炸會令 condor 大蝕。
  而且 10 個月配 500 個 `MIN_HISTORY` 門檻 → 大部分股票會直接唔夠樣本。
  **呢個唔係 bug，係誠實**：唔夠樣本就唔應該有結論。
- **前向係數用同一日兩個到期月**推算，遠月流動性差時會有噪音。已要求 OI ≥ 2000 但唔完美。

## 每日流程建議

```bash
python3 options_scraper.py            # 拉新報告
python3 atm_history.py                # 增量更新乾淨 IV 歷史
python3 chain_history.py --update     # 補期權鏈快取（chain_cache 會用）
python3 vol_system.py                 # 出訊號
```

有 OpenD 跑住就順手收真實 bid/ask（歷史回不了，愈早開始愈好）：

```bash
python3 futu_option_chain.py --snapshot
python3 futu_option_chain.py --spread-report
python3 costs.py --from-live          # 由實測價差反推真實滑價
```

**每星期一次：**

```bash
python3 condor_engine.py --backtest-all --slippage 0.03   # ~10 分鐘
python3 walkforward.py --slippage 0.03                    # 樣本外關卡
```

`walkforward.py` 未跑過嘅話，`vol_system.py` 會印警告 banner 提醒你
現時用嘅係全樣本（in-sample）選股結果。

## 測試

```bash
pytest tests/ -q                      # 42 個測試，全部用合成數據
```

唔依賴本機 `options_data/`。覆蓋 put-call parity、implied vol 往返、
前瞻波幅對齊（唔可以偷望過去）、成本方向、組合權益曲線（平倉後已實現
損益必須保留）、bootstrap 區間隨重疊變闊、真實鏈重估唔等於到期內在值、
止損觸發、日內觸價、`chain_cache` 有／冇 `chain_history` 兩條路徑。
