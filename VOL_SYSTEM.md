# 波幅交易系統（VRP + Term Structure + Iron Condor 三合一）

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
        ├─ vrp_engine.py    ③   波幅風險溢價（前瞻已實現波幅回測）
        │
        ├─ condor_engine.py ④   Iron Condor 建構（delta 揀腳）+ 真歷史回測
        │
        └─ vol_system.py    ⑤   三合一決策引擎 → vol_system.json
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

**評分邏輯（0–10）：** VRP 均值 + 勝率 + Sharpe + IV Rank 位置。
**過濾：** OI ≥ 3000、股票日均成交 ≥ 3M、期權鏈日成交 ≥ 200、IV ≤ 90%、報價唔可以舊過 5 日。

最後一條 filter 好重要——SUNAC（IV 117%）同 COUNTRY GARDEN（IV 258%）本來排 A 級，但佢哋今日連期權鏈都冇，係停牌／重組殘留報價。加咗 filter 之後就自動剔走。

## ③ Iron Condor condor_engine.py

用 Black-Scholes delta 揀腳（預設 short delta 0.20），翼寬用真實可交易行使價。

```bash
python3 condor_engine.py --stock 09626      # 建構今日一張
python3 condor_engine.py --backtest 09626   # 單股真回測
python3 condor_engine.py --backtest-all     # 全市場回測（約 10 分鐘）
```

**回測設定：** 每 5 日開一張、DTE 30–60、持到期、真實結算價計盈虧。
**輸出：** `condor_backtest.json`（122 隻有足夠樣本）

## ④ 三合一決策 vol_system.py

```bash
python3 vol_system.py                 # 今日訊號表
python3 vol_system.py --stock 02020   # 單股完整報告
python3 vol_system.py --all           # 連迴避／觀察都列
```

**決策矩陣：**

| VRP | FF | 回測勝率 | 決策 |
|---|---|---|---|
| ≥ 6 | 0.85–1.15 | ≥ 75% | **Iron Condor** |
| ≥ 6 | < 0.85 | ≥ 75% | Iron Condor + 日曆腿 |
| ≥ 6 | > 1.15 | ≥ 75% | Iron Condor（減碼） |
| < 0 | — | — | **迴避**（IV 平過實際波幅） |
| 6–7 | — | < 75% | 觀察 |

IV Rank < 30% 會自動標「減碼」——即使 VRP 好，自身歷史平嘅時候唔應該重注。

## 今日實測結果（2026-08-14）

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

- **回測冇計交易成本同滑價。** 港股期權買賣價差闊，實際回報會低過回測。粗略打七折比較穩健。
- **持到期假設。** 實際操作應該 50% 利潤就平倉、到期前 14 日強制平，避免 gamma 風險。回測數字係「持到底」嘅上限。
- **模型勝率用 Black-Scholes。** 港股期權係美式，早行使風險冇計。深價內 short put 尤其要留意。
- **樣本只有 10 個月**（2025-10 → 2026-08），未經歷完整熊市。2020 年 3 月式嘅波幅爆炸會令 condor 大蝕。
- **前向係數用同一日兩個到期月**推算，遠月流動性差時會有噪音。已要求 OI ≥ 2000 但唔完美。

## 每日流程建議

```bash
python3 options_scraper.py            # 拉新報告
python3 atm_history.py                # 增量更新乾淨 IV 歷史
python3 vol_system.py                 # 出訊號
```

回測唔需要日日跑（`--backtest-all` 約 10 分鐘），一星期跑一次夠。
