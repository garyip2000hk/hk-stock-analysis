"""condor_gate.py — 鐵鷹三層閘門（VRP × IV × CBBC）.

將 2026-08 回測驗證過嘅三層過濾變成每日生產訊號：

  L1  VRP ≥ 5    入場時點 VRP（IV − 前瞻 21 日已實現波幅）。
                  回測：VRP≤0 嘅倉佔基準虧損嘅大部分，呢層砍走佢哋。
  L2  IV ≥ 34    入場時 ATM IV。回測：IV 過濾單獨用效果有限，
                  但同 VRP 夾埋令總虧損由 −$1,100 萬收到 −$130 萬。
  ⚠ 誠實結論：全樣本掃描（VRP 0-15 × IV 34-50）冇任何組合去到正期望；
    閘門嘅價值係「由大負期望收窄到 −$2,000/筆」，唔係變賺錢。
  L3  CBBC 避坑  strategy_lab squeeze_radar 有 STRONG_SQUEEZE 嘅標的不做。
                  ⚠ 街貨歷史只有 3 日，呢層暫時只可以前瞻過濾，回測唔到。

輸出 `options_data/condor_gate.json`：
  - backtest_evidence：同一個 s_iron_condor builder 喺真回測數據上，
    唔同閘門組合嘅勝率／EV 對照（--backtest 重算，平時讀 cache）
  - candidates：vol_system 嘅鐵鷹候選逐隻過三層，附 blocked 原因
  - cbbc_radar：HSI squeeze 狀態 + 今日被 CBBC 層擋住嘅股票

CLI:
    python3 condor_gate.py              # 今日 gate（快，秒級）
    python3 condor_gate.py --backtest   # 重跑閘門對照回測（~3 分鐘）
    python3 condor_gate.py --json       # 淨出 JSON
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE = Path(__file__).parent
QE = BASE / "quant_engine"
OUT = BASE / "options_data" / "condor_gate.json"
VOL_SYS = BASE / "vol_system.json"
VRP_LOOKUP = QE / "vrp_lookup.parquet"
STRATEGY_LAB = BASE / "options_data" / "strategy_lab.json"

# ── 三層門檻（回測驗證嗰組） ─────────────────────────────
VRP_MIN = 5.0
IV_MIN = 34.0
CBBC_BLOCK_SIGNALS = {"STRONG_SQUEEZE"}
CBBC_CAUTION_SIGNALS = {"WATCH"}   # 唔擋，但註明要減注


def _now() -> str:
    return datetime.now().astimezone(
        __import__("zoneinfo").ZoneInfo("Asia/Hong_Kong")).isoformat(timespec="seconds")


# ───────────────────────── L1: VRP 查表 ─────────────────────────

def latest_vrp() -> tuple[dict[str, dict], str]:
    """每隻股票最新一期點位 VRP → {code: {vrp, iv, date}}."""
    v = pd.read_parquet(VRP_LOOKUP)
    v = v.sort_values("date").groupby("stock_code").tail(1)
    out: dict[str, dict] = {}
    for _, r in v.iterrows():
        out[r.stock_code] = {
            "vrp": round(float(r.vrp), 1),
            "iv_then": round(float(r.atm_iv), 1),
            "date": str(pd.Timestamp(r.date).date()),
        }
    as_of = str(pd.Timestamp(v.date.max()).date())
    return out, as_of


# ───────────────────────── L3: CBBC 雷達 ─────────────────────────

def squeeze_map() -> dict[str, dict]:
    """strategy_lab squeeze_radar → {symbol: {signal, heavy zones}}."""
    try:
        sl = json.loads(STRATEGY_LAB.read_text())
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for it in sl.get("squeeze_radar", []):
        out[str(it.get("symbol"))] = {
            "signal": it.get("signal"),
            "score": it.get("score"),
            "heavy_bear_zone": it.get("heavy_bear_zone"),
            "heavy_bull_zone": it.get("heavy_bull_zone"),
        }
    return out


# ───────────────────────── L4: VIX 環境（顧問層，唔擋盤） ─────────────────────────

def vix_context() -> dict:
    """IB VIX 最新收市 → cross_market.vix_regime 分級。"""
    try:
        import cross_market
        v = cross_market.load_ib().get("VIX")
        if v is None or not len(v):
            return cross_market.vix_regime(None)
        level = float(v.close.iloc[-1])
        out = cross_market.vix_regime(level)
        out["asof"] = str(v.date.iloc[-1])
        if len(v) >= 2:
            out["chg"] = round(level - float(v.close.iloc[-2]), 2)
        return out
    except Exception as e:
        return {"level": None, "regime": "unknown", "note": f"VIX 載入失敗: {e}"}


# ───────────────────────── 候選過閘 ─────────────────────────

def gate_candidates(verbose: bool = True) -> dict:
    rows = json.loads(VOL_SYS.read_text())
    cand = [r for r in rows if "condor" in str(r.get("strategy", "")).lower()]
    vrp_map, vrp_asof = latest_vrp()
    sq = squeeze_map()

    out: list[dict] = []
    n_pass = 0
    for r in cand:
        code = r["stock_code"]
        c = r.get("condor") or {}
        v = vrp_map.get(code)
        iv_now = r.get("iv")
        s = sq.get(code)

        layers = []
        blocked: list[str] = []
        # L0 — 結構
        if not r.get("condor"):
            layers.append({"layer": "結構", "pass": False, "value": None,
                           "detail": "期權鏈砌唔到鷹（行使價／delta／OI 唔達標），有閘都冇盤可落"})
            blocked.append("L0 冇結構")
        else:
            layers.append({"layer": "結構", "pass": True, "value": None,
                           "detail": "有可落盤四腳結構"})
        # L1 — VRP
        if v is None:
            layers.append({"layer": "VRP", "pass": False,
                           "value": None, "detail": "冇 VRP 數據（新股／樣本不足）"})
            blocked.append("L1 冇VRP數據")
        elif v["vrp"] >= VRP_MIN:
            layers.append({"layer": "VRP", "pass": True, "value": v["vrp"],
                           "detail": f"截至 {v['date']}：{v['vrp']:+.1f} ≥ {VRP_MIN:.0f}"})
        else:
            layers.append({"layer": "VRP", "pass": False, "value": v["vrp"],
                           "detail": f"截至 {v['date']}：{v['vrp']:+.1f} < {VRP_MIN:.0f}（波幅冇溢價，賣方冇著數）"})
            blocked.append(f"L1 VRP {v['vrp']:+.1f} < {VRP_MIN:.0f}")

        # L2 — IV
        if iv_now is None:
            layers.append({"layer": "IV", "pass": False, "value": None,
                           "detail": "冇 IV"})
            blocked.append("L2 冇IV")
        elif iv_now >= IV_MIN:
            layers.append({"layer": "IV", "pass": True, "value": iv_now,
                           "detail": f"IV {iv_now} ≥ {IV_MIN:.0f}，權金夠厚"})
        else:
            layers.append({"layer": "IV", "pass": False, "value": iv_now,
                           "detail": f"IV {iv_now} < {IV_MIN:.0f}（權金太薄，輸一次食晒贏嗰啲）"})
            blocked.append(f"L2 IV {iv_now} < {IV_MIN:.0f}")

        # L3 — CBBC squeeze
        if s and s["signal"] in CBBC_BLOCK_SIGNALS:
            layers.append({"layer": "CBBC", "pass": False, "value": s["signal"],
                           "detail": f"CBBC 重倉擠壓風險（score {s['score']}），避開"})
            blocked.append("L3 CBBC擠壓")
        elif s and s["signal"] in CBBC_CAUTION_SIGNALS:
            layers.append({"layer": "CBBC", "pass": True, "value": s["signal"],
                           "detail": "CBBC WATCH：有重倉區但未觸發擠壓，建議減半注"})
        else:
            layers.append({"layer": "CBBC", "pass": True, "value": None,
                           "detail": "冇 CBBC 重倉擠壓訊號"})

        ok = not blocked
        if ok:
            n_pass += 1
        out.append({
            "stock_code": code,
            "name": r.get("name"),
            "spot": r.get("close"),
            "iv": iv_now,
            "iv_rank": r.get("iv_rank"),
            "vrp": v["vrp"] if v else None,
            "vrp_asof": v["date"] if v else None,
            "vrp_mean_longrun": r.get("vrp_mean"),
            "bt_win": r.get("bt_win"),
            "bt_trades": r.get("bt_trades"),
            "layers": layers,
            "blocked": blocked,
            "verdict": "APPROVED" if ok else "BLOCKED",
            "condor": {
                "expiry": c.get("expiry"),
                "dte": c.get("dte"),
                "short_call": c.get("short_call"),
                "long_call": c.get("long_call"),
                "short_put": c.get("short_put"),
                "long_put": c.get("long_put"),
                "credit": c.get("credit"),
                "width": c.get("width"),
                "max_loss": c.get("max_loss"),
                "be_low": c.get("be_low"),
                "be_high": c.get("be_high"),
                "range_pct": c.get("range_pct"),
                "p_win_model": c.get("p_win_model"),
            },
            "reasons": r.get("reasons", []),
        })

    if verbose:
        print(f"🚦 三層閘門 VRP≥{VRP_MIN:.0f} · IV≥{IV_MIN:.0f} · CBBC避坑")
        print(f"   候選 {len(out)} 隻 → 通過 {n_pass} 隻")
        for o in out:
            mark = "✅" if o["verdict"] == "APPROVED" else "🚫"
            why = "；".join(o["blocked"]) or "三層全過"
            print(f"   {mark} {o['stock_code']} {o['name']:<18s} VRP "
                  f"{('%+.1f' % o['vrp']) if o['vrp'] is not None else '  — '} "
                  f"IV {o['iv'] or '—':>5} | {why}")

    hsi = sq.get("HSI", {})
    return {
        "thresholds": {"vrp_min": VRP_MIN, "iv_min": IV_MIN,
                       "cbbc_block": sorted(CBBC_BLOCK_SIGNALS)},
        "candidates": out,
        "summary": {"total": len(out), "approved": n_pass,
                    "blocked": len(out) - n_pass},
        "cbbc_radar": {
            "hsi_signal": hsi.get("signal"),
            "hsi_score": hsi.get("score"),
            "blocked_by_cbbc": [c["stock_code"] for c in out
                                if any("CBBC" in b for b in c["blocked"])],
        },
        "vrp_asof": vrp_asof,
        "note_vrp_lag": "VRP 用前瞻已實現波幅，天生滯後約 21 個交易日，屬正常",
    }


# ───────────────────── 閘門對照回測（L1+L2） ─────────────────────

def backtest_evidence(verbose: bool = True) -> list[dict]:
    """同一個鐵鷹 builder，唔同閘門組合嘅回測對照。

    只驗證到 L1/L2（VRP/IV 有成個歷史期嘅點位數據）；
    L3 CBBC 街貨歷史得 3 日，回測唔到，只做前瞻過濾。
    """
    sys.path.insert(0, str(QE))
    from options_backtester import (  # noqa: E402
        s_iron_condor, load_chain, load_specs, backtest_one,
        metrics_from_trades,
    )

    vrp_df = pd.read_parquet(VRP_LOOKUP)
    vrp_df["ds"] = vrp_df.date.dt.strftime("%Y-%m-%d")
    vrp_map: dict[str, dict[str, float]] = {}
    for _, r in vrp_df.iterrows():
        vrp_map.setdefault(r.stock_code, {})[r.ds] = float(r.vrp)
    iv_map: dict[str, dict[str, float]] = {}
    for _, r in vrp_df.iterrows():
        iv_map.setdefault(r.stock_code, {})[r.ds] = float(r.atm_iv)

    chain = load_chain()
    specs = load_specs()
    codes = sorted(set(chain.stock_code.dropna()) & set(specs))

    if verbose:
        print(f"📦 鐵鷹 builder 跑 {len(codes)} 隻標的…")

    all_trades: list[dict] = []
    for code in codes:
        cs = int(specs[code].get("contract_size") or 1000)
        try:
            all_trades += backtest_one(
                code, chain[chain.stock_code == code].copy(),
                s_iron_condor, False, cs)
        except Exception as e:
            if verbose:
                print(f"   ⚠ {code}: {e}")

    def _gate(trades, vrp_min=None, iv_min=None):
        keep = []
        for t in trades:
            v = vrp_map.get(t["code"], {}).get(t["open"])
            iv = iv_map.get(t["code"], {}).get(t["open"])
            if vrp_min is not None and (v is None or v < vrp_min):
                continue
            if iv_min is not None and (iv is None or iv < iv_min):
                continue
            keep.append(t)
        return keep

    variants = [
        ("基準（無閘門）", None, None),
        ("VRP ≥ 0", 0.0, None),
        ("VRP ≥ 5", 5.0, None),
        ("IV ≥ 34", None, 34.0),
        ("VRP ≥ 0 ＋ IV ≥ 34", 0.0, 34.0),
        ("VRP ≥ 5 ＋ IV ≥ 34（現用）", 5.0, 34.0),
    ]
    rows = []
    for name, vmin, imin in variants:
        kept = _gate(all_trades, vmin, imin)
        m = metrics_from_trades(kept) if kept else {}
        rows.append({
            "variant": name,
            "is_current": name.endswith("現用）"),
            "n": m.get("total_trades", 0),
            "win_rate_pct": m.get("win_rate_pct", 0),
            "ev_hkd": m.get("expectancy_hkd", 0),
            "total_pnl_hkd": m.get("total_pnl_hkd", 0),
            "worst_trade_hkd": m.get("worst_trade", 0),
            "profit_factor": m.get("profit_factor", 0),
        })
        if verbose:
            print(f"   {name:<22s} N={rows[-1]['n']:>4} "
                  f"勝率 {rows[-1]['win_rate_pct']:>5.1f}% "
                  f"EV ${rows[-1]['ev_hkd']:>8,.0f} "
                  f"總PnL ${rows[-1]['total_pnl_hkd']:>10,.0f}")
    return rows


# ───────────────────────── main ─────────────────────────

# ───────────────────────── 落盤門票 ─────────────────────────
# 止賺止蝕規則（2026-08-22 mark-to-market 回測驗證，見 tpsl_note）
TP_RULE = 0.5    # 買回價 ≤ 0.5×credit → 止賺
SL_RULE = 2.0    # 買回價 ≥ 2×credit → 止蝕
TSTOP_DTE = 14   # 剩 14 日未觸發 → 時間止蝕，市價買回
CAPITAL_HKD = 500_000
RISK_PCT = 0.02
MAX_LOTS = 10

TPSL_EVIDENCE = [
    {"rule": "hold（持到期）", "n": 646, "win_pct": 62.7, "ev": -15518, "worst": -396695},
    {"rule": "TP 50%", "n": 665, "win_pct": 61.7, "ev": -14421, "worst": -396695},
    {"rule": "SL 2×", "n": 647, "win_pct": 61.4, "ev": -1198, "worst": -93766},
    {"rule": "TP50+SL2×", "n": 666, "win_pct": 60.8, "ev": -1482, "worst": -31884},
    {"rule": "TP50+SL2×+T7", "n": 667, "win_pct": 56.2, "ev": -1282, "worst": -19280},
    {"rule": "TP50+SL2×+T14（現用）", "n": 667, "win_pct": 49.3, "ev": -995, "worst": -18524},
]
TPSL_NOTE = (
    "閘門 trades 上逐日 mark-to-market 回測（2025-10 → 2026-08，結算價口徑）。"
    "關鍵係止蝕唔係止賺：SL 2× 單獨已把 EV 由 −$15.5k 收窄到 −$1.2k；"
    "現用 TP50+SL2×+T14 係 EV 同尾部最優（−$995/筆、最壞 −$18.5k），"
    "但仍然負期望 —— 細注試、當收租工具，唔好當 alpha。"
)


def make_tickets(cands: list[dict], vx: dict | None = None) -> list[dict]:
    """通過閘門股票 → 可落盤門票：富途合約代碼＋止賺止蝕價＋注碼。"""
    sys.path.insert(0, str(BASE.parent / "auto-trading"))
    from option_codes import option_code, contract_size  # noqa: E402

    tickets = []
    for s in cands:
        if s.get("verdict") != "APPROVED":
            continue
        c = s["condor"]
        code = s["stock_code"]
        cs = contract_size(code) or 1000
        legs = [
            {"action": "SELL", "cp": "C", "strike": c["short_call"],
             "code": option_code(code, c["expiry"], "C", c["short_call"])},
            {"action": "BUY", "cp": "C", "strike": c["long_call"],
             "code": option_code(code, c["expiry"], "C", c["long_call"])},
            {"action": "SELL", "cp": "P", "strike": c["short_put"],
             "code": option_code(code, c["expiry"], "P", c["short_put"])},
            {"action": "BUY", "cp": "P", "strike": c["long_put"],
             "code": option_code(code, c["expiry"], "P", c["long_put"])},
        ]
        credit = c["credit"]
        max_loss_lot = c["max_loss"] * cs
        lots = int(min(MAX_LOTS, (CAPITAL_HKD * RISK_PCT) // max(max_loss_lot, 1)))
        vix_note = None
        regime = (vx or {}).get("regime")
        if regime == "caution":
            lots = max(1, lots // 2)
            vix_note = f"VIX {vx['level']} caution → 注碼減半"
        elif regime == "too_low":
            lots = max(1, lots // 2)
            vix_note = f"VIX {vx['level']} 太低（權金薄）→ 注碼減半"
        elif regime == "stress":
            vix_note = f"VIX {vx['level']} 壓力市 → 建議今日唔開新鷹"
        tstop = (pd.Timestamp(c["expiry"]) - pd.Timedelta(days=TSTOP_DTE)).date().isoformat()
        tickets.append({
            "stock_code": code, "name": s["name"], "spot": s["spot"],
            "why": {
                "vrp": s["vrp"],
                "vrp_detail": f"IV 比前瞻已實現波幅貴 {s['vrp']:.1f} 點，賣方有溢價",
                "iv": s["iv"], "iv_detail": f"ATM IV {s['iv']:.1f}，權金夠厚",
                "cbbc": "冇 CBBC 重倉擠壓訊號，唔怕單邊掃倉",
                "vix": vix_note or ((vx or {}).get("note") or "VIX 正常賣方環境"),
                "structure": f"四腳砌到、breakeven 闊 {c['range_pct']:.0f}%",
            },
            "expiry": c["expiry"], "dte": c["dte"],
            "legs": legs,
            "credit": credit, "credit_hkd_per_lot": round(credit * cs, 1),
            "max_loss_hkd_per_lot": round(max_loss_lot, 1),
            "be_low": c["be_low"], "be_high": c["be_high"],
            "tp": {"rule": f"四腳買回價 ≤ {TP_RULE}×credit",
                   "buyback_max": round(TP_RULE * credit, 3),
                   "lock_hkd_per_lot": round((credit - TP_RULE * credit) * cs, 1)},
            "sl": {"rule": f"四腳買回價 ≥ {SL_RULE}×credit，或收市跌穿短腳",
                   "buyback_min": round(SL_RULE * credit, 3),
                   "loss_hkd_per_lot": round((SL_RULE * credit - credit) * cs, 1),
                   "spot_breach": f"收市 < {c['short_put']} 或 > {c['short_call']} → 翌日市價買回"},
            "time_stop": {"date": tstop,
                          "rule": f"{tstop}（剩 {TSTOP_DTE} 日）未觸發 TP/SL → 市價買回"},
            "lots": lots,
            "vix_adjust": vix_note,
            "lots_note": f"= min({MAX_LOTS}, $500k×2% ÷ 每lot最大虧損 ${max_loss_lot:,.0f})",
        })
    return tickets


def build(recompute_backtest: bool = False, verbose: bool = True) -> dict:
    data = gate_candidates(verbose=verbose)

    old = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text())
        except Exception:
            old = {}

    if recompute_backtest or not old.get("backtest_evidence"):
        data["backtest_evidence"] = backtest_evidence(verbose=verbose)
        data["backtest_computed_at"] = _now()
    else:
        data["backtest_evidence"] = old["backtest_evidence"]
        data["backtest_computed_at"] = old.get("backtest_computed_at")

    vx = vix_context()
    data["vix_regime"] = vx
    data["tickets"] = make_tickets(data["candidates"], vx)
    data["tpsl"] = {"rule": "TP50+SL2×+T14", "tp": TP_RULE, "sl": SL_RULE,
                    "tstop_dte": TSTOP_DTE, "evidence": TPSL_EVIDENCE,
                    "note": TPSL_NOTE}

    data["generated_at"] = _now()
    data["backtest_note"] = (
        "對照回測只驗證 L1 VRP / L2 IV 兩層（呢兩層有成個回測期嘅點位數據）。"
        "L3 CBBC 街貨歷史只有 3 日，回測唔到，暫時只做前瞻過濾。"
        "⚠ 誠實講：全樣本門檻掃描（VRP 0–15 × IV 34–50）冇任何組合去到正期望；"
        "三層齊開最好都係 EV −$2,000/筆左右 —— gate 嘅價值係止蝕（總虧損 −$1,100 萬 → −$130 萬），"
        "唔係保證賺錢。結算價唔等於可成交價，未計買賣差價同滑價。"
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    if verbose:
        print(f"✅ 寫入 {OUT}")
    return data


def main() -> None:
    ap = argparse.ArgumentParser(description="鐵鷹三層閘門")
    ap.add_argument("--backtest", action="store_true", help="重跑閘門對照回測")
    ap.add_argument("--json", action="store_true", help="淨出 JSON")
    args = ap.parse_args()

    data = build(recompute_backtest=args.backtest, verbose=not args.json)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
