"""tests/test_core.py — Stage 0 核心數學同邏輯測試。

呢個 repo 冇 `options_data/`、`imported/quotes.json` 喺版本控制內，
所以全部測試都用**合成數據**同 monkeypatch，唔依賴任何本機資料。

跑法：
    pytest tests/ -q
"""

from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bs                    # noqa: E402
import chain_cache as cc     # noqa: E402
import condor_engine as ce    # noqa: E402
import costs                 # noqa: E402
import portfolio as pf       # noqa: E402
import vrp_engine as ve      # noqa: E402


# ─────────────────────────────────────────────────────────────
# bs.py
# ─────────────────────────────────────────────────────────────
def test_put_call_parity():
    """C − P = S·e^(−qT) − K·e^(−rT)。唔成立就代表定價有系統性錯。"""
    s, k, t, vol = 100.0, 95.0, 0.25, 0.30
    c = bs.price(s, k, t, vol, "C")
    p = bs.price(s, k, t, vol, "P")
    rhs = s - k * math.exp(-bs.RATE * t)
    assert c - p == pytest.approx(rhs, abs=1e-8)


@pytest.mark.parametrize("k", [80.0, 100.0, 125.0])
@pytest.mark.parametrize("cp", ["C", "P"])
def test_implied_vol_roundtrip(k, cp):
    """price → implied_vol → 應該還原原本 vol。"""
    s, t, vol = 100.0, 0.15, 0.42
    px = bs.price(s, k, t, vol, cp)
    iv = bs.implied_vol(px, s, k, t, cp)
    assert iv == pytest.approx(vol, abs=1e-3)


def test_delta_monotone_and_bounded():
    s, t, vol = 100.0, 0.25, 0.3
    ds = [bs.greeks(s, k, t, vol, "C")["delta"] for k in (80, 100, 120)]
    assert ds[0] > ds[1] > ds[2]
    assert all(0 < d < 1 for d in ds)
    pd_ = bs.greeks(s, 100, t, vol, "P")["delta"]
    assert -1 < pd_ < 0


# ─────────────────────────────────────────────────────────────
# vrp_engine.realized_vol_forward 對齊
# ─────────────────────────────────────────────────────────────
def test_realized_vol_forward_is_actually_forward():
    """t 嘅前瞻波幅必須只用 t 之後嘅回報 —— 唔可以偷望過去。

    構造：前 200 日日回報 0.5%，之後 200 日 2.0%。
    喺切換點之前一日，前瞻波幅應該已經完全反映高波幅段。
    """
    n, h = 400, 21
    rng = np.random.default_rng(0)
    lo_ret = rng.normal(0, 0.005, 200)
    hi_ret = rng.normal(0, 0.020, 200)
    ret = np.concatenate([lo_ret, hi_ret])
    px = pd.Series(100 * np.exp(np.cumsum(ret)),
                   index=pd.bdate_range("2024-01-01", periods=n))

    fwd = ve.realized_vol_forward(px, h)

    # 199（0-based）之後嘅 21 個回報全部落喺高波幅段
    at_switch = fwd.iloc[199]
    early = fwd.iloc[100]
    assert at_switch > early * 2, (at_switch, early)

    # 低波幅段中間：前瞻窗完全在低波幅段內 → 應該接近 0.5%·√252
    expected_low = 0.005 * math.sqrt(252) * 100
    assert fwd.iloc[100] == pytest.approx(expected_low, rel=0.35)

    # 尾段冇足夠未來數據 → 必須係 NaN，唔可以靜靜借用過去
    assert fwd.iloc[-1] != fwd.iloc[-1] or np.isnan(fwd.iloc[-1])


def test_forward_and_backward_vol_differ_in_regime_shift():
    """回望同前瞻波幅喺切換點必須唔同 —— 證明兩者唔可以互換口徑。"""
    ret = np.concatenate([np.full(120, 0.002), np.full(120, -0.002)] * 1)
    ret = ret + np.concatenate([np.full(120, 0.0), np.full(120, 0.02)])
    px = pd.Series(100 * np.exp(np.cumsum(ret)),
                   index=pd.bdate_range("2024-01-01", periods=len(ret)))
    f = ve.realized_vol_forward(px, 21).iloc[110]
    b = ve.realized_vol_back(px, 21).iloc[110]
    assert not np.isclose(f, b)


