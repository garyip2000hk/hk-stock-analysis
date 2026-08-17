"""vol_system.py — 波幅交易系統（三合一總成）。

把三個引擎嘅訊號合成一張每日決策表：

  1. vrp_engine     ── 邊隻股票值得做賣方？（波幅風險溢價，前瞻 RV 驗證）
  2. vol_surface    ── 期限結構有冇日曆機會？（前向係數偏離）
  3. condor_engine  ── 真實可落嘅 Iron Condor 結構 + 歷史回測實績

決策邏輯（優先順序）：

  · VRP 分數高 + IV Rank 高 + 回測勝率好   → Iron Condor（主策略）
  · VRP 分數高 + 前向係數偏低（近月被搶貴） → 日曆價差（賣近月買遠月）
  · VRP 負（IV 平過實際波幅）               → 唔做賣方，标示迴避
  · 回測樣本不足／流動性差                  → 剔出候選池

輸出：`vol_system.json`（每日訊號表）+ CLI 表格。

CLI:
    python3 vol_system.py                  # 今日全市場訊號表
    python3 vol_system.py --stock 09626    # 單股全套分析
    python3 vol_system.py --top 10         # 只睇最好 10 個
    python3 vol_system.py --json
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

import atm_history as ah
import condor_engine as ce
import vol_surface as vs
import vrp_engine as ve

BASE = Path(__file__).parent
OUT = BASE / "vol_system.json"
BT = BASE / "condor_backtest.json"

# ── 進場門檻 ─────────────────────────────────────────
MIN_VRP_SCORE = 6.0      # VRP 引擎評分
MIN_BT_TRADES = 20       # 回測最低樣本
MIN_BT_WIN = 70.0        # 回測實際勝率（%）
MIN_BT_RET = 15.0        # 回測平均風險回報（%）
MIN_IV_RANK = 30.0       # IV Rank 太低唔值得賣
FF_CALENDAR = 0.85       # 前向係數低過呢個 → 日曆機會
FF_REVERSE = 1.15        # 前向係數高過呢個 → 反向日曆


def _load_bt() -> dict[str, dict]:
    if not BT.exists():
        return {}
    rows = json.loads(BT.read_text())
    return {r["stock_code"]: r for r in rows}


def _decide(vrp: dict, surf: dict | None, bt: dict | None) -> dict:
    """由三個引擎嘅輸出決定策略 + 理由。"""
    score = vrp.get("score") or 0
    iv_rank = vrp.get("iv_rank")
    ff = (surf or {}).get("forward_factor")
    vrp_mean = (vrp.get("vrp") or {}).get("mean")

    reasons: list[str] = []
    blocks: list[str] = []

    # ── 硬性排除 ──
    if not vrp.get("liquidity_ok"):
        blocks.append("流動性不足")
    if not vrp.get("history_ok"):
        blocks.append("歷史樣本不足")
    if vrp_mean is not None and vrp_mean <= 0:
        blocks.append(f"VRP 負（IV 平過實際波幅 {abs(vrp_mean):.1f} 點）")
    if bt is None or (bt.get("n_trades") or 0) < MIN_BT_TRADES:
        blocks.append("回測樣本不足")

    bt_win = (bt or {}).get("win_rate")
    bt_ret = (bt or {}).get("avg_ret_on_risk")
    bt_sharpe = (bt or {}).get("sharpe")

    if bt_win is not None and bt_win < MIN_BT_WIN:
        blocks.append(f"回測勝率只有 {bt_win:.0f}%")
    if bt_ret is not None and bt_ret < MIN_BT_RET:
        blocks.append(f"回測回報只有 {bt_ret:.0f}%")

    if blocks:
        return {"strategy": "迴避", "overlay": None, "reasons": [],
                "blocks": blocks, "rank": -99}

    # ── 揀策略 ──
    if score >= MIN_VRP_SCORE:
        reasons.append(f"VRP 評分 {score:.1f}（賣方有優勢）")
    if iv_rank is not None and iv_rank >= MIN_IV_RANK:
        reasons.append(f"IV Rank {iv_rank:.0f}%（自身歷史偏貴）")
    if bt_win:
        reasons.append(f"回測 {bt['n_trades']} 筆・勝率 {bt_win:.0f}%・回報 {bt_ret:.0f}%")

    # ── 主策略：Iron Condor（有真回測支撐嘅唯一結構）──
    strategy = None
    if score >= MIN_VRP_SCORE and (iv_rank or 0) >= MIN_IV_RANK:
        strategy = "Iron Condor"
    elif score >= MIN_VRP_SCORE:
        strategy = "Iron Condor（IV Rank 偏低・減碼）"

    # 期限結構只做加註，唔取代主策略（回測數據係 Condor 嘅，唔可以借嚟撐日曆倉）
    overlay = None
    if ff is not None and ff < FF_CALENDAR:
        overlay = "可加日曆腿（賣近月・買遠月）"
        reasons.append(f"前向係數 {ff:.2f}：近月被搶貴，Condor 用近月收價更有利")
    elif ff is not None and ff > FF_REVERSE:
        overlay = "避免用遠月（遠月偏貴）"
        reasons.append(f"前向係數 {ff:.2f}：遠月偏貴，Condor 宜集中近月")

    if strategy is None:
        return {"strategy": "觀察", "overlay": None, "reasons": reasons,
                "blocks": ["VRP 評分未達門檻"], "rank": -1}

    rank = (score * 3
            + (bt_win or 0) / 10
            + (bt_ret or 0) / 10
            + (bt_sharpe or 0) * 2
            + (iv_rank or 0) / 20)
    return {"strategy": strategy, "overlay": overlay, "reasons": reasons,
            "blocks": [], "rank": round(rank, 2)}


def build_signals(as_of: date | None = None) -> list[dict]:
    iv_hist = ah.load()
    if iv_hist.empty:
        return []
    px, tv = ve.load_quotes()
    bt_all = _load_bt()
    surf_all = {s["stock_code"]: s for s in vs.scan(as_of)}
    import options_chain as oc
    ch_all = oc.parse_chains(as_of)

    codes = sorted(iv_hist[iv_hist.date == iv_hist.date.max()].stock_code.unique())
    out: list[dict] = []
    for code in codes:
        vrp = ve.analyse(code, iv_hist, px, tv)
        if not vrp:
            continue
        surf = surf_all.get(code)
        bt = bt_all.get(code)
        dec = _decide(vrp, surf, bt)
        condor = ce.build(code, as_of, chain_df=ch_all)

        out.append({
            "stock_code": code,
            "name": vrp["name"],
            "close": vrp["close"],
            "date": vrp["date"],
            "iv": vrp["iv"],
            "dte": vrp["dte"],
            "iv_rank": vrp.get("iv_rank"),
            "vrp_score": vrp.get("score"),
            "vrp_grade": vrp.get("grade"),
            "vrp_mean": (vrp.get("vrp") or {}).get("mean"),
            "vrp_win": (vrp.get("vrp") or {}).get("win_rate"),
            "forward_factor": (surf or {}).get("forward_factor"),
            "term_structure": (surf or {}).get("structure"),
            "skew": (surf or {}).get("front_skew"),
            "bt_trades": (bt or {}).get("n_trades"),
            "bt_win": (bt or {}).get("win_rate"),
            "bt_ret": (bt or {}).get("avg_ret_on_risk"),
            "bt_sharpe": (bt or {}).get("sharpe"),
            "bt_touch": (bt or {}).get("touch_rate"),
            "condor": condor,
            **dec,
        })
    out.sort(key=lambda r: -(r["rank"] or -99))
    return out


def full_report(code: str, as_of: date | None = None) -> dict | None:
    """單股：VRP + 曲面 + 回測 + 今日可落嘅 condor。"""
    code = code.zfill(5)
    iv_hist = ah.load()
    px, tv = ve.load_quotes()
    vrp = ve.analyse(code, iv_hist, px, tv)
    if not vrp:
        return None
    surf = vs.surface(code, as_of)
    bt = _load_bt().get(code)
    dec = _decide(vrp, surf, bt)
    condor = ce.build(code, as_of)
    return {"vrp": vrp, "surface": surf, "backtest": bt,
            "decision": dec, "condor": condor}


def _fmt_table(rows: list[dict], limit: int) -> str:
    hdr = (f"{'代號':>6} {'名稱':<20} {'IV':>6} {'IVR':>5} {'VRP':>5} "
           f"{'FF':>5} {'回測勝':>6} {'回報':>7} {'Sharpe':>7}  策略")
    out = [hdr, "─" * 108]
    for r in rows[:limit]:
        ff = r.get("forward_factor")
        out.append(
            f"{r['stock_code']:>6} {(r['name'] or '')[:20]:<20} "
            f"{(r['iv'] or 0):>6.1f} {(r.get('iv_rank') or 0):>4.0f}% "
            f"{(r.get('vrp_score') or 0):>5.1f} "
            f"{(ff if ff is not None else 0):>5.2f} "
            f"{(r.get('bt_win') or 0):>5.0f}% {(r.get('bt_ret') or 0):>6.0f}% "
            f"{(r.get('bt_sharpe') or 0):>7.2f}  {r['strategy']}"
        )
    return "\n".join(out)


def _fmt_full(rep: dict) -> str:
    v, s, bt, d = rep["vrp"], rep["surface"], rep["backtest"], rep["decision"]
    L = [
        f"{v['stock_code']} {v['name']}    現價 {v['close']}    ({v['date']})",
        "",
        "── 波幅定價 ──",
        f"  ATM IV                {v['iv']:.1f}%   (DTE {v['dte']})",
        f"  近期已實現波幅        {v.get('rv_recent') or '—'}",
        f"  IV / RV               {v.get('iv_rv_ratio') or '—'}",
        f"  IV Rank               {(v.get('iv_rank') or 0):.0f}%",
        "",
        "── 歷史 VRP（前瞻 21 日）──",
        f"  觀測                  {(v.get('vrp') or {}).get('n')} 個",
        f"  VRP 均值              {(v.get('vrp') or {}).get('mean')}",
        f"  勝率                  {(v.get('vrp') or {}).get('win_rate')}%",
        f"  Sharpe                {(v.get('vrp') or {}).get('sharpe')}",
        f"  評級                  {v.get('grade')}  ({v.get('score')}/10)",
    ]
    if s:
        L += [
            "",
            "── 期限結構 ──",
            f"  結構                  {s.get('structure')}",
            f"  斜率                  {(s.get('slope_pct') or 0):+.1f}%",
            f"  前向波幅              {s.get('forward_vol')}",
            f"  前向係數              {s.get('forward_factor')}",
            f"  Skew (25d)            {s.get('front_skew')}",
        ]
    if bt:
        L += [
            "",
            "── Iron Condor 回測實績 ──",
            f"  交易筆數              {bt['n_trades']}",
            f"  實際勝率              {bt['win_rate']:.1f}%   (模型估 {bt.get('avg_p_win_model')}%)",
            f"  平均風險回報          {bt['avg_ret_on_risk']:.1f}%",
            f"  觸價率                {bt['touch_rate']:.1f}%",
            f"  Sharpe                {(bt.get('sharpe') or 0):.2f}",
            f"  最好 / 最壞           {bt['best']:.2f} / {bt['worst']:.2f}",
        ]
    c = rep.get("condor")
    if c:
        L += ["", "── 今日可落嘅 Iron Condor ──",
              f"  到期 {c['expiry']}  DTE {c['dte']}",
              f"  買 Put   {c['long_put']:>8.2f} @ {c['lp_px']:>6.2f}",
              f"  賣 Put   {c['short_put']:>8.2f} @ {c['sp_px']:>6.2f}",
              f"  賣 Call  {c['short_call']:>8.2f} @ {c['sc_px']:>6.2f}",
              f"  買 Call  {c['long_call']:>8.2f} @ {c['lc_px']:>6.2f}",
              f"  淨收入   {c['credit']:>8.2f}   最大蝕 {c['max_loss']:.2f}",
              f"  盈虧平衡 {c['be_low']:.2f} — {c['be_high']:.2f}  (±{c['range_pct']:.1f}%)",
              f"  模型勝率 {c['p_win_model']:.1f}%"]
    L += ["", "── 系統決策 ──", f"  ▶ {d['strategy']}"]
    if d.get("overlay"):
        L.append(f"     ↳ {d['overlay']}")
    for r in d.get("reasons", []):
        L.append(f"     · {r}")
    for b in d.get("blocks", []):
        L.append(f"     ✗ {b}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="波幅交易系統（VRP + 期限結構 + Iron Condor）")
    ap.add_argument("--stock", help="單股全套分析")
    ap.add_argument("--top", type=int, default=25, help="表格顯示幾多行")
    ap.add_argument("--all", action="store_true", help="連迴避／觀察都顯示")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.stock:
        rep = full_report(a.stock)
        if not rep:
            print(f"{a.stock} 冇足夠數據")
            return
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str)
              if a.json else _fmt_full(rep))
        return

    rows = build_signals()
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    if a.json:
        print(json.dumps(rows[:a.top], ensure_ascii=False, indent=2, default=str))
        return

    tradeable = [r for r in rows if r["strategy"] not in {"迴避", "觀察"}]
    show = rows if a.all else tradeable
    print(f"=== 波幅交易系統訊號（{rows[0]['date'] if rows else '—'}）===\n")
    print(_fmt_table(show, a.top))
    print(f"\n候選 {len(tradeable)} 隻 / 全市場 {len(rows)} 隻。→ {OUT}")

    if tradeable:
        best = tradeable[0]
        print(f"\n▶ 首選：{best['stock_code']} {best['name']} — {best['strategy']}")
        for r in best["reasons"]:
            print(f"   · {r}")


if __name__ == "__main__":
    main()
