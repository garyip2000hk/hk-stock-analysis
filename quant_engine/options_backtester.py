"""options_backtester.py — 真期權策略回測引擎。

同 `batch_backtester.py` 嘅根本分別：

  batch_backtester.py   買賣**股票**，用技術指標做訊號（代理策略）
  options_backtester.py 買賣**期權合約**，用 HKEX 逐個行使價嘅
                        真實結算價計損益

數據源 `options_data/chain_history.parquet`（由 `chain_history.py` 建）：
每個交易日、每隻標的、每個到期月、每個行使價嘅 Call/Put 結算價、
IV、成交、未平倉。全部由 HKEX 官方日報拆出，唔係模型價。

宇宙 = 135 隻有期權嘅港股（`options_data/contract_specs.json`）。
非期權標的做期權策略冇意義，所以唔會出現喺呢個引擎。

損益計法：
  - 開倉用當日結算價（賣收 credit、買付 debit）**再扣每腿滑價**
  - 平倉用平倉日同一合約嘅結算價（真實市價，唔係模型重估）**再扣滑價**
  - 每張合約 × contract_size（每手股數）換成港元
  - 扣佣金 HK$8 + 交易所 HK$3 + 徵費 HK$0.6，每張每腿每次

⚠ 為咩要滑價（2026-08-17 補）：
HKEX 日報嘅 settle 係**結算價**，交易所用嚟計保證金嘅理論價，
通常比真實可成交價更靠中。之前呢個引擎只扣固定手續費，等於假設
每一腿都成交喺中價 —— 對多腿賣方結構特別致命：4 腿開平共跨 8 次價差，
每腿只食中價 3% 就已經蒸發 ~12% credit，剛好等於 MIN_CREDIT_RATIO
全部門檻。所以滑價唔係「保守一點」，佢決定策略存唔存在。

到期用內在值結算嘅腿**唔收滑價**（結算價係交易所定，冇價差要跨），
只有真正喺市場平倉嘅腿才跨價差。呢個分別會令 HOLD_TO_EXPIRY=True
嘅結果比 False 少一半滑價，係真實嘅。

Tier 標準同股票策略唔同：期權賣方策略勝率天然高、回報細，
所以 tier 主要睇 Sharpe + 平均風險回報 + 最壞單筆。

CLI:
    python3 options_backtester.py                    # 跑全部策略
    python3 options_backtester.py --strategy short_put
    python3 options_backtester.py --limit 20
    python3 options_backtester.py --slippage 0.03
    python3 options_backtester.py --sensitivity      # 0/1/3/5% 每腿滑價
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
sys.path.insert(0, str(ROOT))

import bs  # noqa: E402
import costs  # noqa: E402

CHAIN = ROOT / "options_data" / "chain_history.parquet"
SPECS = ROOT / "options_data" / "contract_specs.json"
OUT = BASE / "options_backtest_results.json"

# 費用按「每張合約、每條腿、每次開或平」計。之前用 HK$20 係櫃台價，
# 令 20+ 張嘅多腿倉（鐵鷹）費用食掉 23% 風險資金，結果「賣」同「買」
# 同一結構都同時錄負數 —— 純粹係費用假象，唔係市場結論。
# 現用實際零售費率：佣金 HK$8 + 交易所 HK$3 + 證監會徵費 HK$0.6。
COMMISSION_PER_LEG = 8.0      # 港元／張／腿
EXCHANGE_FEE = 3.0            # 港元／張／腿
SFC_LEVY = 0.6                # 港元／張／腿
STOCK_COST_PCT = 0.0018       # 股票腿單邊成本（印花稅 0.1% + 佣金／費用）
RISK_PER_TRADE = 20_000.0     # 每筆目標風險資金（港元）—— 決定做幾張
MAX_CONTRACTS = 50            # 封頂，避免便宜股做出不現實倉位
MIN_CONTRACTS = 1
HOLD_TO_EXPIRY = True         # True = 持到到期（用內在值結算，正確）
                              # False = 到期前 EXIT_DTE 日用真實結算價平倉
MIN_DTE, MAX_DTE = 20, 60     # 開倉時揀嘅到期月範圍
EXIT_DTE = 7                  # 剩幾日就平（避免到期日 gamma／行使風險）
STEP_DAYS = 5                 # 每幾個交易日開一次倉
MIN_LEG_OI = 100              # 每條腿最低未平倉
MIN_SETTLE = 0.01             # 結算價低過呢個當冇報價
MAX_EXIT_SHORTFALL = 5        # 平倉日最多可以早過目標幾日（防數據截斷假交易）
MAX_DELTA_DRIFT = 0.15        # 揀到嘅腳 |delta| 偏離目標超過呢個就唔開倉。
                              # 流動性篩選（OI ≥ 100）之後,鏈上可能只剩深價內合約,
                              # 「最接近 0.25」會揀到 delta 0.998 —— 咁就唔係
                              # 賣價外 Call,係變相沽空股票。03993 2025-11-24 正是
                              # 如此:只剩 strike 8.0 (spot 15.89),單筆 −$1,490,660。
MIN_CREDIT_RATIO = 0.12       # 收 credit 嘅結構:淨收入／翼寬低過呢個唔值得做。
                              # condor_engine.py 一直有呢個門檻,新引擎之前冇 ——
                              # 所以會做大量「收 $0.05、賭 $2.00」嘅負期望倉。
PARITY_MIN_EDGE_PCT = 0.01    # parity 偏離要大過現價幾 % 才做
STOCK_FEE_PCT = 0.0018        # 股票腿單邊成本（印花稅 0.1% + 佣金／交易費 ≈ 0.08%）
SLIPPAGE_PER_LEG = 0.03       # 每腿滑價（結算價比例）。同 condor_engine 同一口徑,
                              # 由 costs.CostModel 實作,兩個引擎唔會各有一套算法。
                              # 想由 Futu 實測 bid/ask 反推就用 costs.from_measured_spreads()


# ───────────────────────── 數據 ─────────────────────────

def load_specs() -> dict:
    return json.loads(SPECS.read_text()) if SPECS.exists() else {}


def load_chain(codes: list[str] | None = None) -> pd.DataFrame:
    if not CHAIN.exists():
        raise FileNotFoundError(
            f"{CHAIN} 未建立。先跑：python3 {ROOT}/chain_history.py --build")
    filters = [("stock_code", "in", [c.zfill(5) for c in codes])] if codes else None
    df = pd.read_parquet(CHAIN, filters=filters)
    df["date"] = pd.to_datetime(df["date"])
    df["expiry"] = pd.to_datetime(df["expiry"])
    return df


def _t(dte: int) -> float:
    return max(int(dte), 1) / 365.0


def _delta(spot: float, strike: float, dte: int, iv_pct: float, cp: str):
    if not iv_pct or iv_pct <= 0:
        return None
    g = bs.greeks(spot, strike, _t(dte), iv_pct / 100.0, cp)
    return g.get("delta") if g else None


def pick_expiry(day_df: pd.DataFrame) -> pd.Timestamp | None:
    """揀 DTE 落喺範圍內、流動性最好嘅到期月。"""
    cand = day_df[(day_df.dte >= MIN_DTE) & (day_df.dte <= MAX_DTE)]
    if cand.empty:
        return None
    g = cand.groupby("expiry").agg(v=("volume", "sum"), o=("oi", "sum"))
    return (g.v.fillna(0) + g.o.fillna(0) * 0.1).idxmax()


def pick_by_delta(legs: pd.DataFrame, spot: float, dte: int,
                  target: float, cp: str) -> pd.Series | None:
    """揀 |delta| 最接近 target 嘅合約（必須有 OI 同真實報價）。

    ⚠ 如果流動性篩選之後全條鏈只剩深價內合約,「最接近 target」會揀到
    delta 0.998 嘅腳當成 0.25 價外腳 —— 寧可唔開倉都唔可以當成同一個策略。
    """
    sub = legs[(legs.type == cp)
               & (legs.oi.fillna(0) >= MIN_LEG_OI)
               & (legs.settle.fillna(0) >= MIN_SETTLE)].copy()
    if sub.empty:
        return None
    sub["d"] = [abs(_delta(spot, k, dte, iv, cp) or 9)
                for k, iv in zip(sub.strike, sub.iv)]
    sub = sub[sub.d < 5]
    if sub.empty:
        return None
    pick = sub.iloc[(sub.d - target).abs().argsort().iloc[0]]
    if abs(float(pick.d) - target) > MAX_DELTA_DRIFT:
        return None
    return pick


def _credit_ok(legs: list[dict]) -> bool:
    """收 credit 嘅有翼結構:淨收入要佔翼寬 ≥ MIN_CREDIT_RATIO 才值得做。

    冇呢個門檻,引擎會照做「收 $0.05、最多蝕 $1.95」嘅倉。呢類倉即使勝率
    97% 都係負期望 —— 之前鐵鷹／價差錄大幅負期望,主因就係呢度。
    """
    opt = [L for L in legs if L["type"] != "S"]
    net = sum(-L["side"] * L["px"] * L.get("qty", 1) for L in opt)
    if net <= 0:
        return True                      # 淨付出（買方結構）唔適用
    widths = []
    for cp in ("C", "P"):
        shorts = [L for L in opt if L["type"] == cp and L["side"] == -1]
        longs = [L for L in opt if L["type"] == cp and L["side"] == +1]
        for sh in shorts:
            prot = [L for L in longs
                    if (L["strike"] > sh["strike"] if cp == "C"
                        else L["strike"] < sh["strike"])]
            if prot:
                widths.append(min(abs(L["strike"] - sh["strike"]) for L in prot))
    if not widths:
        return True                      # 裸賣冇翼寬,唔適用
    return net / max(widths) >= MIN_CREDIT_RATIO


def nearest_strike(legs: pd.DataFrame, target: float, cp: str) -> pd.Series | None:
    sub = legs[(legs.type == cp)
               & (legs.oi.fillna(0) >= MIN_LEG_OI)
               & (legs.settle.fillna(0) >= MIN_SETTLE)]
    if sub.empty:
        return None
    return sub.iloc[(sub.strike - target).abs().argsort().iloc[0]]


# ───────────────────────── 策略定義 ─────────────────────────
# 每個策略回一個 legs list：[{type, strike, side, px}]
# side: -1 = 賣（收 credit）, +1 = 買（付 debit）

def s_short_put(legs, spot, dte, **kw):
    """賣價外 Put（Cash-Secured Put）— 收波幅溢價，願意接貨。"""
    p = pick_by_delta(legs, spot, dte, 0.25, "P")
    if p is None:
        return None
    return [{"type": "P", "strike": float(p.strike), "side": -1, "px": float(p.settle)}]


def s_covered_call_proxy(legs, spot, dte, **kw):
    """賣價外 Call（裸賣／Covered Call 期權腿）。"""
    c = pick_by_delta(legs, spot, dte, 0.25, "C")
    if c is None:
        return None
    return [{"type": "C", "strike": float(c.strike), "side": -1, "px": float(c.settle)}]


def s_short_strangle(legs, spot, dte, **kw):
    """賣勒式 — 兩邊都賣，賭股價唔會大幅移動。"""
    c = pick_by_delta(legs, spot, dte, 0.20, "C")
    p = pick_by_delta(legs, spot, dte, 0.20, "P")
    if c is None or p is None:
        return None
    return [
        {"type": "C", "strike": float(c.strike), "side": -1, "px": float(c.settle)},
        {"type": "P", "strike": float(p.strike), "side": -1, "px": float(p.settle)},
    ]


def s_iron_condor(legs, spot, dte, **kw):
    """鐵鷹 — 賣勒式 + 兩邊買翼封頂風險。"""
    sc = pick_by_delta(legs, spot, dte, 0.20, "C")
    sp = pick_by_delta(legs, spot, dte, 0.20, "P")
    if sc is None or sp is None:
        return None
    gap = max(spot * 0.05, 0.5)
    lc = nearest_strike(legs[legs.strike > sc.strike], float(sc.strike) + gap, "C")
    lp = nearest_strike(legs[legs.strike < sp.strike], float(sp.strike) - gap, "P")
    if lc is None or lp is None:
        return None
    out = [
        {"type": "C", "strike": float(sc.strike), "side": -1, "px": float(sc.settle)},
        {"type": "C", "strike": float(lc.strike), "side": +1, "px": float(lc.settle)},
        {"type": "P", "strike": float(sp.strike), "side": -1, "px": float(sp.settle)},
        {"type": "P", "strike": float(lp.strike), "side": +1, "px": float(lp.settle)},
    ]
    return out if _credit_ok(out) else None


def s_bull_put_spread(legs, spot, dte, **kw):
    """牛市 Put 價差 — 看不跌，收 credit，風險封頂。"""
    sp = pick_by_delta(legs, spot, dte, 0.30, "P")
    if sp is None:
        return None
    gap = max(spot * 0.05, 0.5)
    lp = nearest_strike(legs[legs.strike < sp.strike], float(sp.strike) - gap, "P")
    if lp is None:
        return None
    out = [
        {"type": "P", "strike": float(sp.strike), "side": -1, "px": float(sp.settle)},
        {"type": "P", "strike": float(lp.strike), "side": +1, "px": float(lp.settle)},
    ]
    return out if _credit_ok(out) else None


def s_bear_call_spread(legs, spot, dte, **kw):
    """熊市 Call 價差 — 看不升，收 credit，風險封頂。"""
    sc = pick_by_delta(legs, spot, dte, 0.30, "C")
    if sc is None:
        return None
    gap = max(spot * 0.05, 0.5)
    lc = nearest_strike(legs[legs.strike > sc.strike], float(sc.strike) + gap, "C")
    if lc is None:
        return None
    out = [
        {"type": "C", "strike": float(sc.strike), "side": -1, "px": float(sc.settle)},
        {"type": "C", "strike": float(lc.strike), "side": +1, "px": float(lc.settle)},
    ]
    return out if _credit_ok(out) else None


def s_long_straddle(legs, spot, dte, **kw):
    """買跨式 — 賭大幅波動（方向唔重要）。IV 平時才有優勢。"""
    c = nearest_strike(legs, spot, "C")
    p = nearest_strike(legs, spot, "P")
    if c is None or p is None:
        return None
    return [
        {"type": "C", "strike": float(c.strike), "side": +1, "px": float(c.settle)},
        {"type": "P", "strike": float(p.strike), "side": +1, "px": float(p.settle)},
    ]


def s_long_strangle(legs, spot, dte, **kw):
    """買勒式 — 比跨式便宜，但要更大波動才賺。"""
    c = pick_by_delta(legs, spot, dte, 0.25, "C")
    p = pick_by_delta(legs, spot, dte, 0.25, "P")
    if c is None or p is None:
        return None
    return [
        {"type": "C", "strike": float(c.strike), "side": +1, "px": float(c.settle)},
        {"type": "P", "strike": float(p.strike), "side": +1, "px": float(p.settle)},
    ]


def s_put_ratio_spread(legs, spot, dte, **kw):
    """Put 比率價差 — 買 1 近價、賣 2 遠價外，淨收 credit。"""
    lp = pick_by_delta(legs, spot, dte, 0.40, "P")
    if lp is None:
        return None
    sp = pick_by_delta(legs[legs.strike < lp.strike], spot, dte, 0.18, "P")
    if sp is None:
        return None
    return [
        {"type": "P", "strike": float(lp.strike), "side": +1, "px": float(lp.settle)},
        {"type": "P", "strike": float(sp.strike), "side": -1, "px": float(sp.settle), "qty": 2},
    ]


def s_call_butterfly(legs, spot, dte, **kw):
    """Call 蝶式 — 賭股價停留喺現價附近，成本低、回報高。"""
    body = nearest_strike(legs, spot, "C")
    if body is None:
        return None
    gap = max(spot * 0.05, 0.5)
    lo = nearest_strike(legs[legs.strike < body.strike], float(body.strike) - gap, "C")
    hi = nearest_strike(legs[legs.strike > body.strike], float(body.strike) + gap, "C")
    if lo is None or hi is None:
        return None
    return [
        {"type": "C", "strike": float(lo.strike), "side": +1, "px": float(lo.settle)},
        {"type": "C", "strike": float(body.strike), "side": -1, "px": float(body.settle), "qty": 2},
        {"type": "C", "strike": float(hi.strike), "side": +1, "px": float(hi.settle)},
    ]


def s_jade_lizard(legs, spot, dte, **kw):
    """Jade Lizard — 賣 Put + 賣 Call 價差，上方零風險。"""
    sp = pick_by_delta(legs, spot, dte, 0.25, "P")
    sc = pick_by_delta(legs, spot, dte, 0.25, "C")
    if sp is None or sc is None:
        return None
    gap = max(spot * 0.05, 0.5)
    lc = nearest_strike(legs[legs.strike > sc.strike], float(sc.strike) + gap, "C")
    if lc is None:
        return None
    return [
        {"type": "P", "strike": float(sp.strike), "side": -1, "px": float(sp.settle)},
        {"type": "C", "strike": float(sc.strike), "side": -1, "px": float(sc.settle)},
        {"type": "C", "strike": float(lc.strike), "side": +1, "px": float(lc.settle)},
    ]


def s_synthetic_arb(legs, spot, dte, **kw):
    """Conversion / Reversal — 真正嘅 Put-Call Parity 套利。

    Parity: C − P = S − K·e^(−rt)。偏離時**三隻腳**一齊做才叫套利：
      Call 貴 (edge > 0) → 賣 Call + 買 Put + **買股** (conversion)
      Put  貴 (edge < 0) → 買 Call + 賣 Put + **沽股** (reversal)

    之前只做兩隻期權腳,冇股票對沖 —— 咁樣係「合成沽空／合成做多」,
    純方向性賭博,唔係套利。所以先會出現 21% 勝率、單筆 −$651,100:
    02899 由 34.44 跌到 27.68,合成多頭直接輸鏨,同 parity 偏離無關。
    加返股票腳之後,到期時三隻腳互相鎖死,盈虧 ≈ 開倉偏離 − 成本。
    """
    r = 0.035
    atm = nearest_strike(legs, spot, "C")
    if atm is None:
        return None
    k = float(atm.strike)
    c = legs[(legs.type == "C") & (legs.strike == k)]
    p = legs[(legs.type == "P") & (legs.strike == k)]
    if c.empty or p.empty:
        return None
    cpx, ppx = float(c.settle.iloc[0]), float(p.settle.iloc[0])
    if cpx < MIN_SETTLE or ppx < MIN_SETTLE:
        return None
    if (c.oi.fillna(0).iloc[0] < MIN_LEG_OI) or (p.oi.fillna(0).iloc[0] < MIN_LEG_OI):
        return None
    fair = spot - k * math.exp(-r * _t(dte))
    edge = (cpx - ppx) - fair
    if abs(edge) < spot * PARITY_MIN_EDGE_PCT:
        return None
    if edge > 0:
        return [
            {"type": "C", "strike": k, "side": -1, "px": cpx},
            {"type": "P", "strike": k, "side": +1, "px": ppx},
            {"type": "S", "strike": 0.0, "side": +1, "px": spot},
        ]
    return [
        {"type": "C", "strike": k, "side": +1, "px": cpx},
        {"type": "P", "strike": k, "side": -1, "px": ppx},
        {"type": "S", "strike": 0.0, "side": -1, "px": spot},
    ]


def s_calendar_spread(legs_all, spot, dte, **kw):
    """真日曆價差 — 賣近月 ATM、買遠月同一行使價。賺時間值差。"""
    near_exp = kw.get("expiry") or pick_expiry(legs_all)
    if near_exp is None:
        return None
    later = [e for e in sorted(legs_all.expiry.unique())
             if pd.Timestamp(e) > pd.Timestamp(near_exp)]
    if not later:
        return None
    near = legs_all[legs_all.expiry == near_exp]
    far = legs_all[legs_all.expiry == later[0]]
    n = nearest_strike(near, spot, "C")
    if n is None:
        return None
    f = far[(far.type == "C") & (far.strike == n.strike)
            & (far.oi.fillna(0) >= MIN_LEG_OI)
            & (far.settle.fillna(0) >= MIN_SETTLE)]
    if f.empty:
        return None
    return [
        {"type": "C", "strike": float(n.strike), "side": -1, "px": float(n.settle),
         "expiry": near_exp},
        {"type": "C", "strike": float(f.strike.iloc[0]), "side": +1,
         "px": float(f.settle.iloc[0]), "expiry": later[0]},
    ]


STRATEGIES = [
    # (key, 中文名, category, description, builder, 需要全部到期月)
    ("short_put", "賣價外 Put", "期權賣方",
     "賣 0.25 delta 價外 Put 收波幅溢價，願意以較低價接貨", s_short_put, False),
    ("short_call", "賣價外 Call", "期權賣方",
     "賣 0.25 delta 價外 Call，看股價唔會突破上方阻力", s_covered_call_proxy, False),
    ("short_strangle", "賣勒式", "期權賣方",
     "同時賣兩邊 0.20 delta，賭股價喺區間內橫行（無限風險）", s_short_strangle, False),
    ("iron_condor", "鐵鷹", "期權賣方",
     "賣勒式 + 兩邊買翼封頂，風險有限嘅收租策略", s_iron_condor, False),
    ("bull_put_spread", "牛市 Put 價差", "方向性價差",
     "賣 0.30 delta Put + 買更價外 Put，看不跌收 credit", s_bull_put_spread, False),
    ("bear_call_spread", "熊市 Call 價差", "方向性價差",
     "賣 0.30 delta Call + 買更價外 Call，看不升收 credit", s_bear_call_spread, False),
    ("long_straddle", "買跨式", "期權買方",
     "買 ATM Call + Put，賭大幅波動，IV 平時才有優勢", s_long_straddle, False),
    ("long_strangle", "買勒式", "期權買方",
     "買兩邊 0.25 delta，成本低過跨式但要更大波動", s_long_strangle, False),
    ("put_ratio", "Put 比率價差", "波幅套利",
     "買 1 張 0.40 delta Put、賣 2 張 0.18 delta Put，淨收 credit", s_put_ratio_spread, False),
    ("call_butterfly", "Call 蝶式", "波幅套利",
     "買低賣中買高，賭股價停留現價附近，風險回報比極高", s_call_butterfly, False),
    ("jade_lizard", "Jade Lizard", "期權賣方",
     "賣 Put + 賣 Call 價差，若 credit 大過翼寬則上方零風險", s_jade_lizard, False),
    ("parity_arb", "Put-Call Parity 套利", "真套利",
     "同一行使價 C−P 偏離 S−K·e^(−rt) 超過 1% 時反向鎖定價差", s_synthetic_arb, False),
    ("calendar_spread", "日曆價差", "真套利",
     "賣近月 ATM Call、買遠月同行使價，賺近月時間值衰減更快", s_calendar_spread, True),
]


# ───────────────────────── 回測核心 ─────────────────────────

def leg_value(row_df: pd.DataFrame, leg: dict) -> float | None:
    """喺某日嘅鏈度搵返**同一張**合約嘅結算價。

    一定要夾 expiry：同一個行使價喺唔同到期月都有合約，
    唔夾就會拎錯月份嘅價，損益完全失真。
    """
    if leg["type"] == "S":
        return float(row_df.close.iloc[0]) if len(row_df) else None
    m = row_df[(row_df.type == leg["type"])
               & (row_df.strike == leg["strike"])
               & (row_df.expiry == pd.Timestamp(leg["expiry"]))]
    if m.empty:
        return None
    v = m.settle.iloc[0]
    return float(v) if pd.notna(v) else None


def intrinsic(leg: dict, spot: float) -> float:
    if leg["type"] == "S":          # 股票腿:到期價值就係股價本身
        return spot
    if leg["type"] == "C":
        return max(0.0, spot - leg["strike"])
    return max(0.0, leg["strike"] - spot)


def bs_value(leg: dict, spot: float, as_of, iv_pct: float = 30.0) -> float:
    """未到期但喺平倉日找唔到報價嘅腿 —— 用 BS 重估，唔可以用內在值。

    用內在值等於強行扒光所有時間值，會系統性偷賣方、重手罰買方。
    """
    if leg["type"] == "S":
        return spot
    dte = max((pd.Timestamp(leg["expiry"]) - pd.Timestamp(as_of)).days, 1)
    try:
        v = bs.price(spot, leg["strike"], _t(dte), iv_pct / 100.0, leg["type"])
        if v is not None and pd.notna(v) and v >= 0:
            return float(v)
    except Exception:
        pass
    return intrinsic(leg, spot)


def _risk_per_contract(legs: list[dict], spot: float, contract_size: int) -> float:
    """每張合約嘅風險資金（港元）。

    有翼／有價差嘅結構：風險 = 最闊嘅翼寬 − 淨收入，封頂。
    裸賣（無翼）：無限風險，用「行使價 × 20% 保證金」近似券商要求。
    淨付出（買方）：風險就係付出嘅權利金。
    Conversion／Reversal（有股票腿）：三隻腳鎖死，風險 ≈ 資金佔用。
    日曆價差：短腿被同行使價嘅長腿（更遠月）蓋住，唔算裸賣。
    """
    has_stock = any(L["type"] == "S" for L in legs)
    opt = [L for L in legs if L["type"] != "S"]
    net = sum(-L["side"] * L["px"] * L.get("qty", 1) for L in legs)

    if has_stock:
        # 鎖倉套利：真正風險係股票資金佔用，唔係方向。
        return max(spot * contract_size, 1.0)

    widths = []
    for cp in ("C", "P"):
        shorts = [L for L in opt if L["type"] == cp and L["side"] == -1]
        longs = [L for L in opt if L["type"] == cp and L["side"] == +1]
        for sh in shorts:
            same_k_later = [L for L in longs
                            if L["strike"] == sh["strike"]
                            and pd.Timestamp(L.get("expiry", 0))
                            > pd.Timestamp(sh.get("expiry", 0))]
            if same_k_later:                 # 日曆價差：風險 = 淨付出
                widths.append(0.0)
                continue
            prot = [L for L in longs
                    if (L["strike"] > sh["strike"] if cp == "C"
                        else L["strike"] < sh["strike"])]
            if prot:
                widths.append(min(abs(L["strike"] - sh["strike"]) for L in prot))
            else:
                widths.append(None)          # 裸賣

    if net < 0:                              # 淨付出 → 買方，風險 = 權利金
        return max(abs(net) * contract_size, 1.0)

    if widths and all(w is not None for w in widths):
        width = max(widths)
        return max((width - net) * contract_size, contract_size * 0.01)

    # 裸賣或比率倉：用保證金近似
    strikes = [L["strike"] for L in opt if L["side"] == -1] or [spot]
    # ⚠ 地板：(strike*0.20 − net) 喺深價內時會變負數,之前跌到 contract_size*0.01,
    # 令 RISK_PER_TRADE // risk 撞到 MAX_CONTRACTS,一筆倉做足 50 張。
    # 裸賣真實風險同標的價掛鈎,所以最少要 spot * 10%。
    naive = (max(strikes) * 0.20 - net) * contract_size
    floor = spot * 0.10 * contract_size
    return max(naive, floor)


def _fill(mid: float, action: str, cm) -> float:
    """單腿成交價。action = 'sell'（我賣出，收 bid）或 'buy'（我買入，付 ask）。

    直接用 costs.CostModel，同 condor_engine 共用同一套算法。
    """
    side = "short" if action == "sell" else "long"
    return cm.fill(costs.Leg(mid=max(float(mid), 0.0), side=side), action)


def backtest_one(code: str, chain: pd.DataFrame, builder, needs_all_exp: bool,
                 contract_size: int, cm=None) -> list[dict]:
    """單隻標的、單個策略 — 逐 STEP_DAYS 開一次倉，跟到平倉。

    cm = costs.CostModel；None 就用 SLIPPAGE_PER_LEG 造一個。
    佣金／交易所費仍然由下面 per_leg 港元口徑處理（CostModel 只負責滑價），
    避免同一筆費用計兩次。
    """
    if cm is None:
        cm = costs.CostModel(slippage_per_leg=SLIPPAGE_PER_LEG,
                             commission_per_leg=0.0)
    days = sorted(chain.date.unique())
    trades: list[dict] = []

    for i in range(0, len(days) - 1, STEP_DAYS):
        d = days[i]
        day = chain[chain.date == d]
        if day.empty:
            continue
        spot = float(day.close.iloc[0])
        if not spot or spot <= 0:
            continue

        exp = pick_expiry(day)
        if exp is None:
            continue
        pool = day if needs_all_exp else day[day.expiry == exp]
        if pool.empty:
            continue
        dte = int(day[day.expiry == exp].dte.iloc[0])

        try:
            legs = builder(pool, spot, dte)
        except Exception:
            legs = None
        if not legs:
            continue

        # 每條腿必須記住自己嘅到期月，否則平倉時會錯配到另一個月
        for L in legs:
            L.setdefault("expiry", exp)
            if "iv" not in L:
                mk = day[(day.type == L["type"]) & (day.strike == L["strike"])
                         & (day.expiry == pd.Timestamp(L["expiry"]))]
                iv0 = mk.iv.iloc[0] if not mk.empty else None
                L["iv"] = float(iv0) if iv0 is not None and pd.notna(iv0) else None

        # 開倉現金流（每股）。open_cf_mid = 中價口徑，用嚟量化滑價蒸發咗幾多。
        open_cf = 0.0
        open_cf_mid = 0.0
        n_contracts = 0
        for L in legs:
            q = L.get("qty", 1)
            n_contracts += q
            open_cf_mid += -L["side"] * L["px"] * q      # 賣收正、買付負（中價）
            if L["type"] == "S":                          # 股票腿用 STOCK_COST_PCT
                open_cf += -L["side"] * L["px"] * q
            else:
                act = "sell" if L["side"] == -1 else "buy"
                open_cf += -L["side"] * _fill(L["px"], act, cm) * q

        exit_exp = pd.Timestamp(min(L["expiry"] for L in legs))

        if HOLD_TO_EXPIRY:
            # 持到到期：只需要標的到期日收市價，用內在值結算。
            # 呢個係唯一唔會偏袒買方或賣方嘅計法 —— 賣方真正收足全部
            # 時間值，買方亦要真正等到方向兌現。
            on_exp = [x for x in days if pd.Timestamp(x) >= exit_exp]
            if not on_exp:
                continue                      # 未到期，數據仲未有結果
            d_exit = on_exp[0]
            exit_day = chain[chain.date == d_exit]
            if exit_day.empty:
                continue
            exit_spot = float(exit_day.close.iloc[0])
            close_cf = 0.0
            close_cf_mid = 0.0
            stale = 0
            for L in legs:
                q = L.get("qty", 1)
                # 只有**真正到期**嘅腿才可以用內在值結算。
                # 日曆價差嘅遠月腿喺近月到期日仍然有時間值,
                # 用內在值等於當佢一文不值 —— 之前正係咁令 3,121 筆
                # 全部錄虧 (勝率 0.0%)，close_cf 每一筆都硬係 0。
                expired = pd.Timestamp(L["expiry"]) <= pd.Timestamp(d_exit)
                if expired:
                    v = intrinsic(L, exit_spot)
                else:
                    v = leg_value(exit_day, L)
                    if v is None:
                        v = bs_value(L, exit_spot, d_exit, L.get("iv") or 30.0)
                        stale += 1
                close_cf_mid += L["side"] * v * q
                # 到期內在值結算冇價差要跨；未到期嘅腿真要落市場平。
                if expired or L["type"] == "S":
                    close_cf += L["side"] * v * q
                else:
                    act = "buy" if L["side"] == -1 else "sell"
                    close_cf += L["side"] * _fill(v, act, cm) * q
        else:
            exit_target = exit_exp - pd.Timedelta(days=EXIT_DTE)
            later = [x for x in days if x > d and x <= exit_target]
            if not later:
                continue
            d_exit = later[-1]
            # 數據到尾時，只持倉一兩日嘅「交易」並不存在——那只是欄位不足。
            if (exit_target - pd.Timestamp(d_exit)).days > MAX_EXIT_SHORTFALL:
                continue
            exit_day = chain[chain.date == d_exit]
            if exit_day.empty:
                continue
            exit_spot = float(exit_day.close.iloc[0])
            close_cf = 0.0
            close_cf_mid = 0.0
            stale = 0
            for L in legs:
                q = L.get("qty", 1)
                v = leg_value(exit_day, L)
                if v is None:
                    # 未到期嘅腿冇報價時,用 BS 重估而唔係內在值:
                    # 內在值會抹走成個時間值,系統性偏袒賣方。
                    v = (intrinsic(L, exit_spot)
                         if pd.Timestamp(L["expiry"]) <= pd.Timestamp(d_exit)
                         else bs_value(L, exit_spot, d_exit, L.get("iv") or 30.0))
                    stale += 1
                close_cf_mid += L["side"] * v * q
                if L["type"] == "S":
                    close_cf += L["side"] * v * q
                else:
                    act = "buy" if L["side"] == -1 else "sell"
                    close_cf += L["side"] * _fill(v, act, cm) * q

        # ── 倉位大小 ──────────────────────────────────────────────
        # 之前每筆一律做 1 張，令固定佣金（HK$20/腿）主導結果：
        # 4 腿鐵鷹開平兩次 = HK$184 成本，而 1 張賺嘅毛利往往只有幾十蚊，
        # 所以連「賣」同「買」同一結構都同時錄負數 —— 純粹係手續費假象。
        # 真實落盤會按風險資金決定張數，所以呢度用同一邏輯。
        risk_per_ct = _risk_per_contract(legs, spot, contract_size)
        qty = int(max(MIN_CONTRACTS,
                      min(MAX_CONTRACTS, RISK_PER_TRADE // max(risk_per_ct, 1))))

        gross = (open_cf + close_cf) * contract_size * qty
        gross_mid = (open_cf_mid + close_cf_mid) * contract_size * qty
        slip_cost = gross_mid - gross          # 滑價蒸發咗幾多（港元）
        # 期權腿：每張每腿收佣金 + 交易所費 + 證監會徵費（開、平各一次）。
        # 股票腿（Conversion／Reversal）：唔收期權佣金，收百分比成本
        # （印花稅 0.1% × 2 邊 + 佣金 ~0.08%），按成交金額計。
        opt_legs = [L for L in legs if L["type"] != "S"]
        stk_legs = [L for L in legs if L["type"] == "S"]
        per_leg = COMMISSION_PER_LEG + EXCHANGE_FEE + SFC_LEVY
        fees = per_leg * len(opt_legs) * 2 * qty
        for L in stk_legs:
            notional = L["px"] * contract_size * qty * L.get("qty", 1)
            fees += notional * STOCK_COST_PCT * 2
        pnl = gross - fees

        margin = risk_per_ct * qty
        trades.append({
            "code": code,
            "open": str(pd.Timestamp(d).date()),
            "exit": str(pd.Timestamp(d_exit).date()),
            "dte": dte,
            "spot": round(spot, 3),
            "exit_spot": round(exit_spot, 3),
            "open_cf": round(open_cf, 4),
            "close_cf": round(close_cf, 4),
            "open_cf_mid": round(open_cf_mid, 4),
            "close_cf_mid": round(close_cf_mid, 4),
            "pnl": round(pnl, 1),
            "pnl_mid": round(gross_mid - fees, 1),   # 零滑價口徑（舊引擎等價）
            "slip_cost": round(slip_cost, 1),
            "fees": round(fees, 1),
            "margin": round(margin, 1),
            "qty": qty,
            "ret_pct": round(pnl / margin * 100, 2) if margin else None,
            "n_legs": len(legs),
            "stale_legs": stale,
        })

    return trades


def metrics_from_trades(trades: list[dict]) -> dict:
    if not trades:
        return {}
    t = pd.DataFrame(trades)
    pnl = t.pnl
    wins, losses = t[pnl > 0], t[pnl <= 0]
    rets = t.ret_pct.dropna()

    gross_win = float(wins.pnl.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.pnl.sum()) if len(losses) else 0.0
    pf = gross_win / gross_loss if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)

    # 以「每筆佔用資金回報」計 Sharpe（期權策略冇連續淨值曲線）
    sharpe = float(rets.mean() / rets.std()) if len(rets) > 2 and rets.std() else 0.0
    # 年化：每筆平均持倉 ≈ (MIN_DTE+MAX_DTE)/2 − EXIT_DTE 日
    per_year = 365 / max(((MIN_DTE + MAX_DTE) / 2) - EXIT_DTE, 1)
    sharpe_ann = round(sharpe * math.sqrt(per_year), 3)

    # 資金曲線（按開倉日排序累計）
    curve = t.sort_values("open").pnl.cumsum()
    peak = curve.cummax()
    dd = (curve - peak)
    worst_dd = float(dd.min()) if len(dd) else 0.0
    dd_pct = (worst_dd / peak.max() * 100) if peak.max() and peak.max() > 0 else 0.0

    return {
        "total_trades": len(t),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": round(len(wins) / len(t) * 100, 1),
        "total_pnl_hkd": round(float(pnl.sum()), 0),
        "avg_pnl_hkd": round(float(pnl.mean()), 0),
        "median_pnl_hkd": round(float(pnl.median()), 0),
        "avg_win": round(float(wins.pnl.mean()), 0) if len(wins) else 0.0,
        "avg_loss": round(float(losses.pnl.mean()), 0) if len(losses) else 0.0,
        "worst_trade": round(float(pnl.min()), 0),
        "best_trade": round(float(pnl.max()), 0),
        "profit_factor": round(pf, 2) if pf != float("inf") else 99.0,
        "avg_return_on_margin_pct": round(float(rets.mean()), 2) if len(rets) else 0.0,
        "sharpe_ratio": sharpe_ann,
        "max_drawdown_hkd": round(worst_dd, 0),
        "max_drawdown_pct": round(dd_pct, 2),
        "expectancy_hkd": round(float(pnl.mean()), 0),
        "stocks_traded": int(t.code.nunique()),
        "start_date": str(t.open.min()),
        "end_date": str(t.exit.max()),
        "avg_hold_days": round(float(
            (pd.to_datetime(t.exit) - pd.to_datetime(t.open)).dt.days.mean()), 1),
    }


def classify_tier(m: dict) -> str:
    """期權策略 tier：睇 Sharpe + 期望值 + 最壞單筆佔期望值幾多倍。"""
    if not m or not m.get("total_trades"):
        return "C"
    sharpe = m.get("sharpe_ratio", 0)
    exp_v = m.get("expectancy_hkd", 0)
    pf = m.get("profit_factor", 0)
    wr = m.get("win_rate_pct", 0)
    dd = abs(m.get("max_drawdown_pct", 100))

    if exp_v <= 0:
        return "C"
    if sharpe > 1.2 and pf > 1.5 and dd < 25:
        return "S"
    if sharpe > 0.7 and pf > 1.25 and dd < 40:
        return "A"
    if sharpe > 0.3 and pf > 1.05:
        return "B"
    return "C"


def composite(m: dict) -> float:
    if not m:
        return 0.0
    return round(
        m.get("sharpe_ratio", 0) * 30
        + min(m.get("profit_factor", 0), 5) * 12
        + m.get("win_rate_pct", 0) * 0.3
        + min(m.get("avg_return_on_margin_pct", 0), 30)
        + max(0, 25 - abs(m.get("max_drawdown_pct", 0))) * 1.5,
        1)


def run(keys: list[str] | None = None, verbose: bool = True,
        slippage: float | None = None) -> dict:
    cm = costs.CostModel(
        slippage_per_leg=SLIPPAGE_PER_LEG if slippage is None else slippage,
        commission_per_leg=0.0)
    specs = load_specs()
    chain = load_chain()
    codes = sorted(set(chain.stock_code.dropna()) & set(specs))
    # 靜靜咁跑出「零筆交易」係最危險嘅失敗方式 —— 冇 contract_specs.json
    # 時 codes 會係空集，成個回測會「成功」完成而每個策略都顯示冇成交，
    # 睇落好似策略唔成立，其實只係缺一個規格檔。所以要即刻叫停。
    if not specs:
        raise FileNotFoundError(
            f"{SPECS} 唔存在。先跑：python3 {ROOT}/contract_specs_hkex.py")
    if not codes:
        raise ValueError(
            f"chain 同 specs 冇交集（chain {chain.stock_code.nunique()} 隻、"
            f"specs {len(specs)} 隻）。多數係 hkats→stock_code 對照表未建："
            f"先跑 python3 {ROOT}/options_scraper.py --days 400")
    by_code = {c: g.copy() for c, g in chain.groupby("stock_code") if c in codes}

    if verbose:
        print(f"📊 真期權數據: {len(codes)} 隻期權標的 · "
              f"{chain.date.nunique()} 個交易日 · {len(chain):,} 條合約報價")
        print(f"   期間: {chain.date.min().date()} → {chain.date.max().date()}")
        print()

    todo = [s for s in STRATEGIES if keys is None or s[0] in keys]
    results = []

    for i, (key, name, cat, desc, builder, all_exp) in enumerate(todo, 1):
        if verbose:
            print(f"[{i}/{len(todo)}] {name} ({cat})...", end=" ", flush=True)
        all_trades: list[dict] = []
        for code in codes:
            cs = int(specs[code].get("contract_size") or 1000)
            try:
                all_trades += backtest_one(code, by_code[code], builder,
                                           all_exp, cs, cm)
            except Exception as e:
                if verbose:
                    print(f"\n    ⚠ {code}: {e}")

        m = metrics_from_trades(all_trades)
        tier = classify_tier(m)
        entry = {
            "key": key,
            "name": name,
            "category": cat,
            "description": desc,
            "engine": "options_real",
            "tier": tier,
            "composite_score": composite(m),
            "metrics": m,
            "params": {
                "min_dte": MIN_DTE, "max_dte": MAX_DTE,
                "exit_dte": EXIT_DTE, "step_days": STEP_DAYS,
                "min_leg_oi": MIN_LEG_OI,
                "commission_per_leg": COMMISSION_PER_LEG,
                "slippage_per_leg": cm.slippage_per_leg,
            },
            "trades_sample": sorted(all_trades,
                                    key=lambda x: x["open"])[-60:],
        }
        results.append(entry)

        if verbose:
            if m:
                emo = {"S": "🏆", "A": "✅", "B": "🟡", "C": "🔴"}[tier]
                print(f"{emo} {tier} | {m['total_trades']:>4} 筆 | "
                      f"勝率 {m['win_rate_pct']:>4.1f}% | "
                      f"期望 ${m['expectancy_hkd']:>7,.0f} | "
                      f"Sharpe {m['sharpe_ratio']:>5.2f} | "
                      f"PF {m['profit_factor']:.2f}")
            else:
                print("⚪ 冇成交（條件太嚴／流動性不足）")

    results.sort(key=lambda x: -x["composite_score"])
    for r, e in enumerate(results, 1):
        e["rank"] = r

    out = {
        "generated_at": pd.Timestamp.now().isoformat(),
        "engine": "options_real",
        "data_source": "HKEX Stock Options Daily Market Report (真實結算價)",
        "universe": "135 隻港股期權標的",
        "universe_count": len(codes),
        "trading_days": int(chain.date.nunique()),
        "date_range": [str(chain.date.min().date()), str(chain.date.max().date())],
        "chain_rows": int(len(chain)),
        "slippage_per_leg": cm.slippage_per_leg,
        "tier_counts": {t: sum(1 for r in results if r["tier"] == t)
                        for t in "SABC"},
        "results": results,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    if verbose:
        print(f"\n✅ {OUT}")
        print(f"   Tier: " + " ".join(f"{t}={out['tier_counts'][t]}" for t in "SABC"))
    return out


def sensitivity(keys: list[str] | None = None) -> dict:
    """0 / 1 / 3 / 5% 每腿滑價網格。判斷策略係唔係只存在於零成本假設。"""
    out = {}
    for s in costs.SENSITIVITY_GRID:
        r = run(keys, verbose=False, slippage=s)
        rows = []
        for e in r["results"]:
            m = e.get("metrics") or {}
            if not m.get("total_trades"):
                continue
            rows.append((e["name"], m["total_trades"], m["win_rate_pct"],
                         m["expectancy_hkd"], m["sharpe_ratio"], e["tier"]))
        out[f"{s * 100:.0f}%"] = rows

    names = [r[0] for r in out[f"{costs.SENSITIVITY_GRID[0] * 100:.0f}%"]]
    print(f"\n{'策略':<18}", end="")
    for k in out:
        print(f"{'滑價 ' + k:>22}", end="")
    print()
    print("-" * (18 + 22 * len(out)))
    for i, nm in enumerate(names):
        print(f"{nm:<18}", end="")
        for k in out:
            row = next((r for r in out[k] if r[0] == nm), None)
            if row is None:
                print(f"{'—':>22}", end="")
            else:
                cell = f"{row[2]:.0f}% / ${row[3]:,.0f} / {row[5]}"
                print(f"{cell:>22}", end="")
        print()
    print("\n判斷：3% 一欄期望值轉負，代表該策略只存在於零成本假設之下。")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="真期權策略回測")
    ap.add_argument("--strategy", help="只跑指定策略 key（逗號分隔）")
    ap.add_argument("--list", action="store_true", help="列出所有策略")
    ap.add_argument("--slippage", type=float, default=None,
                    help=f"每腿滑價（結算價比例），預設 {SLIPPAGE_PER_LEG}")
    ap.add_argument("--sensitivity", action="store_true",
                    help="跑 0/1/3/5%% 每腿滑價網格")
    a = ap.parse_args()

    if a.list:
        for k, n, c, d, _, _ in STRATEGIES:
            print(f"{k:<18} {n:<16} {c:<10} {d}")
        return
    keys = a.strategy.split(",") if a.strategy else None
    if a.sensitivity:
        sensitivity(keys)
        return
    run(keys, slippage=a.slippage)


if __name__ == "__main__":
    main()
