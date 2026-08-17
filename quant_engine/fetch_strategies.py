#!/usr/bin/env python3
"""
批量搵量化交易策略，寫入 Obsidian 量化日報
用 zo/ask API 並行搵，每批 10 個並行
"""
import asyncio, aiohttp, os, json, re, sys

API_URL = "https://api.zo.computer/zo/ask"
TOKEN = os.environ["ZO_CLIENT_IDENTITY_TOKEN"]
MODEL = "byok:193f8bf6-ad5d-4116-af0b-80d4dc044580"
VAULT = "/home/workspace/Desktop/Garysir/量化日報"
TARGET = 300

# 所有策略分類（每個分類會搵 5-10 個策略）
CATEGORIES = [
    # 動量類
    "trend following momentum strategies: dual moving average crossover, breakout with ATR filter, Donchian channel, turtle trading, momentum factor (12-1 month), sector rotation momentum",
    "mean reversion strategies: RSI mean reversion, Bollinger Band bounce, statistical mean reversion with z-score, pair cointegration, Ornstein-Uhlenbeck process, Kalman filter pairs",
    "breakout strategies: price breakout with volume confirmation, volatility breakout (Keltner), opening range breakout, range expansion, Darvas box, pivot point breakout",
    
    # 技術指標類
    "MACD-based strategies: MACD divergence, MACD histogram reversal, MACD zero-cross, MACD signal line crossover with volume filter",
    "RSI-based strategies: RSI divergence, RSI swing rejection, RSI trendline break, RSI with ADX filter, multi-timeframe RSI",
    "Bollinger Band strategies: Bollinger squeeze, band walk, %B oscillator, bandwidth expansion, Bollinger + RSI combo",
    "ADX/trend strength strategies: ADX breakout, directional movement index, trend strength filter with trailing stop",
    "Ichimoku strategies: Ichimoku cloud breakout, Kumo twist signal, Tenkan-Kijun cross, Ichimoku with volume confirmation",
    "Fibonacci strategies: Fibonacci retracement entry, Fibonacci extension target, Fibonacci fan, Fibonacci time zones, harmonic patterns (Gartley, Butterfly, Bat)",
    "candlestick pattern strategies: engulfing pattern, morning/evening star, hammer/hanging man, three white soldiers, doji reversal, harami pattern",
    
    # 因子類
    "value factor strategies: P/E quintile rotation, P/B value trap filter, EV/EBIT ranking, dividend yield capture, shareholder yield",
    "quality factor strategies: ROE-based stock selection, gross profitability, Piotroski F-Score, Altman Z-Score, accruals anomaly",
    "size factor strategies: small cap premium, micro-cap with liquidity filter, size factor timing",
    "low volatility strategies: minimum variance portfolio, low vol anomaly, volatility-weighted allocation, risk parity",
    "multi-factor model: Fama-French 3-factor, Carhart 4-factor, AQR-style factor combination, composite factor scoring",
    "dividend strategies: dividend growth investing, Dogs of the Dow, dividend capture around ex-date, DRIP optimization",
    
    # 統計套利類
    "statistical arbitrage: market-neutral pairs, distance-based pairs, copula-based pairs, basket trading, index arbitrage",
    "cointegration-based strategies: Engle-Granger pairs, Johansen test multi-asset, VECM-based trading, spread trading",
    "PCA-based strategies: eigenportfolios, residual trading, principal component regression, factor-residual decomposition",
    
    # 波動率類
    "volatility trading strategies: volatility risk premium harvesting, VIX term structure roll yield, variance swap replication, vol-of-vol trading",
    "options strategies: covered call, protective put, collar strategy, bull/bear spread, calendar spread, diagonal spread, iron butterfly, jade lizard, ratio spread, backspread",
    "volatility surface strategies: skew trading, term structure arbitrage, volatility smile arbitrage, dispersion trading, correlation trading",
    "gamma strategies: gamma scalping, long gamma + theta management, dynamic delta hedging, gamma squeeze detection",
    
    # 事件驅動類
    "earnings strategies: pre-earnings momentum, post-earnings drift, earnings surprise capture, whisper number gap, implied move vs realized",
    "M&A arbitrage: merger arbitrage spread, deal completion probability, regulatory risk adjustment, rumor-to-announcement capture",
    "insider trading signals: insider buying cluster, Form 4 filing analysis, insider transaction momentum, smart money following",
    "share buyback strategies: buyback announcement drift, repurchase yield ranking, buyback + insider combo signal",
    "index rebalancing: index inclusion effect, Russell reconstitution, MSCI rebalancing front-run, S&P 500 addition anticipation",
    "spin-off and restructuring: spin-off value unlock, corporate restructuring momentum, stub equity analysis, tracking stock mispricing",
    
    # 高頻/微結構類
    "order flow strategies: order book imbalance, trade flow toxicity (VPIN), large order detection, iceberg order detection, quote stuffing detection",
    "market making: symmetric market making with inventory control, Avellaneda-Stoikov model, adverse selection adjustment, rebate capture",
    "intraday patterns: opening gap fill, lunch hour mean reversion, last hour momentum, intraday VWAP reversion, time-of-day seasonality",
    
    # 機器學習類
    "ML stock selection: random forest feature importance, gradient boosted trees for return prediction, LSTM for time series, XGBoost multi-factor",
    "NLP strategies: news sentiment analysis, earnings call transcript analysis, social media momentum, analyst report NLP, Fed minutes parsing",
    "deep learning: autoencoder for anomaly detection, GAN for scenario generation, transformer for sequence prediction, CNN for chart patterns",
    "reinforcement learning: deep RL for portfolio allocation, Q-learning for trade execution, policy gradient for dynamic hedging",
    
    # 宏觀/跨資產類
    "macro strategies: yield curve positioning, credit spread timing, inflation regime switching, central bank policy reaction function",
    "carry trade: currency carry, commodity carry, volatility carry, dividend carry, funding cost arbitrage",
    "cross-asset momentum: asset class momentum rotation, risk-on/risk-off regime detection, cross-asset correlation breakdown, intermarket divergence",
    "global macro: purchasing power parity, Taylor rule positioning, current account imbalance, capital flow tracking",
    
    # 另類數據類
    "alternative data: satellite imagery (parking lots, crop), credit card transaction data, web scraping demand signals, app download tracking, job posting analysis",
    "sentiment strategies: news sentiment scoring, social media buzz, Google Trends signal, put/call ratio contrarian, AAII sentiment survey",
    
    # 風險管理類
    "risk management overlay: Kelly criterion position sizing, volatility targeting, drawdown control, tail risk hedging with OTM puts, risk budgeting",
    "portfolio construction: Black-Litterman model, hierarchical risk parity, equal risk contribution, max diversification, minimum correlation",
    
    # 市場特定
    "A-share specific: daily limit (漲停) follow-through, T+1 reversal, northbound flow signal, margin trading balance, dragon-tiger list analysis",
    "HK stock specific: CCASS concentration signal, short selling ratio, southbound flow, dual-listing AH premium, shell stock (殼股) screening",
    "crypto-specific: funding rate arbitrage, on-chain whale tracking, DeFi yield farming, DEX-CEX basis, hash rate momentum",
    
    "momentum crash hedging: momentum with volatility scaling, dynamic momentum lookback, residual momentum, momentum timing with credit spread",
    "tail risk strategies: tail risk parity, crash protection with VIX calls, drawdown-controlled momentum, max drawdown targeting",
    "seasonal strategies: sell in May, January effect, turn-of-month, holiday effect, Monday effect, September effect",
    "liquidity strategies: liquidity provision, illiquidity premium capture, Amihud illiquidity factor, volume-weighted signals",
    "real estate: REIT momentum, NAV discount trading, property cycle timing, cap rate spread",
    "commodity strategies: term structure roll yield, backwardation/contango carry, gold/silver ratio, oil crack spread, grain crush spread",
    "fixed income: yield curve trades (steepener/flattener), butterfly trades, credit carry, sovereign spread convergence, TIPS breakeven",
    "FX strategies: purchasing power parity mean reversion, momentum carry, real effective exchange rate, FX intervention detection",
]

