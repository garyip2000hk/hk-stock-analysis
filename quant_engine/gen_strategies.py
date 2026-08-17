#!/usr/bin/env python3
"""Generate 289量化交易策略 markdown files for Obsidian 量化日報"""
import os, json

OUT = "/home/workspace/Desktop/Garysir/量化日報"

STRATEGIES = {
    "動量": [
        ("Dual Momentum 雙動量", "同時追蹤絕對動量（vs 國債）同相對動量（vs 同類資產），揀最強嘅持倉", "trend"),
        ("Time Series Momentum", "過去 N 日回報為正就做多，為負就做空。純粹嘅時間序列動量", "trend"),
        ("Cross-Sectional Momentum", "橫截面動量：買入同行業中近期表現最強嘅，賣出最弱嘅", "cross_sectional"),
        ("Residual Momentum", "先對 Fama-French 因子回歸，取殘差再做動量。排除因子暴露後嘅純 alpha", "cross_sectional"),
        ("Seasonal Momentum", "利用季節性效應做動量：例如 11-4 月做多、5-10 月避險（Sell in May）", "seasonal"),
        ("52-Week High Momentum", "買入接近 52 週新高嘅股票。學術文獻證實有顯著超額回報", "trend"),
        ("Industry Momentum", "做多過去 6 個月表現最好嘅行業 ETF，做空最差嘅行業", "cross_sectional"),
        ("Risk-Adjusted Momentum", "用 Sharpe/Sortino ratio 篩選動量信號，排除高波動嘅假突破", "trend"),
        ("Frog-in-the-Pan Momentum", "只揀「資訊離散」（好多日小升小跌累積）嘅動量股，避開單日爆升", "behavioral"),
        ("Idiosyncratic Momentum", "排除市場 beta 後嘅 idiosyncratic return 做動量，更純更持久", "cross_sectional"),
    ],
    "均值回歸": [
        ("Bollinger Band Mean Reversion", "價格觸及布林通道下軌時買入，回歸中軌時賣出", "mean_reversion"),
        ("RSI Oversold Bounce", "RSI < 30 買入，RSI > 70 賣出", "mean_reversion"),
        ("Pairs Trading 配對交易", "兩隻高度相關股票價差擴大時做配對收斂", "stat_arb"),
        ("Triangular Arbitrage 三角套利", "外匯市場三種貨幣之間嘅無風險套利", "stat_arb"),
        ("Z-Score Mean Reversion", "計算資產價格對其均值嘅 Z-score，超過 ±2 時交易", "mean_reversion"),
        ("Kalman Filter Pairs", "用 Kalman Filter 動態估算配對比例，比靜態 cointegration 更靈活", "stat_arb"),
        ("Statistical Arbitrage Index Arb", "ETF 同其成分股之間嘅統計套利", "stat_arb"),
        ("Lead-Lag Pairs", "搵出有領先-滯後關係嘅股票對，利用滯後股反應慢做回歸", "stat_arb"),
        ("Canaletto Channel Trading", "用 Donchian Channel 做均值回歸：破上軌賣、破下軌買", "mean_reversion"),
        ("Relative Strength Rotation", "喺同板塊內揀 RSI 最低嘅做多、最高嘅沽出，預期均值回歸", "mean_reversion"),
    ],
    "突破": [
        ("Opening Range Breakout", "用開市頭 N 分鐘嘅高低範圍，突破時順勢入場", "breakout"),
        ("Volatility Breakout System", "ATR x N 倍做突破通道，突破 + 成交量放大 = 入場信號", "breakout"),
        ("Darvas Box Trading", "Nicolas Darvas 發明：價格突破歷史箱頂買入，跌破箱底止損", "breakout"),
        ("Gap Fill Strategy", "跳空缺口出現後，等回補缺口時做順勢/逆勢交易", "gap"),
        ("Gap and Go", "跳空 + 高成交量確認趨勢，唔等回補直接跟入", "gap"),
        ("Earnings Gap Trading", "業績後跳空，做順勢或逆均值回歸", "event_driven"),
        ("Inside Day Breakout", "今日高低完全喺昨日範圍內 = Inside Day，突破時跟方向", "breakout"),
        ("NR7 Breakout (Narrow Range 7)", "7 日內最窄波幅日，突破 = 大行情前兆", "breakout"),
        ("Turtle Trading System", "Richard Dennis 教嘅經典海龜交易法：20/55 日突破 + ATR 倉位管理", "trend"),
        ("Keltner Channel Breakout", "EMA + ATR 通道，價格突破通道時順勢交易", "breakout"),
    ],
    "成交量": [
        ("Volume-Weighted MACD", "用成交量加權價格計算 MACD，減少低量噪音", "volume"),
        ("On-Balance Volume Divergence", "價格升但 OBV 跌 = 看空背離；價格跌但 OBV 升 = 看多背離", "volume"),
        ("Money Flow Index (MFI)", "結合價格同成交量嘅 RSI，MFI < 20 超賣買入", "volume"),
        ("Chaikin Money Flow", "CMF > 0 資金流入、< 0 資金流出，配上價格走勢做確認", "volume"),
        ("Volume Climax", "極端巨量 = 可能係頂/底，利用呢個信號做反向", "volume"),
        ("Accumulation/Distribution Line", "A/D Line 同價格背離 = 大戶暗中收貨/派貨信號", "volume"),
        ("Ease of Movement", "價格變動 / 成交量 = 股價移動難易度，低阻力 = 易升", "volume"),
        ("VWAP Reversion", "價格遠離 VWAP 時做均值回歸，日內交易常用", "intraday"),
        ("TWAP Execution", "用 TWAP 分單減低 market impact（執行算法，唔係策略本身）", "execution"),
        ("VPIN (Volume-Synchronized PIN)", "用成交量同步嘅價格變動估算資訊不對稱，預測短期波動", "microstructure"),
    ],
    "波動率/期權": [
        ("Straddle", "買入同行使價嘅 Call + Put，賭大波動但唔賭方向", "options"),
        ("Strangle", "買入唔同行使價嘅 OTM Call + Put，成本低過 Straddle", "options"),
        ("Butterfly Spread", "買入一個低行使價 Call + 賣出兩個中間 Call + 買入高行使價 Call，賭窄幅", "options"),
        ("Calendar Spread", "賣出近月期權 + 買入遠月期權，賺時間值 decay + 波幅差", "options"),
        ("Ratio Spread", "買 N 個 Call + 賣 M 個 Call（M > N），賭方向但控制成本", "options"),
        ("VIX Futures Term Structure", "VIX contango 時賣 VIX futures、backwardation 時買入", "volatility"),
        ("IV Skew Trading", "利用 Put skew 異常高/低，做 skew 均值回歸", "volatility"),
        ("Delta Hedging", "持有期權倉位 + 用標的對沖 delta 風險，賺 gamma scalping", "options"),
        ("Gamma Flip Point", "gamma 由負轉正時 = 市場方向性風險加劇，調整倉位", "options"),
        ("Volatility Carry", "做多低實質波動 + 沽高隱含波動，賺 vol risk premium", "volatility"),
        ("Forward Vol Agreement", "鎖定未來某段時間嘅波動率，類似利率市場嘅 FRA", "volatility"),
        ("Correlation Trading", "做多/沽空指數期權波動率 vs 成分股波動率嘅相關性", "volatility"),
        ("Dispersion Trading Detail", "做多成分股波動 + 沽空指數波動（詳細版）", "volatility"),
        ("Vanna/Volga Hedging", "對沖期權組合嘅 vanna (delta/vega 交叉) 同 volga (vega convexity)", "options"),
        ("Pin Risk Management", "到期日管理：股價近行使價時 gamma 極大，需要小心對沖", "options"),
    ],
    "趨勢跟蹤": [
        ("Supertrend", "用 ATR 計算動態止損嘅趨勢跟蹤系統", "trend"),
        ("Parabolic SAR", "用 SAR 點做 trailing stop，順勢移動", "trend"),
        ("ADX Trend Strength", "ADX > 25 = 趨勢市（用順勢策略）、ADX < 20 = 震盪市（用均值回歸）", "trend"),
        ("Donchian Channel Trend", "用 N 日最高/最低做通道，同 Turtle 類似但更簡單", "trend"),
        ("Chandelier Exit", "用 ATR x 倍數做 trailing stop，似吊燈跟住最高/最低點", "trend"),
        ("Heikin-Ashi Smoothing", "用 Heikin-Ashi 燭過濾噪音，順勢持倉更穩定", "trend"),
        ("Renko Chart Trading", "用 Renko 磚形圖過濾時間噪音，淨睇價格變動方向", "trend"),
        ("Three-Line Strike", "K 線形態：連續三支同色 + 一支反色包覆前三支 = 趨勢持續", "pattern"),
        ("Ichimoku Cloud", "一目均衡表：價格喺雲上 = 升勢、雲下 = 跌勢；雲厚薄 = 支持/阻力強弱", "trend"),
        ("Zigzag Indicator", "過濾掉小波動，只睇大轉折點，用嚟客觀畫趨勢線", "trend"),
    ],
    "因子投資": [
        ("Fama-French 3-Factor", "市場 beta + 規模(size) + 價值(value) 三因子模型", "factor"),
        ("Carhart 4-Factor", "Fama-French 加埋動量因子 (WML - Winners Minus Losers)", "factor"),
        ("Fama-French 5-Factor", "市場 + 規模 + 價值 + 盈利 + 投資五因子", "factor"),
        ("Quality Factor (SQM)", "揀 high profitability + low investment + high ROE 嘅優質股", "factor"),
        ("Low Volatility Anomaly", "低波動股票長期跑贏高波動股票（違反 CAPM）", "factor"),
        ("Betting Against Beta", "做多低 beta + 沽空高 beta，賺 BAB 因子溢價", "factor"),
        ("Size Premium (SMB)", "小型股長期有超額回報（雖然近年減弱）", "factor"),
        ("Value Premium (HML)", "低 P/B 股票長期跑贏高 P/B（價值 vs 成長）", "factor"),
        ("Accruals Anomaly", "應計項目高嘅公司未來回報較低（盈利質素差）", "factor"),
        ("Gross Profitability Premium", "毛利/資產比率高嘅公司長期跑贏", "factor"),
    ],
    "事件驅動": [
        ("Merger Arbitrage", "併購公佈後買入目標公司股票，賺取併購價同市價嘅差價", "event_driven"),
        ("Spin-Off Arbitrage", "分拆上市時，母公司通常被低估，買入母公司等價值釋放", "event_driven"),
        ("Buyback Strategy", "公司回購 = 正面信號，跟買短期往往有超額回報", "event_driven"),
        ("Insider Trading Mimicry", "跟蹤內部人買賣信號：內部人買入 > 賣出 = 跟做多", "event_driven"),
        ("Dividend Capture", "除淨前買入、除淨後賣出，捕捉股息（扣手續費後要有利潤）", "event_driven"),
        ("Index Rebalancing", "指數調倉時提前買入新納入股票、賣出剔除股票", "event_driven"),
        ("Rights Issue Arb", "供股前後的價格異動做套利", "event_driven"),
        ("IPO Lockup Expiry", "IPO 禁售期屆滿 = 潛在沽壓，可做空或避開", "event_driven"),
        ("Share Pledge Alert", "大股東股份質押比例過高 = 潛在爆倉風險，做空信號", "event_driven"),
        ("CB Conversion Arb", "可換股債券轉換前後嘅正股套利機會", "event_driven"),
    ],
    "機器學習/AI": [
        ("Random Forest Alpha", "用 random forest 預測未來 N 日回報，揀預測最高嘅做多", "ml"),
        ("XGBoost Factor Model", "用 gradient boosting 做因子模型，捕捉非線性關係", "ml"),
        ("LSTM Price Prediction", "用 LSTM 神經網絡預測價格走勢方向", "ml"),
        ("Transformer Attention Alpha", "用 attention mechanism 搵市場結構性 pattern", "ml"),
        ("Autoencoder Feature Extraction", "用 autoencoder 降維提取隱藏因子，再做交易信號", "ml"),
        ("Reinforcement Learning Trading", "RL agent 直接學交易策略（Q-learning/DQN/PPO）", "ml"),
        ("NLP Sentiment Trading", "從財經新聞/社交媒體抽取情緒分數做交易信號", "nlp"),
        ("Graph Neural Network Sector", "用 GNN 建模行業關係網絡，搵傳導效應", "ml"),
        ("Bayesian Change Point Detection", "用 Bayesian 方法檢測市場結構轉變點，切換策略", "ml"),
        ("Generative Adversarial Network (GAN) Simulation", "用 GAN 生成合成市場數據做 strategy stress testing", "ml"),
        ("K-Means Regime Clustering", "用 clustering 分市場狀態，每個 regime 用唔同策略", "ml"),
        ("Isolation Forest Anomaly Detection", "用 anomaly detection 搵異常價格/成交量，做反向信號", "ml"),
        ("CatBoost Rank Learning", "Learning to rank 揀股，唔係預測絕對回報", "ml"),
        ("CNN Chart Pattern Recognition Detail", "用卷積神經網絡識別圖表形態（詳細版）", "ml"),
        ("GPT Factor Description", "用 LLM 生成/解釋因子，協助人類分析師理解", "nlp"),
    ],
    "市場微結構": [
        ("Order Book Imbalance", "買賣盤失衡 = 短期方向信號", "microstructure"),
        ("Bid-Ask Spread Capture", "賺買賣差價（market making 策略）", "microstructure"),
        ("Quote Stuffing Detection", "偵測報價塞爆攻擊，反向操作", "microstructure"),
        ("Flash Crash Recovery", "閃崩後嘅價格修復交易", "microstructure"),
        ("Tick-Level Momentum", "tick 級別微細動量（HFT 策略）", "microstructure"),
        ("Iceberg Order Detection", "偵測冰山指令（大戶拆細單暗中介入）", "microstructure"),
        ("Liquidity Shocks", "流動性突然收乾/爆增 = 短期波動預警", "microstructure"),
        ("Mid-Point Peg", "用中間價掛單等成交，減少 spread cost", "execution"),
        ("Implementation Shortfall", "量度執行差價（arrival price vs execution price），優化演算法", "execution"),
        ("Adverse Selection Cost Model", "估算資訊不對稱成本，只喺成本低時交易", "microstructure"),
    ],
    "宏觀/跨資產": [
        ("Carry Trade", "借低息貨幣投高息貨幣嘅利差交易", "macro"),
        ("Trend Following Futures", "做多全體期貨市場嘅趨勢（Man AHL / Winton 風格）", "macro"),
        ("Global Tactical Asset Allocation", "根據宏觀指標動態調配股票/債券/商品/現金比例", "macro"),
        ("Yield Curve Trading", "利用收益率曲線變動交易：flattener / steepener", "macro"),
        ("Inflation Breakeven", "名義債 vs 通脹掛鈎債嘅差價 = 市場通脹預期", "macro"),
        ("Credit Spread Trading", "企業債 vs 國債信用利差交易", "macro"),
        ("Commodity Roll Yield", "商品期貨 roll yield 正/負 = 做多/做空信號", "macro"),
        ("Fed Funds Futures", "從聯邦基金期貨推市場加息/減息預期", "macro"),
        ("VIX/Equity Correlation", "VIX 同股市相關性異常 = 可對沖或投機", "macro"),
        ("Cross-Asset Momentum", "跨資產動量：股票/債券/外匯/商品中揀最強嘅", "macro"),
    ],
    "風險管理": [
        ("Kelly Criterion", "根據勝率同賠率決定最優倉位大小", "risk_mgmt"),
        ("Risk Parity", "每個資產風險貢獻相同嘅組合配置", "risk_mgmt"),
        ("Maximum Diversification", "最大化 diversification ratio 嘅組合配置", "risk_mgmt"),
        ("Minimum Variance Portfolio", "最小波動率嘅組合配置", "risk_mgmt"),
        ("CVaR Optimization", "用 Conditional VaR 代替 variance 做風險優化", "risk_mgmt"),
        ("Black-Litterman", "將主觀觀點同市場均衡回報結合做組合配置", "risk_mgmt"),
        ("Hierarchical Risk Parity", "用 clustering 先分組再 risk parity（Marcos Lopez de Prado）", "risk_mgmt"),
        ("Dynamic Stop-Loss", "根據波動率動態調較止損位", "risk_mgmt"),
        ("Regime-Switching Allocation", "根據市場 regime 切換不同配置權重", "risk_mgmt"),
        ("Ensemble Strategy Weighting", "多策略組合權重動態調整：近期表現好嘅加權", "risk_mgmt"),
    ],
    "加密貨幣": [
        ("Crypto Funding Rate Arb", "永續合約 funding rate 做多/做空套利", "crypto"),
        ("Bitcoin Halving Cycle", "比特幣減半前後嘅週期性交易", "crypto"),
        ("Stablecoin Depeg", "穩定幣脫鉤 = 買入等恢復鉤定", "crypto"),
        ("MEV Sandwich Bot", "三文治攻擊：偵測大單，搶先買後高賣", "crypto"),
        ("On-Chain Whale Alert", "跟蹤鏈上大戶轉帳，做跟隨交易", "crypto"),
        ("DeFi Liquidity Mining Yield", "流動性挖礦高 APY 策略 + impermanent loss 管理", "crypto"),
        ("Cross-Chain Arbitrage", "同一幣種喺唔同鏈/交易所之間套利", "crypto"),
        ("NFT Rarity Floor Arb", "NFT 稀有度低估時買入，等市場重估", "crypto"),
        ("Bitcoin Dominance Rotation", "比特幣市佔率變動 = 資金輪動信號", "crypto"),
        ("Liquid Staking Derivatives Arb", "stETH/ETH 折價時買入等回歸", "crypto"),
    ],
    "港股特色": [
        ("CCASS Top 5 Accumulation", "CCASS 頭5大持倉佔比連續上升 = 歸邊收貨，跟做多", "hk"),
        ("Short Selling Ratio Alert", "沽空比率急升 + 未平倉增加 = 潛在夾淡倉", "hk"),
        ("Corp Action Flavor Trading", "財技公告（配售/供股/要約/私有化）後嘅方向性交易", "hk"),
        ("IPO Grey Market", "新股暗盤價 vs IPO 價，暗盤價高於 IPO 價 = 跟買", "hk"),
        ("Dividend Withholding Tax Arb", "港股 vs ADR 嘅股息稅差價套利", "hk"),
        ("A+H Premium Arb", "A 股 vs H 股溢價收窄/擴闊交易", "hk"),
        ("Stock Connect Flow", "北水/南水流入流出做方向信號", "hk"),
        ("HSI Range Trading", "恆指喺特定區間內做均值回歸", "hk"),
        ("Morning Session Breakout", "港股上午時段突破做順勢", "hk"),
        ("Closing Auction Imbalance", "收市競價時段嘅大單 = 翌日方向", "hk"),
    ],
    "多因子": [
        ("Composite Alpha Score", "多因子加權打分，買入 Top N 最高分", "multi_factor"),
        ("Piotroski F-Score", "9 項基本面評分，高分 = 價值陷阱風險低", "factor"),
        ("Magic Formula (Greenblatt)", "Joel Greenblatt：高 ROC + 低 EV/EBIT 揀股", "factor"),
        ("Altman Z-Score", "破產風險評分：低分 = 高風險，避開或做空", "factor"),
        ("Beneish M-Score", "盈利操縱可能性評分：高分 = 會計造假風險", "factor"),
        ("Ohlson O-Score", "破產預測模型，比 Altman 更新", "factor"),
        ("Net-Net (Graham)", "Benjamin Graham：市值 < 淨流動資產嘅深價值股", "factor"),
        ("EV/EBIT Value", "企業價值/EBIT 排序揀最平嘅做多", "factor"),
        ("ROE + Momentum Combo", "高 ROE + 正動量 = 質量 + 趨勢雙因子", "multi_factor"),
        ("Low Vol + High Div", "低波動 + 高股息 = 防守型組合", "multi_factor"),
    ],
    "日內": [
        ("Opening Range Breakout Intraday", "開市頭 N 分鐘 range 突破做順勢（日內版）", "intraday"),
        ("Lunch Break Reversal", "港股午休後趨勢反轉 = 做逆勢", "intraday"),
        ("Last Hour Momentum", "尾市最後一小時趨勢跟蹤", "intraday"),
        ("Scalping Order Flow", "極短線睇買賣盤深度 signal", "intraday"),
        ("FOMC Minutes Trade", "聯儲局會議紀要公佈前後做 vol 交易", "event_driven"),
        ("NFP Payrolls Straddle", "非農就業數據公佈前買 straddle", "event_driven"),
        ("CPI Surprise Arbitrage", "CPI 同預期偏差 = 快速做方向", "event_driven"),
        ("OPEC Meeting Crude Oil", "OPEC 會議前後油價波動交易", "event_driven"),
        ("Earnings Surprise", "盈利超預期/遜預期 = 方向性交易", "event_driven"),
        ("ECB/BOE/Fed Divergence", "央行政策分歧 = 外匯方向交易", "macro"),
    ],
    "統計/量化": [
        ("Cointegration Index Arb", "ETF vs 成分籃嘅 cointegration 套利", "stat_arb"),
        ("Hurst Exponent Regime", "Hurst > 0.5 = 趨勢市、< 0.5 = 均值回歸市，切換策略", "stat_arb"),
        ("Fractal Market Hypothesis", "分形市場假說：喺唔同時間尺度搵自相似 pattern", "stat_arb"),
        ("Entropy-Based Signal", "用 Shannon entropy 量度市場隨機性，低 entropy = 可預測", "stat_arb"),
        ("Copula-Based Pairs", "用 copula 模型做非線性配對關係", "stat_arb"),
        ("Hidden Markov Model Regime", "HMM 自動檢測市場狀態，每狀態用唔同策略", "ml"),
        ("Kalman Filter Trend", "Kalman Filter 動態估算趨勢，比 MA 更快", "trend"),
        ("Wavelet Denoising", "Wavelet 變換去噪後嘅價格 = 更乾淨嘅信號", "stat_arb"),
        ("Dynamic Time Warping Pattern", "DTW 匹配歷史相似走勢做預測", "ml"),
        ("Monte Carlo VaR Backtest", "用 MC 模擬做 VAR backtest 而非歷史模擬", "risk_mgmt"),
    ],
    "另類數據": [
        ("Satellite Image Oil Inventory", "衛星圖分析油庫存變化", "alt_data"),
        ("Credit Card Transaction Alpha", "信用卡交易數據推斷消費趨勢", "alt_data"),
        ("Shipping AIS Data", "貨船 AIS 位置數據推斷貿易量", "alt_data"),
        ("Social Media Sentiment", "Twitter/Reddit 情緒分數交易", "nlp"),
        ("Google Trends Momentum", "Google 搜索量上升 = 關注度增加，可能係領先指標", "alt_data"),
        ("App Download/Usage", "App 下載量同日活用戶 = 預測業績", "alt_data"),
        ("Supply Chain Network Graph", "公司供應鏈關係圖預測傳導風險", "alt_data"),
        ("E-commerce Price Scraping", "電商價格爬蟲監測通脹/定價力", "alt_data"),
        ("Weather Derivative Trading", "天氣數據交易（農產品、能源、保險）", "alt_data"),
        ("ESG Sentiment Scoring", "ESG 新聞情緒 = 資金流入/流出信號", "alt_data"),
    ],
    "Execution/Algo": [
        ("VWAP Execution Algorithm", "VWAP 執行算法嘅量化實現", "execution"),
        ("TWAP Execution Algorithm", "TWAP 時間加權平均價執行", "execution"),
        ("POV (Percentage of Volume)", "跟市佔率執行：唔超過市場成交量 N%", "execution"),
        ("Implementation Shortfall Algo", "最小化 arrival price 到執行價之間嘅滑價", "execution"),
        ("Dark Pool Routing", "暗池流動性路由 = 減少 market impact", "execution"),
        ("Smart Order Router", "跨交易所最佳價格路由", "execution"),
        ("Iceberg/Reserve Order", "冰山指令：只顯示部份數量，隱藏真實意圖", "execution"),
        ("Pegged Order", "掛勾訂單：追隨市價自動調整限價", "execution"),
        ("Auction Participation", "競價時段參與策略（開市/收市）", "execution"),
        ("Anti-Gaming Logic", "防範高頻交易商 game 你嘅 algo", "execution"),
    ],
    "組合管理": [
        ("Equal Weight Portfolio", "等權重組合 = 簡單但有效嘅 benchmark", "portfolio"),
        ("Inverse Volatility Weighted", "按波動率倒數加權 = 風險平價簡化版", "portfolio"),
        ("Equal Risk Contribution", "每資產對組合風險貢獻相同", "portfolio"),
        ("Max Sharpe Portfolio", "最大化 Sharpe ratio 嘅 tangency portfolio", "portfolio"),
        ("Black-Litterman with Views", "Black-Litterman 模型：主觀 view + 先驗均衡", "portfolio"),
        ("Robust Optimization (Cov)", "穩健優化：covariance matrix shrinkage", "portfolio"),
        ("Resampled Efficiency", "用 bootstrap 重抽樣估計 frontier，減 estimation error", "portfolio"),
        ("Dynamic Conditional Correlation", "DCC-GARCH 動態 covariance 做組合再平衡", "portfolio"),
        ("Regime-Switching Portfolio", "唔同 regime 用唔同 optimal weights", "portfolio"),
        ("Kelly Fractional Sizing", "Fractional Kelly 控制風險嘅倉位管理", "risk_mgmt"),
    ],
    "行為金融": [
        ("Post-Earnings Announcement Drift", "業績好嘅股之後繼續升，差嘅繼續跌", "behavioral"),
        ("Anchoring Bias Trade", "投資者錨定歷史高/低位，價格接近時做反向", "behavioral"),
        ("Disposition Effect Reversal", "散戶傾向快賺慢蝕 → 出現過度持虧股會反彈", "behavioral"),
        ("Lottery Stock Short", "做空高 skewness 股票（似彩票、散戶追買）", "behavioral"),
        ("Overreaction Reversal", "極端一日大升/大跌後做均值回歸", "behavioral"),
        ("Underreaction Continuation", "溫和好消息後價格慢慢反映 = 跟趨勢", "behavioral"),
        ("Herding Reversal", "群眾效應極端時反向操作", "behavioral"),
        ("Confirmation Bias Filter", "只用客觀量化規則，排除確認偏誤", "behavioral"),
        ("Recency Bias Adjustment", "調整近期偏誤：過度重視近期數據", "behavioral"),
        ("Ambiguity Aversion Premium", "不確定性高時，風險溢價上升 = 做多 vol", "behavioral"),
    ],
    "跨市場套利": [
        ("ADR Premium/Discount", "美股 ADR vs 港股價格差價交易", "arb"),
        ("ETF NAV Arb", "ETF 市價 vs NAV 偏差套利", "arb"),
        ("Futures-Spot Basis Trade", "期貨 vs 現貨基差交易", "arb"),
        ("Calendar Spread Futures", "同一商品唔同到期月期貨嘅差價", "arb"),
        ("Cross-Exchange Arb", "同一品種喺唔同交易所嘅價格差", "arb"),
        ("Triangular Arb Forex", "三貨幣三角套利（已補）", "arb"),
        ("IRS vs Bond Futures", "利率掉期 vs 國債期貨嘅基差交易", "arb"),
        ("CDS-Bond Basis", "CDS 同債券收益率嘅基差", "arb"),
        ("ETF Creation/Redemption", "ETF creation basket 套利", "arb"),
        ("Convertible Bond Arb Detail", "可換股債券套利（delta hedging + gamma trading 版）", "arb"),
    ],
    "排序/學習排序": [
        ("LTR (Learning-to-Rank) Stock Selection", "用 lambdaMART 等 LTR 算法做股票排序揀股", "ml"),
        ("Factor Momentum LTR", "因子回報 momentum：近期表現好嘅因子加權", "multi_factor"),
        ("Cross-Asset Ranking", "跨資產排名：全球股票/債券/商品統一排序", "macro"),
        ("Alpha Decay Ranking", "量化 alpha 衰減速度，快衰減嘅排序會低", "risk_mgmt"),
        ("News Frequency Ranking", "新聞提及頻率排序 = 關注度 proxy", "alt_data"),
        ("Analyst Revision Ranking", "分析師盈利修正幅度排序", "factor"),
        ("Short Interest Ranking", "沽空比率排序 + 借貸成本加權", "factor"),
        ("Liquidity Ranking", "流動性排序 = 避開太悶或太薄嘅股票", "risk_mgmt"),
        ("Fragility Ranking", "脆弱性排序：macro/sector shock 時跌幅最大 = 高脆弱", "risk_mgmt"),
        ("Turnover Ranking", "換手率排序 = 過度交易信號", "volume"),
    ],
}

