# Stage 0 — 「回測數字可信」修正清單

審查報告指出：原本嘅回測數字**唔可以用嚟做決策**，因為平倉價用到期內在值、
止賺止損從來冇執行、成本假設為零、樣本量誤算、選股門檻用全樣本。
Stage 0 嘅唯一目標係令回測數字誠實 —— **唔係令佢好睇**。

修完之後如果策略消失，咁就係策略本身唔存在，而唔係修正做錯。

## 判斷標準（唯一放行條件）

> 3% 每腳滑價下仍然有**正期望**，而且**樣本外勝率 ≥ 60%**。

跑法：

```bash
python3 condor_engine.py --backtest-all --sensitivity
python3 walkforward.py --slippage 0.03
```

`walkforward.json` 嘅 `verdict.pass` 必須為 `true`。

---

## 清單

### 1. ✅ 平倉價用真實期權鏈重估，唔用到期內在值

**問題**：`_payoff()` 計嘅係**到期**內在值，但餵入嘅係 14-DTE 嘅現價 →
等於假設剩 14 日嘅期權時間價值為零，系統性高估勝率同回報。

**改動**：
- `condor_engine.value_legs(d, code, c, spot, iv_hint, basis) -> (legs, src)`
  逐條腳去當日真實鏈搵中價；`src` = `"chain"`（真實）/ `"model"`（BS 回退）/ `"none"`。
- `chain_cache.py` 新增：每份日報只 parse 一次 + LRU（60 日）+ parquet 落地，
  否則逐日 MTM 會慢到不能用。
- `pnl = credit − exit_cost`，`exit_px_source` 記錄咗價錢由邊嚟。

**測試**：`test_value_legs_uses_real_chain_not_intrinsic`、
`test_value_legs_falls_back_to_model_when_chain_missing`

---

### 2. ✅ 真正執行 `PROFIT_TARGET = 0.50` 止賺 + `STOP_LOSS = 2 × credit`

**問題**：`PROFIT_TARGET` 定義咗但**從來冇被引用**；min/max clamp 係 no-op。
即係回測其實係「開倉後不理，剩 14 日平倉」，同你打算行嘅紀律完全唔同。

**改動**：`backtest()` 改成**逐日 MTM 循環**，每日重估 4 條腳並檢查：
賺 ≥ 50% credit → 平（`exit_reason="target"`）；
蝕 ≥ 2 × credit → 平（`"stop"`）；
到 `FORCE_EXIT_DTE=14` → 平（`"time"`）。
每張倉留 `mtm = {date: 累計損益}` 供 `portfolio.py` 用。

**測試**：`test_backtest_records_daily_mtm_and_exit_reason`、
`test_backtest_stop_loss_triggers_on_iv_spike`

---

### 3. ✅ 加入滑價／佣金，並跑 0 / 1 / 3 / 5% 敏感度

**問題**：回測用 HKEX **結算價**，即係零成本假設。4 腳 × 3% ≈ 12%，
**剛好等於 `MIN_CREDIT_RATIO = 0.12` 全部門檻**。呢個唔係細節，
係決定策略存唔存在。

**改動**：
- `costs.py`：`Leg` / `CostModel(slippage_per_leg, commission_per_leg, min_fill)`，
  `open_credit`（賣收 bid、買付 ask）、`close_cost`、`expiry_cost`、`round_trip_drag`。
- `build()` 同時回報 `mid_credit`（結算價口徑）、`credit`（扣成本）、`cost_drag`。
- `condor_engine --sensitivity` 跑 0/1/3/5% 網格。
- `costs.from_measured_spreads()` 由 `options_data/chain_live.parquet`
  **實測** bid/ask 反推真實滑價（`--use-measured-slippage`）。

**測試**：`test_cost_direction_shorts_receive_bid_longs_pay_ask`、
`test_round_trip_drag_scales_with_slippage`

---

### 4. ✅ 觸價判斷用日內 high / low

**問題**：原本用收市價判斷短腳有冇被觸及 → 日內插穿再收返嘅情況全部漏掉，
低估觸價率。

**改動**：`_touched_on()` 用 `high` / `low`；`quotes.json` 冇 high/low 時
記錄 `touch_basis="close"` 並在輸出標明口徑已降級。

**測試**：`test_touched_uses_intraday_range_not_close`

---

### 5. ✅ `MIN_HISTORY` 提到 ≥ 500 並加 block bootstrap 置信區間

