"""
strategy_registry.py — 註冊所有量化策略，統一載入
"""
from strategies.momentum import MomentumStrategy
from strategies.momentum_cross import MomentumCross
from strategies.momentum_strategy import MomentumStrategy as MomentumStrategyAlt
from strategies.ma_cross import MACrossStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.mean_reversion_strategy import MeanReversionStrategy as MeanRevAlt
from strategies.mean_revert_bb import MeanRevertBB
from strategies.rsi_macd import RSIMACDStrategy
from strategies.rsi_strategy import RSIStrategy
from strategies.breakout import BreakoutStrategy
from strategies.volatility_breakout import VolatilityBreakoutStrategy
from strategies.volume_breakout import VolumeBreakout
from strategies.volume_breakout_strategy import VolumeBreakoutStrategy as VolBreakAlt
from strategies.gap_reversion import GapReversion
from strategies.trend_follow_atr import TrendFollowATR
from strategies.short_squeeze import ShortSqueeze
from strategies.smart_money import SmartMoney
from strategies.obv_divergence import OBVDivergence
from strategies.vwap_mean_reversion import VWAPMeanReversion
from strategies.vwap_reversion import VWAPReversion
from strategies.ccass_concentration import CCASSConcentration
from strategies.ccass_momentum import CCASSMomentum


from strategies.opt_gamma_squeeze import GammaSqueeze
from strategies.opt_iv_reversion import IVReversion
from strategies.arb_stat_pairs import StatPairsArb
from strategies.opt_vol_breakout import VolBreakout
from strategies.arb_calendar_spread import CalendarSpreadArb


from strategies.opt_earnings_runup_proxy import EarningsRunupProxyStrategy
from strategies.arb_dividend_capture_proxy import DividendCaptureArbProxy
from strategies.opt_put_call_parity_proxy import PutCallParityArbProxy
from strategies.opt_delta_hedge_proxy import DeltaHedgeRebalanceProxy
from strategies.opt_iv_skew_proxy import IVSkewTradingProxy


def get_all_strategies():
    """Return list of (name, strategy_class, category, description) tuples."""
    return [
        ("動量選股", MomentumStrategy, "動量", "買入過去 N 日漲幅最大的 Top K 隻股票"),
        ("橫截面動量", MomentumCross, "動量", "同行業中近期表現最強的做多"),
        ("MA 交叉", MACrossStrategy, "趨勢", "短期均線上穿長期均線時買入"),
        ("均值回歸", MeanReversionStrategy, "均值回歸", "價格偏離均值時反向交易"),
        ("布林通道回歸", MeanRevertBB, "均值回歸", "價格觸及布林通道下軌時買入"),
        ("RSI+MACD", RSIMACDStrategy, "技術指標", "RSI 超賣回升 + MACD 金叉同時出現"),
        ("RSI 選股", RSIStrategy, "技術指標", "RSI < 30 超賣買入，RSI > 70 超買賣出"),
        ("突破策略", BreakoutStrategy, "突破", "價格突破 N 日高位時買入"),
        ("波動率突破", VolatilityBreakoutStrategy, "突破", "ATR × N 倍突破通道 + 成交量確認"),
        ("成交量突破", VolumeBreakout, "成交量", "成交量放大 + 價格突破 = 入場信號"),
        ("缺口回歸", GapReversion, "缺口", "跳空缺口出現後等待回補"),
        ("ATR 趨勢跟蹤", TrendFollowATR, "趨勢", "ATR 動態止損的趨勢跟蹤系統"),
        ("夾淡倉", ShortSqueeze, "事件驅動", "沽空比率極高 + 成交量放大 = 夾淡倉信號"),
        ("聰明錢", SmartMoney, "資金流", "大戶暗中收貨的成交量模式識別"),
        ("OBV 背離", OBVDivergence, "成交量", "價格升但 OBV 跌 = 看空背離"),
        ("VWAP 均值回歸", VWAPMeanReversion, "日內", "價格遠離 VWAP 時做均值回歸"),
        ("VWAP 回歸", VWAPReversion, "日內", "VWAP 回歸策略（簡化版）"),
        ("CCASS 集中度", CCASSConcentration, "資金流", "CCASS 持倉集中度變化識別大戶動作"),
        ("CCASS 動量", CCASSMomentum, "資金流", "CCASS 持倉動量變化識別趨勢"),
        ("Gamma Squeeze (代理)", GammaSqueeze, "波動率/期權", "價格急升且成交量放大，模擬做市商被迫追入 Delta"),
        ("引伸波幅回歸 (代理)", IVReversion, "波動率/期權", "以 ATR 偏離歷史均值作為波幅過高指標，反向操作做空波幅"),
        ("統計套利 (代理)", StatPairsArb, "跨市場套利", "尋找短期大幅落後於大市的股票進行均值回歸"),
        ("波幅突破 (代理)", VolBreakout, "波動率/期權", "波幅壓縮至極點後出現實體陽燭"),
        ("跨期套利 (代理)", CalendarSpreadArb, "跨市場套利", "長期趨勢不變但短期極度超買/超賣"),
        ("業績前異動 (代理)", EarningsRunupProxyStrategy, "波動率/期權", "尋找近 3 個月內累積回報最高且近期成交穩步放大的股票"),
        ("收息套利 (代理)", DividendCaptureArbProxy, "跨市場套利", "尋找波動率低且處於穩定上升通道的股票"),
        ("Put-Call Parity 套利 (代理)", PutCallParityArbProxy, "跨市場套利", "跌破布林下軌且成交量萎縮的極端情況"),
        ("Delta 對沖重平衡 (代理)", DeltaHedgeRebalanceProxy, "波動率/期權", "偵測價格順勢且成交量極大的 K 線"),
        ("IV Skew 交易 (代理)", IVSkewTradingProxy, "波動率/期權", "連續多日小幅下跌但未破前低，模擬反向賣出 Put"),
    ]