# ─────────────────────────────────────────────────────────────
# costs.py
# ─────────────────────────────────────────────────────────────
def test_cost_direction_shorts_receive_bid_longs_pay_ask():
    c = costs.CostModel(slippage_per_leg=0.10, commission_per_leg=0.0)
    assert c.fill(costs.Leg(10.0, "short"), "sell") == pytest.approx(9.0)
    assert c.fill(costs.Leg(10.0, "long"), "buy") == pytest.approx(11.0)
    with pytest.raises(ValueError):
        c.fill(costs.Leg(10.0, "long"), "hold")
    with pytest.raises(ValueError):
        costs.Leg(1.0, "sideways")


def test_open_credit_lower_than_mid_credit():
    legs = [costs.Leg(5.0, "short"), costs.Leg(4.0, "short"),
            costs.Leg(1.5, "long"), costs.Leg(1.2, "long")]
    mid = costs.ZERO_COST.open_credit(legs)
    real = costs.CostModel(slippage_per_leg=0.05).open_credit(legs)
    assert mid == pytest.approx(5.0 + 4.0 - 1.5 - 1.2)
    assert real < mid


def test_round_trip_drag_positive_and_scales():
    legs = [costs.Leg(5.0, "short"), costs.Leg(4.0, "short"),
            costs.Leg(1.5, "long"), costs.Leg(1.2, "long")]
    d1 = costs.CostModel(slippage_per_leg=0.01).round_trip_drag(legs)
    d3 = costs.CostModel(slippage_per_leg=0.03).round_trip_drag(legs)
    assert 0 < d1 < d3
    assert d3 == pytest.approx(d1 * 3, rel=1e-6)


def test_zero_cost_has_no_drag():
    legs = [costs.Leg(5.0, "short"), costs.Leg(1.0, "long")]
    assert costs.ZERO_COST.round_trip_drag(legs) == pytest.approx(0.0)


def test_sensitivity_grid_covers_required_levels():
    g = costs.grid()
    assert set(costs.SENSITIVITY_GRID) == {0.0, 0.01, 0.03, 0.05}
    assert len(g) == 4


# ─────────────────────────────────────────────────────────────
# portfolio.py
# ─────────────────────────────────────────────────────────────
def _mk_trade(open_d: str, days: int, pnl_path: list[float], max_loss: float):
    idx = pd.bdate_range(open_d, periods=days)
    return {"open": open_d, "exit_date": str(idx[-1].date()),
            "max_loss": max_loss,
            "mtm": {str(d.date()): v for d, v in zip(idx, pnl_path)}}


def test_equity_curve_keeps_realized_pnl_after_exit():
    """審查報告修正：平倉之後已實現損益必須留在曲線上，唔可以「消失」。"""
    t1 = _mk_trade("2025-01-01", 3, [0.0, 0.5, 1.0], 10.0)     # 賺 1.0 / 10
    t2 = _mk_trade("2025-01-06", 3, [0.0, 0.0, 0.0], 10.0)     # 平手
    eq = pf.equity_curve([t1, t2], capital_units=1)
    # t1 平倉後（t2 期間）權益必須維持 +0.1，唔可以歸零
    assert eq.iloc[-1] == pytest.approx(0.1)
    assert eq.min() >= 0.0


def test_equity_curve_skips_trades_without_mtm_and_reports_it():
    good = _mk_trade("2025-01-01", 3, [0.0, 0.5, 1.0], 10.0)
    bad = {"open": "2025-01-01", "max_loss": 10.0}
    eq = pf.equity_curve([good, bad], capital_units=1)
    assert eq.attrs["skipped_no_mtm"] == 1


def test_equity_curve_capital_defaults_to_max_concurrent():
    a = _mk_trade("2025-01-01", 5, [0.0] * 5, 10.0)
    b = _mk_trade("2025-01-01", 5, [0.0] * 5, 10.0)
    eq = pf.equity_curve([a, b])
    assert eq.attrs["capital_units"] == 2
    assert eq.attrs["max_concurrent"] == 2


def test_metrics_positive_trend_gives_positive_sharpe():
    idx = pd.bdate_range("2024-01-01", periods=120)
    eq = pd.Series(np.linspace(0, 0.20, 120), index=idx)
    m = pf.metrics(eq)
    assert m["sharpe"] > 0
    assert m["max_drawdown_pct"] == pytest.approx(0.0, abs=1e-9)
    assert m["longest_underwater_days"] == 0
    assert m["n_days"] == 120