**問題**：`MIN_HISTORY = 40` 配 21 日前瞻窗 → 相鄰觀測重疊 20/21 日，
40 個「觀測」實質只有 **~2 個獨立樣本**。任何由此得出嘅勝率／均值都係噪音。

**改動**：
- `vrp_engine.MIN_HISTORY` 40 → **500**；新增 `MIN_INDEPENDENT = 20`（n ÷ HORIZON）。
- `_stats()` 加 `n_independent`、`mean_ci`、`win_rate_ci`（block bootstrap，block=21）。
- `_grade` / `hist_ok` 兩個門檻都要過。
- 「VRP Sharpe」改名「信噪比（非 Sharpe）」—— 佢本來就唔係 Sharpe，
  冇無風險利率、冇時間加權。

**測試**：`test_block_bootstrap_ci_widens_with_overlap`、
`test_named_bootstrap_stats`、`test_min_history_vs_overlap`

---

### 6. ✅ Walk-forward：決策日 t 只用 t 之前**已平倉**嘅回測

**問題**：`MIN_BT_WIN = 70%` / `MIN_BT_RET = 15%` 係用**全樣本**回測結果去揀股票，
再用同一批樣本報告表現 —— 典型 data snooping。全樣本勝率 70% 幾乎保證。

**改動**：`walkforward.py`
- 逐月 rebalance；決策日只可以見到**開倉同平倉都早於決策日**嘅交易。
- 用該子集套門檻揀股 → 之後一個月嘅交易計入 OOS。
- `walkforward.json` 出 `oos` / `full_sample` / `windows` / `latest.selected` /
  `verdict{oos_win_ge_60, oos_positive_expectancy, overfit_gap_pct, pass}`。
- `vol_system._load_bt()` **優先讀 `walkforward.json` 嘅樣本外選股**，
  只有冇 walkforward 時才回退 `condor_backtest.json` 並印警告 banner。

---

### 7. ✅ 每日組合 equity curve → 真 Sharpe / 最大回撤 / 最長水下期

**問題**：舊「Sharpe」= 逐張倉損益均值 ÷ 標準差，冇時間軸、冇無風險利率、
冇考慮 5-11 張倉同時持有。呢個數字唔可以同任何其他策略比較。

**改動**：`portfolio.py`
- `equity_curve(trades, capital_units)`：逐日疊加所有未平倉嘅 MTM，
  **平倉後已實現損益必須繼續計入權益**（原本會蒸發 —— 已修）。
- `metrics()`：真年化 Sharpe / Sortino / 最大回撤 / 最長水下日數 / Calmar。
- `block_bootstrap_ci()`：勝率、均值、中位數、Sharpe 嘅區間。
- `condor_engine` 嘅 `sharpe` 改名 `signal_to_noise`，避免誤導。

**測試**：`test_equity_curve_keeps_realized_pnl`、
`test_metrics_drawdown_and_underwater`

---

### 8. ✅ 順手修嘅明確 bug

| 位置 | 問題 |
|---|---|
| `options_chain.py:170` | `["date" if False else "expiry"]` 死碼 → `["expiry"]` |
| `vol_surface.py` | forward_factor 解讀同 `vol_system.py` **相反**（FF < 0.85 應該係近月被搶貴 → 賣近月／買遠月）；docstring 同 CLI 一齊改 |
| `condor_engine._t()` | `dte × 252/365 ÷ 252` 數學上等於 `dte/365`，係 no-op。改成真數營業日（`np.busday_count` + `options_data/hk_holidays.txt`），冇假期表就照實退回日曆口徑並在 docstring 講明幅度 |
| `condor_engine.backtest()` | 樣本不足時靜靜返 `None` → 改成一定回報 `insufficient` + `skipped` 原因 |

---

## 未做（Stage 1 及以後，只標記唔動手）

- wing 距離仍然固定 `spot × 5%`，應該係 `k × IV × √T`
- 冇合約乘數 / lot size / 保證金 / 倉位大小 / 組合上限
- `bs.py` `q = 0.0` 忽略股息；美式 + 實物交收提早行使未模擬
- 冇業績期 / 事件 filter
- `skew_25d` 計咗但 `p_win_model` 仍然用對稱 ATM IV
- 冇 HSI beta / 相關性上限
- `hk_holidays.txt` 未有內容 → `--time-basis trading` 暫時同 calendar 差別極微

詳見 [README.md](README.md) 嘅「已知缺口」。
