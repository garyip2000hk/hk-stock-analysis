"""costs.py — 交易成本模型（滑價 + 佣金 + 交易所費）。

審查報告 A4：原本嘅回測直接用 HKEX 每日報告嘅 **結算價（settlement price）**
相加減做 credit。結算價唔係可成交價 —— 佢係交易所用嚟計保證金嘅理論價，
通常落在 bid/ask 中間甚至更靠中。真實落單要跨價差：

    賣一腳 → 成交在 bid（低過中價）
    買一腳 → 成交在 ask（高過中價）

Iron Condor 有 4 條腳，開倉跨 4 次，平倉再跨 4 次，共 8 次。
若每腳只食中價嘅 3%，開倉已經蒸發 credit 嘅 ~12% —— 剛好等於
`condor_engine.MIN_CREDIT_RATIO = 0.12` 全部門檻。所以呢個模組唔係
「錦上添花」，佢係判斷策略存唔存在嘅核心。

用法：

    import costs
    cm = costs.CostModel(slippage_per_leg=0.03, commission_per_leg=0.0)
    credit = cm.open_credit([
        costs.Leg(mid=12.5, side="short"),   # 賣 call
        costs.Leg(mid=8.0,  side="short"),   # 賣 put
        costs.Leg(mid=4.2,  side="long"),    # 買 call wing
        costs.Leg(mid=2.8,  side="long"),    # 買 put wing
    ])
    cost  = cm.close_cost(legs_at_exit)      # 平倉要付幾多
    pnl   = credit - cost

所有金額單位 = **每股**（同 `options_chain` 嘅 settle 同一單位），
未乘合約乘數。跨股票比較真實金額之前必須乘 lot size（見 README「已知缺口」）。

CLI:
    python3 costs.py --sensitivity 12.0 100.0     # credit=12, width=100
    python3 costs.py --from-live                  # 由 Futu 實測價差反推滑價
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

BASE = Path(__file__).parent
LIVE_CHAIN = BASE / "options_data" / "chain_live.parquet"

# 港交所股票期權雜費（每張合約，2026 年水平；只作預設，實際跟你券商單）
# 交易所費 + 證監會徵費，用「每股」表達需要除以合約乘數，所以呢度用
# EXCHANGE_FEE_PER_CONTRACT，只在 per_contract() 出現，回測嘅 per-share
# 口徑用 commission_per_leg 統一代表。
EXCHANGE_FEE_PER_CONTRACT = 3.5
SFC_LEVY_PER_CONTRACT = 0.6

SENSITIVITY_GRID = (0.0, 0.01, 0.03, 0.05)


@dataclass(frozen=True)
class Leg:
    """一條腳。mid = 中價（結算價或 (bid+ask)/2），side = short（賣）/ long（買）。"""

    mid: float
    side: str

    def __post_init__(self) -> None:
        if self.side not in {"short", "long"}:
            raise ValueError(f"side 只可以係 short 或 long，收到 {self.side!r}")


@dataclass(frozen=True)
class CostModel:
    """滑價 + 佣金模型。

    slippage_per_leg
        每腳滑價，表達為**中價嘅比例**。0.03 = 成交價比中價差 3%。
        用比例而唔用絕對值，因為深度價外嘅腳中價細，絕對價差反而更闊；
        比例係較保守嘅折衷。要更嚴謹就用 `from_measured_spreads()`
        由 Futu 實測 bid/ask 反推。
    commission_per_leg
        每腳每股佣金（絕對金額，同 credit 同單位）。
    min_fill
        成交價下限，避免中價極細時滑價後變負數。
    """

    slippage_per_leg: float = 0.03
    commission_per_leg: float = 0.0
    min_fill: float = 0.001

    def fill(self, leg: Leg, action: str) -> float:
        """單腳成交價。action = 'sell'（我賣出）或 'buy'（我買入）。"""
        if action == "sell":
            px = leg.mid * (1.0 - self.slippage_per_leg)
        elif action == "buy":
            px = leg.mid * (1.0 + self.slippage_per_leg)
        else:
            raise ValueError(f"action 只可以係 buy 或 sell，收到 {action!r}")
        return max(px, self.min_fill)

    def open_credit(self, legs: list[Leg]) -> float:
        """開倉淨收入：賣腳收 bid、買腳付 ask，再扣每腳佣金。"""
        got = sum(self.fill(l, "sell") for l in legs if l.side == "short")
        paid = sum(self.fill(l, "buy") for l in legs if l.side == "long")
        return got - paid - self.commission_per_leg * len(legs)

    def close_cost(self, legs: list[Leg]) -> float:
        """平倉淨支出：買返短腳（付 ask）、賣走長腳（收 bid），再加佣金。

        回傳值可以係負數（即係平倉仲有錢收），例如長腳已經變 ITM。
        """
        paid = sum(self.fill(l, "buy") for l in legs if l.side == "short")
        got = sum(self.fill(l, "sell") for l in legs if l.side == "long")
        return paid - got + self.commission_per_leg * len(legs)

    def expiry_cost(self, n_legs: int = 4) -> float:
        """持到到期：冇平倉滑價，但仍有結算／行使成本（保守只計佣金）。

        注意：港股個股期權係**美式 + 實物交收**，短腳被行使要真金收／交股票。
        呢個函數只反映現金費用，唔反映交收風險 —— 見 README「已知缺口」。
        """
        return self.commission_per_leg * n_legs

    def round_trip_drag(self, legs: list[Leg]) -> float:
        """一開一平總共蒸發幾多（相對零成本基準）。用嚟做 sensitivity。"""
        zero = CostModel(0.0, 0.0, self.min_fill)
        gross = zero.open_credit(legs) - zero.close_cost(legs)
        net = self.open_credit(legs) - self.close_cost(legs)
        return gross - net


ZERO_COST = CostModel(slippage_per_leg=0.0, commission_per_leg=0.0)
DEFAULT_COST = CostModel(slippage_per_leg=0.03, commission_per_leg=0.0)


def grid(commission_per_leg: float = 0.0) -> dict[str, CostModel]:
    """Sensitivity 用嘅一組模型：0% / 1% / 3% / 5% per leg。"""
    return {
        f"{s*100:.0f}%": CostModel(slippage_per_leg=s,
                                   commission_per_leg=commission_per_leg)
        for s in SENSITIVITY_GRID
    }


def from_measured_spreads(df=None, quantile: float = 0.5,
                          owner_code: str | None = None) -> CostModel | None:
    """由 `futu_option_chain.py` 收集嘅實測 bid/ask 反推 slippage_per_leg。

    半個價差 ÷ 中價 = 由中價行到 bid（或 ask）要付嘅比例，正好就係
    `slippage_per_leg` 嘅定義。取中位數（可改 quantile=0.75 更保守）。

    冇實測數據就回傳 None —— 唔會靜靜地用一個假設值當實測。
    """
    import pandas as pd

    if df is None:
        if not LIVE_CHAIN.exists():
            return None
        df = pd.read_parquet(LIVE_CHAIN)
    if df is None or len(df) == 0:
        return None
    if owner_code is not None:
        df = df[df.owner_code.astype(str).str.zfill(5) == str(owner_code).zfill(5)]
    need = {"bid", "ask"}
    if not need.issubset(df.columns) or df.empty:
        return None

    d = df.dropna(subset=["bid", "ask"])
    d = d[(d.bid > 0) & (d.ask > d.bid)]
    if d.empty:
        return None
    mid = (d.bid + d.ask) / 2.0
    half = (d.ask - d.bid) / 2.0 / mid
    s = float(half.quantile(quantile))
    return CostModel(slippage_per_leg=round(s, 4))


def _fmt_sensitivity(credit: float, width: float,
                     commission_per_leg: float = 0.0) -> str:
    """用一張典型 condor 展示滑價點樣食掉 edge。

    假設四條腳嘅中價比例 = 短腳各佔 credit 嘅 ~70%、長腳各 ~20%
    （即 0.7+0.7-0.2-0.2 = 1.0 × credit），呢個係港股 0.20 delta
    condor 嘅粗略形狀。真實形狀請用 `condor_engine --sensitivity`。
    """
    legs = [
        Leg(mid=credit * 0.70, side="short"),
        Leg(mid=credit * 0.70, side="short"),
        Leg(mid=credit * 0.20, side="long"),
        Leg(mid=credit * 0.20, side="long"),
    ]
    out = [
        f"典型 Iron Condor：中價 credit {credit:.2f} / 翼寬 {width:.2f} "
        f"（credit ratio {credit/width*100:.1f}%）",
        "",
        f"{'每腳滑價':>10} {'實收 credit':>13} {'剩餘 ratio':>12} "
        f"{'一開一平蒸發':>14} {'邊際':>8}",
    ]
    for name, cm in grid(commission_per_leg).items():
        net = cm.open_credit(legs)
        drag = cm.round_trip_drag(legs)
        ratio = net / width * 100
        verdict = "✓" if ratio >= 12.0 else ("薄" if ratio > 6 else "✗ 冇 edge")
        out.append(f"{name:>10} {net:>13.3f} {ratio:>11.1f}% "
                   f"{drag:>14.3f} {verdict:>8}")
    out += [
        "",
        "判斷：3% 一欄嘅『剩餘 ratio』若跌穿 MIN_CREDIT_RATIO（12%），",
        "代表門檻本身已經被成本吞掉，策略在該成本假設下唔存在。",
    ]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="交易成本模型")
    ap.add_argument("--sensitivity", nargs=2, type=float,
                    metavar=("CREDIT", "WIDTH"), help="展示滑價敏感度")
    ap.add_argument("--commission", type=float, default=0.0,
                    help="每腳每股佣金")
    ap.add_argument("--from-live", action="store_true",
                    help="由 options_data/chain_live.parquet 實測價差反推滑價")
    ap.add_argument("--stock", help="--from-live 時只計某隻標的")
    a = ap.parse_args()

    if a.from_live:
        cm = from_measured_spreads(owner_code=a.stock)
        if cm is None:
            print("冇實測數據。先跑：python3 futu_option_chain.py --snapshot")
            return
        print(f"實測中位半價差 → slippage_per_leg = {cm.slippage_per_leg:.4f} "
              f"（{cm.slippage_per_leg*100:.2f}% per leg）")
        for q in (0.25, 0.5, 0.75, 0.9):
            c = from_measured_spreads(quantile=q, owner_code=a.stock)
            if c:
                print(f"  q{int(q*100):>2} → {c.slippage_per_leg*100:>6.2f}%")
        return

    if a.sensitivity:
        print(_fmt_sensitivity(a.sensitivity[0], a.sensitivity[1], a.commission))
        return

    ap.print_help()


if __name__ == "__main__":
    main()