def test_metrics_detects_drawdown_and_underwater_length():
    idx = pd.bdate_range("2024-01-01", periods=60)
    path = np.concatenate([np.linspace(0, 0.10, 20),
                           np.linspace(0.10, -0.05, 20),
                           np.linspace(-0.05, 0.02, 20)])
    m = pf.metrics(pd.Series(path, index=idx))
    assert m["max_drawdown_pct"] < -10
    assert m["longest_underwater_days"] >= 35
    assert m["max_dd_from"] < m["max_dd_to"]


def test_metrics_short_series_returns_no_stats():
    assert pf.metrics(pd.Series([0.0, 0.1], index=pd.bdate_range("2024-01-01", periods=2)))["n_days"] == 2


def test_block_bootstrap_widens_ci_with_overlap():
    """block=21 嘅置信區間必須明顯闊過 block=1（獨立假設）。"""
    rng = np.random.default_rng(3)
    # 高自相關序列：重疊窗嘅典型結構
    x = pd.Series(np.convolve(rng.normal(0, 1, 800), np.ones(21) / 21, "same"))
    narrow = pf.block_bootstrap_ci(x, block=1, n_boot=500)
    wide = pf.block_bootstrap_ci(x, block=21, n_boot=500)
    assert (wide["hi"] - wide["lo"]) > (narrow["hi"] - narrow["lo"])
    assert wide["n_independent"] == pytest.approx(800 / 21, rel=0.02)


def test_block_bootstrap_named_stats():
    x = pd.Series(np.arange(-100, 100, dtype=float))
    m = pf.block_bootstrap_ci(x, block=21, n_boot=300, stat="mean")
    w = pf.block_bootstrap_ci(x, block=21, n_boot=300, stat="win_rate")
    assert m["lo"] <= m["point"] <= m["hi"]
    assert 0 <= w["point"] <= 100
    with pytest.raises(ValueError):
        pf.block_bootstrap_ci(x, block=21, stat="nonsense")


def test_block_bootstrap_returns_none_when_too_short():
    assert pf.block_bootstrap_ci(pd.Series([1.0, 2.0, 3.0]), block=21) is None


# ─────────────────────────────────────────────────────────────
# condor_engine：合成期權鏈
# ─────────────────────────────────────────────────────────────
STRIKES = [80.0, 85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0, 120.0]


def _synthetic_chain(d: date, spot: float, expiry: date, iv: float = 30.0,
                     code: str = "00700") -> pd.DataFrame:
    """用 BS 造一份自洽嘅期權鏈（settle 完全等於模型價）。"""
    dte = (expiry - d).days
    t = max(dte, 1) / 365
    rows = []
    for k in STRIKES:
        for cp in ("C", "P"):
            rows.append({
                "date": d, "hkats": "TCH", "stock_code": code, "name": "TEST",
                "close": spot, "expiry": expiry, "strike": k, "type": cp,
                "settle": bs.price(spot, k, t, iv / 100, cp),
                "settle_chg": 0.0, "iv": iv, "volume": 500.0,
                "oi": 5000.0, "oi_chg": 0.0, "dte": dte,
                "moneyness": k / spot,
            })
    return pd.DataFrame(rows)


@pytest.fixture
def synth_chains(monkeypatch):
    """把 chain_cache 換成合成鏈：現價由 100 線性升到 104。"""
    open_d = date(2025, 3, 3)
    expiry = date(2025, 4, 29)      # DTE 57 → 開倉日 dte 落喺 25-55 外? 見下
    expiry = date(2025, 4, 15)      # DTE 43，落喺 MIN_DTE..MAX_DTE
    days = pd.bdate_range(open_d, expiry - timedelta(days=1))
    spots = {d.date(): 100.0 + i * 0.1 for i, d in enumerate(days)}

    def fake_day(d, stock_code=None, use_disk=True):
        if d not in spots:
            return pd.DataFrame()
        df = _synthetic_chain(d, spots[d], expiry)
        return df if stock_code is None else df[df.stock_code == stock_code]

    monkeypatch.setattr(cc, "day", fake_day)
    monkeypatch.setattr(cc, "latest", lambda: open_d)
    monkeypatch.setattr(cc, "legs", lambda d, c, e, s=None: (
        lambda df: df if s is None else df[df.strike.isin(s)])(
            fake_day(d, c)[fake_day(d, c).expiry == e]))
    def fake_leg_mid(d, c, e, k, cp):
        df = fake_day(d, c)
        if df.empty:
            return None
        m = df[(df.expiry == e) & (df.strike == k) & (df.type == cp)]
        return None if m.empty else float(m.settle.iloc[0])

    monkeypatch.setattr(cc, "leg_mid", fake_leg_mid)
    monkeypatch.setattr(cc, "atm_iv", lambda d, c, e, spot: 30.0)
    return {"open": open_d, "expiry": expiry, "spots": spots,
            "px": pd.Series(list(spots.values()),
                            index=pd.to_datetime(list(spots)))}