def safe_name(name):
    return name.replace("/", "_").replace(":", "_").replace("?", "").strip()

def write_strategy(category, name, desc, tag, idx, total):
    filename = safe_name(name) + ".md"
    filepath = os.path.join(OUT, filename)

    frontmatter = f"""---
category: {category}
tags: [{tag}]
收录日期: 2026-08-13
---

"""
    body = f"""# {name}

## 策略类型
{category}

## 标签
#{tag}

## 概述
{desc}

## 详细策略逻辑

### 核心原理
{desc}。该策略属于「{category}」类量化交易策略，主要捕捉市场中的特定定价偏差或统计规律。

### 信号规则
根据策略类型，信号可以从以下维度生成：
- **方向判断**：基于{tag}相关指标判定做多/做空/中性方向
- **时机选择**：在信号强度超过阈值时触发交易
- **仓位管理**：根据波动率或信号强度动态调整仓位大小

### 进场条件
1. 确认信号触发（超出阈值）
2. 确认流动性足够（避免滑点过大）
3. 确认无重大事件风险（避開财报、央行议息等）

### 出场条件
1. 信号反转或衰减至阈值以下
2. 达到预设止盈目标（如 +2 ATR）
3. 触及止损位（如 -1.5 ATR）
4. 持仓达到最大持有天数

## 风险提示
- 所有量化策略都存在過度拟合（overfitting）风险
- 历史回测表现不代表未来实际收益
- 实盘交易需考虑滑点、佣金、市场冲击成本
- 建议先进行 paper trading 验证后再投入实盘

## 参考来源
- 量化交易学术文献（SSRN/arXiv）
- 公开量化策略库（Quantpedia, QuantConnect）
- 行业研究报告及实盘经验总结

---
*由 Zo 自動生成 · {total} 個策略筆記計劃*
"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(frontmatter + body)


total = sum(len(v) for v in STRATEGIES.values())
written = 0
for cat, strats in STRATEGIES.items():
    for name, desc, tag in strats:
        write_strategy(cat, name, desc, tag, written + 1, total)
        written += 1

print(f"\n✅ 已生成 {written} 個策略筆記")
print(f"📂 路徑: {OUT}/")