# 用正則清理檔名
def sanitize(name):
    return re.sub(r'[\\/:*?"<>|]', '', name).strip()

async def fetch_category(session, cat_name, cat_prompt):
    prompt = f"""Research quantitative trading strategies in this category: {cat_prompt}

For EACH strategy, provide a Markdown document with this EXACT format (separate each strategy with ===STRATEGY===):

STRATEGY_NAME: <name>
CATEGORY: {cat_name}
DIFFICULTY: <Beginner|Intermediate|Advanced|Expert>
TIMEFRAME: <e.g. Intraday, Daily, Weekly, Monthly>
MARKET: <e.g. Stocks, Options, Futures, FX, Crypto, Multi-asset>
EDGE_SOURCE: <where does the alpha come from in 1-2 sentences>
RISK_FACTORS: <main risks in 1-2 sentences>

## 策略概述
<2-3 paragraphs explaining the strategy logic, what market inefficiency it exploits, and why it works>

## 入場條件
- <specific entry signal 1>
- <specific entry signal 2>
- <filter conditions>

## 出場條件
- <take profit condition>
- <stop loss condition>
- <time-based exit if applicable>

## 風險管理
- <position sizing method>
- <max drawdown target>
- <correlation considerations>

## 回測要點
- <backtest period recommendation>
- <key metrics to track: Sharpe, win rate, profit factor, max DD>
- <common pitfalls and biases to avoid>

## 參考資源
- <2-3 real academic papers or books that study this strategy>
===STRATEGY===

Generate 8 distinct strategies. Be specific with parameters, formulas, and real paper references. Each strategy should be actionable and distinct from others."""

    headers = {
        "authorization": TOKEN,
        "content-type": "application/json",
        "Accept": "application/json"
    }
    body = {"input": prompt, "model_name": MODEL}
    
    for attempt in range(3):
        try:
            async with session.post(API_URL, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                data = await resp.json()
                text = data.get("output", "")
                # 拆開每個策略
                parts = text.split("===STRATEGY===")
                strategies = []
                for part in parts:
                    part = part.strip()
                    if not part or "STRATEGY_NAME:" not in part:
                        continue
                    # 攞策略名
                    m = re.search(r"STRATEGY_NAME:\s*(.+)", part)
                    name = sanitize(m.group(1).strip()) if m.group(1) else "Unknown"
                    # 清理 markdown header
                    content = part.strip()
                    strategies.append((name, content))
                print(f"  ✓ {cat_name}: {len(strategies)} strategies")
                return strategies
        except Exception as e:
            print(f"  ⚠ {cat_name} attempt {attempt+1} failed: {e}")
            await asyncio.sleep(2)
    return []

async def main():
    # 讀現有策略名
    existing = set()
    for f in os.listdir(VAULT):
        if f.endswith('.md'):
            existing.add(f[:-3])  # 去 .md
    
    total_needed = TARGET - len(existing)
    print(f"現有: {len(existing)} | 目標: {TARGET} | 需要: {total_needed}")
    
    all_strategies = []
    # 分批並行（每批 8 個分類）
    batch_size = 8
    for i in range(0, len(CATEGORIES), batch_size):
        if len(all_strategies) >= total_needed:
            break
        
        batch = CATEGORIES[i:i+batch_size]
        # 為每個分類生成一個簡短嘅名稱
        cat_names = []
        for j, cat in enumerate(batch):
            # 取前 3 個詞做分類名
            words = cat.split()[:3]
            cat_names.append(" ".join(words))
        
        print(f"\n批次 {i//batch_size + 1}: 搵 {len(batch)} 個分類...")
        
        async with aiohttp.ClientSession() as session:
            tasks = [fetch_category(session, cn, cp) for cn, cp in zip(cat_names, batch)]
            results = await asyncio.gather(*tasks)
        
        for result in results:
            all_strategies.extend(result)
        
        # 寫入已搵到嘅策略
        written = 0
        for name, content in all_strategies[len(existing):]:
            if name in existing:
                continue
            filepath = os.path.join(VAULT, f"{name}.md")
            if os.path.exists(filepath):
                continue
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content + "\n")
            existing.add(name)
            written += 1
        
        total_now = len([f for f in os.listdir(VAULT) if f.endswith('.md')])
        print(f"  已寫入: {written} | 總計: {total_now}/{TARGET}")
        
        if total_now >= TARGET:
            break
        
        # 避免 rate limit
        await asyncio.sleep(1)
    
    final_count = len([f for f in os.listdir(VAULT) if f.endswith('.md')])
    print(f"\n✅ 完成！量化日報現有 {final_count} 個策略")
    if final_count >= TARGET:
        print(f"🎯 已達標 {TARGET} 個！")

if __name__ == "__main__":
    asyncio.run(main())