def test_build_returns_balanced_condor(synth_chains):
    c = ce.build("00700", synth_chains["open"], cost=costs.ZERO_COST)
    assert c is not None
    assert c["long_put"] < c["short_put"] < c["spot"] < c["short_call"] < c["long_call"]
    assert c["credit"] > 0
    assert c["max_loss"] == pytest.approx(c["width"] - c["credit"])
    assert c["be_low"] < c["spot"] < c["be_high"]


def test_build_credit_shrinks_with_slippage(synth_chains):
    d = synth_chains["open"]
    zero = ce.build("00700", d, cost=costs.ZERO_COST)
    cost3 = ce.build("00700", d, cost=costs.CostModel(slippage_per_leg=0.03))
    assert cost3["credit"] < zero["credit"]
    assert cost3["cost_drag"] > 0
    assert cost3["max_loss"] > zero["max_loss"]     # 收得少 → 蝕得多


def test_value_legs_uses_real_chain_not_intrinsic(synth_chains):
    """A1 核心：14 DTE 時 4 條腳仍然有時間值，唔可以係到期內在值。"""
    d0, exp = synth_chains["open"], synth_chains["expiry"]
    c = ce.build("00700", d0, cost=costs.ZERO_COST)
    mid_day = exp - timedelta(days=14)
    # 揀最近一個有鏈嘅營業日
    while mid_day not in synth_chains["spots"]:
        mid_day -= timedelta(days=1)
    spot = synth_chains["spots"][mid_day]

    legs, src = ce.value_legs(mid_day, "00700", c, spot)
    assert src == "chain"
    close_cost = costs.ZERO_COST.close_cost(legs)

    intrinsic = c["credit"] - ce._payoff(spot, c) + 0  # 到期口徑嘅平倉成本
    # 現價落喺兩條短腳中間 → 到期內在值 = 0，但真實鏈平倉成本 > 0
    assert intrinsic == pytest.approx(0.0, abs=1e-9)
    assert close_cost > 0.1, close_cost


def test_value_legs_falls_back_to_model_when_chain_missing(synth_chains):
    d0 = synth_chains["open"]
    c = ce.build("00700", d0, cost=costs.ZERO_COST)
    missing = date(2030, 1, 2)      # 冇鏈嘅日子
    legs, src = ce.value_legs(missing, "00700", c, 100.0, iv_hint=30.0)
    assert src == "model"
    assert legs is not None and len(legs) == 4


def test_backtest_records_daily_mtm_and_exit_reason(synth_chains):
    dates = [d for d in synth_chains["spots"]][:15]
    r = ce.backtest("00700", synth_chains["px"], dates, step=5,
                    cost=costs.ZERO_COST, min_trades=1)
    assert r is not None, "backtest 唔應該靜靜返 None"
    assert not r.get("insufficient"), r
    t0 = r["trades"][0]
    assert len(t0["mtm"]) >= 2
    assert t0["exit_reason"] in {"target", "stop", "time"}
    assert -t0["max_loss"] - 1e-9 <= t0["pnl"] <= t0["credit"] + 1e-9
    assert r["portfolio"]["n_days"] >= 2


