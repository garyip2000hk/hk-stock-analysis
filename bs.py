"""bs.py — Black-Scholes 定價 + Greeks。

HKEX 每日報告有逐個行使價嘅結算價同 IV%，但冇 Greeks。
有咗呢個模組就可以：
  · 用 delta 揀行使價（0.25 delta short strike 等等）
  · 計策略嘅淨 Greeks（第 4 項動態對沖會用）
  · 計贏面（觸價概率 / 到期價外概率）

假設歐式期權、連續複利、股息用連續收益率 q 近似。
港股單一股票期權實際係美式，但用嚟揀行使價同估贏面已經夠準。
"""

from __future__ import annotations

import math

SQRT_2PI = math.sqrt(2 * math.pi)
RATE = 0.035          # HKD 無風險利率近似
TRADING_DAYS = 252


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _d1d2(s: float, k: float, t: float, vol: float,
          r: float, q: float) -> tuple[float, float]:
    sd = vol * math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * vol * vol) * t) / sd
    return d1, d1 - sd


def price(s: float, k: float, t: float, vol: float, cp: str,
          r: float = RATE, q: float = 0.0) -> float | None:
    """理論價。t = 年化年期，vol = 小數（0.35 = 35%）。"""
    if not (s and k and t and vol) or t <= 0 or vol <= 0:
        return None
    d1, d2 = _d1d2(s, k, t, vol, r, q)
    df, dq = math.exp(-r * t), math.exp(-q * t)
    if cp.upper().startswith("C"):
        return s * dq * _cdf(d1) - k * df * _cdf(d2)
    return k * df * _cdf(-d2) - s * dq * _cdf(-d1)


def greeks(s: float, k: float, t: float, vol: float, cp: str,
           r: float = RATE, q: float = 0.0) -> dict:
    """delta / gamma / theta（每日）/ vega（每 1 vol 點）/ rho。"""
    if not (s and k and t and vol) or t <= 0 or vol <= 0:
        return {}
    d1, d2 = _d1d2(s, k, t, vol, r, q)
    df, dq = math.exp(-r * t), math.exp(-q * t)
    sqt = math.sqrt(t)
    call = cp.upper().startswith("C")

    delta = dq * _cdf(d1) if call else -dq * _cdf(-d1)
    gamma = dq * _pdf(d1) / (s * vol * sqt)
    vega = s * dq * _pdf(d1) * sqt / 100
    if call:
        theta = (-s * dq * _pdf(d1) * vol / (2 * sqt)
                 - r * k * df * _cdf(d2) + q * s * dq * _cdf(d1)) / 365
        rho = k * t * df * _cdf(d2) / 100
    else:
        theta = (-s * dq * _pdf(d1) * vol / (2 * sqt)
                 + r * k * df * _cdf(-d2) - q * s * dq * _cdf(-d1)) / 365
        rho = -k * t * df * _cdf(-d2) / 100
    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
        "rho": round(rho, 4),
    }


def implied_vol(target: float, s: float, k: float, t: float, cp: str,
                r: float = RATE, q: float = 0.0) -> float | None:
    """由市價反解 IV（Brent 式二分，穩定過 Newton）。"""
    if not (target and s and k and t) or t <= 0 or target <= 0:
        return None
    lo, hi = 1e-4, 5.0
    for _ in range(100):
        mid = (lo + hi) / 2
        p = price(s, k, t, mid, cp, r, q)
        if p is None:
            return None
        if abs(p - target) < 1e-6:
            return mid
        if p > target:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def prob_below(s: float, k: float, t: float, vol: float,
               r: float = RATE, q: float = 0.0) -> float | None:
    """到期時股價 < K 嘅風險中性概率（= 沽權被行使概率）。"""
    if not (s and k and t and vol) or t <= 0 or vol <= 0:
        return None
    _, d2 = _d1d2(s, k, t, vol, r, q)
    return _cdf(-d2)


def prob_above(s: float, k: float, t: float, vol: float,
               r: float = RATE, q: float = 0.0) -> float | None:
    p = prob_below(s, k, t, vol, r, q)
    return None if p is None else 1 - p


def prob_touch(s: float, k: float, t: float, vol: float) -> float | None:
    """期內曾經觸及 K 嘅概率（約等於 2 × 到期越過概率）。"""
    p = prob_above(s, k, t, vol) if k > s else prob_below(s, k, t, vol)
    return None if p is None else min(1.0, 2 * p)


def expected_move(straddle_price: float, spot: float) -> float | None:
    """市場隱含事件跳幅（%）≈ ATM straddle 價 / 現價 × 0.85。

    0.85 係業界慣用調整（straddle 亦包含事件後嘅剩餘時間值）。
    """
    if not (straddle_price and spot):
        return None
    return straddle_price / spot * 0.85 * 100


def vol_to_move(vol_pct: float, days: int) -> float | None:
    """由年化波幅換算 N 個交易日嘅 1 標準差幅度（%）。"""
    if not vol_pct or days <= 0:
        return None
    return vol_pct * math.sqrt(days / TRADING_DAYS)


def yearfrac(days: int) -> float:
    return max(days, 0) / 365


if __name__ == "__main__":
    s, k, t, v = 479.2, 480.0, 22 / 365, 0.30
    print("call", round(price(s, k, t, v, "C"), 3))
    print("put ", round(price(s, k, t, v, "P"), 3))
    print("greeks", greeks(s, k, t, v, "C"))
    print("P(<K)", round(prob_below(s, k, t, v), 3))
    print("IV back-out", round(implied_vol(price(s, k, t, v, "C"), s, k, t, "C"), 4))