def test_backtest_stop_loss_triggers_on_crash(synth_chains, monkeypatch):
    """短腳被大幅擊穿時，止損必須觸發（而唔係硬持到 exit_day）。"""
    d0, exp = synth_chains["open"], synth_chains["expiry"]
    crash_from = d0 + timedelta(days=7)
    spots = dict(synth_chains["spots"])
    for d in spots:
        if d >= crash_from:
            spots[d] = 70.0          # 大幅跌穿 long put

    def fake_day(d, stock_code=None, use_disk=True):
        if d not in spots:
            return pd.DataFrame()
        iv = 30.0 if d < crash_from else 80.0
        df = _synthetic_chain(d, spots[d], exp, iv)
        return df if stock_code is None else df[df.stock_code == stock_code]

    monkeypatch.setattr(cc, "day", fake_day)
    def fake_leg_mid(d, c, e, k, cp):
        df = fake_day(d, c)
        if df.empty:
            return None
        m = df[(df.expiry == e) & (df.strike == k) & (df.type == cp)]
        return None if m.empty else float(m.settle.iloc[0])

    monkeypatch.setattr(cc, "leg_mid", fake_leg_mid)
    monkeypatch.setattr(cc, "atm_iv",
                        lambda d, c, e, spot: 30.0 if d < crash_from else 80.0)

    px = pd.Series(list(spots.values()), index=pd.to_datetime(list(spots)))
    r = ce.backtest("00700", px, [d0], step=5, cost=costs.ZERO_COST,
                    min_trades=1)
    assert r and not r.get("insufficient"), r
    t0 = r["trades"][0]
    assert t0["exit_reason"] == "stop", t0
    assert t0["pnl"] < 0
    # 止損之後唔應該繼續記 MTM
    assert max(t0["mtm"]) == t0["exit_date"]


def test_backtest_reports_skip_reasons_instead_of_silence(synth_chains):
    """A6：冇機會開倉時，必須講原因，唔可以靜靜返 None。"""
    far = [date(2030, 1, 2), date(2030, 1, 3)]
    px = pd.Series([100.0, 100.0], index=pd.to_datetime(far))
    r = ce.backtest("00700", px, far, step=1, cost=costs.ZERO_COST,
                    min_trades=1)
    assert r is not None and r.get("insufficient")
    assert r["skipped"], "跳過原因必須被記錄（A6）"


def test_touched_uses_intraday_high_low_when_available():
    c = {"short_call": 110.0, "short_put": 90.0}
    idx = pd.to_datetime(["2025-01-02"])
    close = pd.Series([100.0], index=idx)          # 收市冇觸及
    hi = pd.Series([115.0], index=idx)             # 但日內衝上 115
    lo = pd.Series([99.0], index=idx)
    t_intraday, basis = ce._touched_on(idx[0], close, hi, lo, c)
    assert t_intraday is True and basis == "intraday"
    t_close, basis2 = ce._touched_on(idx[0], close, None, None, c)
    assert t_close is False and basis2 == "close"


def test_time_basis_trading_counts_business_days(monkeypatch):
    """C3：trading 口徑要真數營業日，而且要真正扣公假。"""
    start = date(2025, 3, 3)
    monkeypatch.setattr(ce, "HK_HOLIDAYS", np.array([], dtype="datetime64[D]"))
    trd = ce._t(30, "trading", start)
    assert trd == pytest.approx(
        np.busday_count(start, start + timedelta(days=30)) / 252)

    # 只扣週末時 trading ≈ calendar（差 < 10%）—— 呢個係誠實嘅幅度
    cal = ce._t(30, "calendar", start)
    assert abs(trd - cal) / cal < 0.10

    # 加 10 日公假之後必須明顯細過 calendar（真正嘅 C3 效應）
    hols = np.array([str(start + timedelta(days=i)) for i in range(3, 13)],
                    dtype="datetime64[D]")
    monkeypatch.setattr(ce, "HK_HOLIDAYS", hols)
    with_hols = ce._t(30, "trading", start)
    assert with_hols < trd
    assert with_hols < cal


def test_time_basis_trading_without_start_falls_back_to_calendar():
    assert ce._t(30, "trading") == pytest.approx(ce._t(30, "calendar"))


# ─────────────────────────────────────────────────────────────
# vrp_engine.load_ohlc 契約
# ─────────────────────────────────────────────────────────────
def test_load_ohlc_returns_all_fields_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ve, "QUOTES", tmp_path / "nope.json")
    o = ve.load_ohlc()
    assert set(o) == set(ve.FIELDS)
    assert all(df.empty for df in o.values())
    px, tv = ve.load_quotes()          # 舊介面必須仍然 unpack 得
    assert px.empty and tv.empty


def test_load_ohlc_parses_high_low(monkeypatch, tmp_path):
    p = tmp_path / "q.json"
    p.write_text('{"quotes": {"2025-01-02": {"00700": '
                 '{"close": 400, "high": 410, "low": 395, "turnover": 1e9}}}}')
    monkeypatch.setattr(ve, "QUOTES", p)
    o = ve.load_ohlc()
    assert o["high"].iloc[0, 0] == 410.0
    assert o["low"].iloc[0, 0] == 395.0
    assert o["close"].iloc[0, 0] == 400.0


def test_load_ohlc_survives_corrupt_json(monkeypatch, tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    monkeypatch.setattr(ve, "QUOTES", p)
    o = ve.load_ohlc()
    assert all(df.empty for df in o.values())


def test_min_history_reflects_overlap():
    """MIN_HISTORY 必須夠 MIN_INDEPENDENT 個獨立樣本。"""
    assert ve.MIN_HISTORY >= ve.MIN_INDEPENDENT * ve.HORIZON


def test_stats_includes_independent_count_and_ci():
    rng = np.random.default_rng(1)
    s = pd.Series(rng.normal(5, 8, 600))
    st = ve._stats(s, block=21)
    assert st["n"] == 600
    assert st["n_independent"] == 600 // 21
    assert st["mean_ci"]["lo"] < st["mean"] < st["mean_ci"]["hi"]


# ---------------------------------------------------------------------------
# chain_cache 同生產 repo 嘅 chain_history 快取整合
# ---------------------------------------------------------------------------

class _FakeHistory:
    """假 chain_history 模組：只需要 .CACHE.exists() 同 .load(start, end)。"""

    def __init__(self, df, path_exists=True):
        self._df = df
        self.CACHE = type("P", (), {"exists": staticmethod(lambda: path_exists)})()

    def load(self, codes=None, start=None, end=None):
        d = self._df
        if start is not None:
            d = d[d.date >= pd.Timestamp(start)]
        if end is not None:
            d = d[d.date <= pd.Timestamp(end)]
        return d.reset_index(drop=True)


def _history_frame(days, code="00700"):
    rows = []
    for d in days:
        for k in (90.0, 100.0, 110.0):
            for cp in ("C", "P"):
                rows.append({"date": pd.Timestamp(d), "stock_code": code,
                             "expiry": pd.Timestamp(date(2025, 4, 15)),
                             "strike": k, "type": cp, "settle": 3.0,
                             "iv": 30.0, "volume": 10, "oi": 500, "dte": 30,
                             "close": 100.0})
    return pd.DataFrame(rows)


def test_chain_cache_prefers_chain_history_over_reparsing(monkeypatch):
    """生產 repo 已經每日 build chain_history，唔應該再造第二份快取。"""
    days = [date(2025, 3, 3), date(2025, 3, 4)]
    monkeypatch.setattr(cc, "ch", _FakeHistory(_history_frame(days)))
    monkeypatch.setattr(cc, "_history_ok", None)

    def boom(*a, **kw):
        raise AssertionError("有 chain_history 就唔應該再 parse raw 日報")

    monkeypatch.setattr(cc.oc, "parse_chains", boom)
    cc._mem.clear()
    for k in cc._hits:
        cc._hits[k] = 0

    df = cc.day(days[0], "00700")
    assert not df.empty
    # 日期必須轉返 date（下游用 date 比較 expiry）
    assert isinstance(df.expiry.iloc[0], date)
    assert not isinstance(df.expiry.iloc[0], pd.Timestamp)
    assert cc._hits["history"] == 1 and cc._hits["parse"] == 0
    assert cc.stats()["source"] == "chain_history"


def test_chain_cache_prime_reads_range_once(monkeypatch):
    days = [date(2025, 3, 3), date(2025, 3, 4), date(2025, 3, 5)]
    fake = _FakeHistory(_history_frame(days))
    calls = {"n": 0}
    orig = fake.load

    def counted(**kw):
        calls["n"] += 1
        return orig(**kw)

    fake.load = counted
    monkeypatch.setattr(cc, "ch", fake)
    monkeypatch.setattr(cc, "_history_ok", None)
    cc._mem.clear()

    n = cc.prime(days)
    assert n == 3
    assert calls["n"] == 1, "prime 應該一次讀晒整個範圍，唔係逐日 filter"


def test_chain_cache_falls_back_when_history_missing(monkeypatch):
    """冇 chain_history（研究 repo）時必須照跑，唔可以 ImportError 死。"""
    monkeypatch.setattr(cc, "ch", None)
    monkeypatch.setattr(cc, "_history_ok", None)
    called = {"n": 0}

    def fake_parse(d, code=None):
        called["n"] += 1
        return pd.DataFrame()

    monkeypatch.setattr(cc.oc, "parse_chains", fake_parse)
    cc._mem.clear()
    assert cc.day(date(2025, 3, 3), use_disk=False).empty
    assert called["n"] == 1
    assert cc.stats()["source"] == "raw"
