"""CCASS × 期權資金流交叉分析 — 大戶暗中收貨 vs 期權大單異動。

思路：兩邊數據講同一個故事先算真訊號。

  CCASS 邊（誰在收貨）
    · c5% / c10% = 前 5 / 前 10 大參與者佔已發行股數比例（HKEX dailylog 官方數）
    · Δc10 (5日 / 20日)  = 歸邊速度；升 = 貨源集中（收貨），跌 = 派貨
    · Δintermed% = 全體 CCASS 中介持股佔比變化；升 = 街貨流入券商（多數係散戶接貨或北水）
    · 參與者數目變化 = 持股人數收窄 = 洗籌

  期權邊（誰在落注）
    · call_vol_z / put_vol_z = 今日成交對比自身 20 日平均嘅標準分（>2 就係異動大單）
    · Δcall_oi / Δput_oi (1日 / 5日) = 未平倉真金白銀新開倉，唔係當日炒賣
    · pcr_vol / pcr_oi 變化 = 資金情緒轉向
    · IV 變化 = 市場有冇提前知情（IV 靜靜升 = 有人偷偷買）

  交叉規則（重點：CCASS 同期權方向一致才出 Alert）
    看多共振  收貨（Δc10 > 0）+ Call OI 增 + Call 成交異動 → 「大戶吸貨・Call 追注」
    看空共振  派貨（Δc10 < 0）+ Put OI 增 + Put 成交異動  → 「大戶派貨・Put 對沖」
    知情靜吸  收貨 + IV 未升（IV Rank 低）+ Call OI 增     → 「靜吸未反映・Call 仍平」← 最有價值
    出貨掩護  派貨 + Call 成交異動（散戶接火棒）           → 「派貨中・小心 Call 陷阱」
    純期權異動 CCASS 無明顯動作但期權大異動                → 「純期權異動・待確認」

CLI:
    python3 ccass_options_cross.py                  # 全市場交叉掃描（分數高 → 低）
    python3 ccass_options_cross.py --stock 00700    # 單隻詳細拆解
    python3 ccass_options_cross.py --json           # 出 JSON 供 API 用
    python3 ccass_options_cross.py --bullish        # 只睇看多共振
    python3 ccass_options_cross.py --bearish        # 只睇看空共振
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import ccass_snapshot as cs

HERE = Path(__file__).parent
IV_HISTORY = HERE / "options_data" / "iv_history.parquet"
OUT_JSON = HERE / "options_data" / "ccass_options_cross.json"

VOL_WINDOW = 20      # 成交異動基準窗口
OI_SHORT = 1         # OI 短期變化（日）
OI_MED = 5           # OI 中期變化（日）
CC_SHORT = 5         # CCASS 短期變化（交易日）
CC_MED = 20          # CCASS 中期變化（交易日）

VOL_Z_ALERT = 2.0    # 成交標準分門檻
OI_PCT_ALERT = 5.0   # OI 變化百分比門檻
CONC_ALERT = 0.15    # 集中度變化門檻（百分點）
MIN_CONTRACTS = 200  # 冷門合約門檻：今日成交少過呢個數，z-score 唔算異動
Z_CAP = 12.0         # z-score 上限，避免冷門股少量成交爆出天文數字


# ---------------------------------------------------------------- 數據載入

def load_options() -> pd.DataFrame:
    if not IV_HISTORY.exists():
        raise SystemExit(f"未見 {IV_HISTORY}，先跑 options_scraper.py")
    df = pd.read_parquet(IV_HISTORY)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["stock_code", "date"])


def load_ccass_daily(stock_codes: list[str]) -> tuple[pd.DataFrame, dict[int, str]]:
    """一次過由 dailylog.parquet 讀出所有期權標的嘅每日集中度數據。"""
    import duckdb

    id_to_code: dict[int, str] = {}
    for code in stock_codes:
        iid = cs.resolve_issue_id(code)
        if iid is not None:
            id_to_code[iid] = code
    if not id_to_code:
        return pd.DataFrame(), {}

    paths = cs._existing(cs.DAILYLOG_SOURCES)
    if not paths:
        return pd.DataFrame(), {}

    ids = ",".join(str(i) for i in id_to_code)
    sql = f"""
        SELECT issue_id, at_date, c5, c10, intermed_hldg, intermed_cnt
        FROM read_parquet(?)
        WHERE issue_id IN ({ids})
        ORDER BY issue_id, at_date
    """
    with duckdb.connect() as con:
        df = con.execute(sql, [paths]).df()

    df["stock_code"] = df.issue_id.map(id_to_code)
    df["issued"] = df.stock_code.map(lambda c: cs.issued_shares(c))
    df = df[df.issued.notna() & (df.issued > 0)].copy()
    df["c5_pct"] = df.c5 / df.issued * 100
    df["c10_pct"] = df.c10 / df.issued * 100
    df["intermed_pct"] = df.intermed_hldg / df.issued * 100
    return df.sort_values(["stock_code", "at_date"]), id_to_code


# ---------------------------------------------------------------- 計算

def _delta(series: pd.Series, lag: int) -> float | None:
    if len(series) <= lag:
        return None
    return float(series.iloc[-1] - series.iloc[-1 - lag])


def _pct_change(series: pd.Series, lag: int) -> float | None:
    if len(series) <= lag:
        return None
    prev = series.iloc[-1 - lag]
    if not prev:
        return None
    return float((series.iloc[-1] - prev) / prev * 100)


def _zscore(series: pd.Series, window: int = VOL_WINDOW,
            min_contracts: float = MIN_CONTRACTS) -> float | None:
    """今日值對比過去 window 日嘅標準分（唔含今日）。

    冷門合約保護：今日成交細過 min_contracts 就唔算異動（返 None），
    因為 20 日平均近乎 0 嗰陣，幾十張都會爆出離譜 z-score。
    另外 z 會封頂喺 ±Z_CAP，令排序唔會被一兩隻極端值霸佔。
    """
    if len(series) < window + 1:
        return None
    today = float(series.iloc[-1])
    if today < min_contracts:
        return None
    hist = series.iloc[-(window + 1):-1]
    mu, sd = hist.mean(), hist.std(ddof=1)
    if not sd or np.isnan(sd):
        return None
    z = (today - mu) / sd
    return float(max(-Z_CAP, min(Z_CAP, z)))


def ccass_metrics(g: pd.DataFrame) -> dict:
    """單隻股票嘅 CCASS 歸邊指標。"""
    return {
        "ccass_date": str(g.at_date.iloc[-1])[:10],
        "c5_pct": round(float(g.c5_pct.iloc[-1]), 2),
        "c10_pct": round(float(g.c10_pct.iloc[-1]), 2),
        "intermed_pct": round(float(g.intermed_pct.iloc[-1]), 2),
        "participants": int(g.intermed_cnt.iloc[-1]),
        "d_c10_5d": _round(_delta(g.c10_pct, CC_SHORT), 3),
        "d_c10_20d": _round(_delta(g.c10_pct, CC_MED), 3),
        "d_c5_5d": _round(_delta(g.c5_pct, CC_SHORT), 3),
        "d_intermed_5d": _round(_delta(g.intermed_pct, CC_SHORT), 3),
        "d_intermed_20d": _round(_delta(g.intermed_pct, CC_MED), 3),
        "d_participants_20d": _round(_delta(g.intermed_cnt.astype(float), CC_MED), 0),
    }


def option_metrics(g: pd.DataFrame) -> dict:
    """單隻股票嘅期權資金流指標。"""
    last = g.iloc[-1]
    return {
        "date": str(last.date)[:10],
        "name": last["name"],
        "close": _round(last.close, 3),
        "iv": _round(last.iv, 1),
        "d_iv_1d": _round(_delta(g.iv.astype(float), 1), 1),
        "d_iv_5d": _round(_delta(g.iv.astype(float), 5), 1),
        "volume": int(last.volume or 0),
        "call_vol": int(last.call_vol or 0),
        "put_vol": int(last.put_vol or 0),
        "call_vol_z": _round(_zscore(g.call_vol.astype(float)), 2),
        "put_vol_z": _round(_zscore(g.put_vol.astype(float)), 2),
        "vol_z": _round(_zscore(g.volume.astype(float)), 2),
        "oi": int(last.oi or 0),
        "call_oi": int(last.call_oi or 0),
        "put_oi": int(last.put_oi or 0),
        "d_call_oi_1d": _round(_pct_change(g.call_oi.astype(float), OI_SHORT), 2),
        "d_put_oi_1d": _round(_pct_change(g.put_oi.astype(float), OI_SHORT), 2),
        "d_call_oi_5d": _round(_pct_change(g.call_oi.astype(float), OI_MED), 2),
        "d_put_oi_5d": _round(_pct_change(g.put_oi.astype(float), OI_MED), 2),
        "pcr_vol": _round(last.pcr_vol, 3),
        "pcr_oi": _round(last.pcr_oi, 3),
        "d_pcr_oi_5d": _round(_delta(g.pcr_oi.astype(float), 5), 3),
    }


def _round(v, nd):
    if v is None:
        return None
    try:
        if isinstance(v, float) and np.isnan(v):
            return None
    except TypeError:
        return None
    r = round(float(v), nd)
    return int(r) if nd == 0 else r


def iv_rank(g: pd.DataFrame) -> float | None:
    """IV 喺自身歷史嘅位置（0-100），用嚟判斷「靜吸未反映」。"""
    iv = g.iv.astype(float).dropna()
    if len(iv) < 30:
        return None
    lo, hi, cur = iv.min(), iv.max(), iv.iloc[-1]
    if hi == lo:
        return None
    return round(float((cur - lo) / (hi - lo) * 100), 1)


# ---------------------------------------------------------------- 交叉規則

def classify(cc: dict, op: dict, ivr: float | None) -> dict:
    """交叉 CCASS 同期權方向，出訊號 + 分數。"""
    d_c10 = cc.get("d_c10_5d") or 0.0
    d_c10_m = cc.get("d_c10_20d") or 0.0
    d_int = cc.get("d_intermed_5d") or 0.0
    cvz = op.get("call_vol_z") or 0.0
    pvz = op.get("put_vol_z") or 0.0
    d_coi = op.get("d_call_oi_5d") or 0.0
    d_poi = op.get("d_put_oi_5d") or 0.0
    d_iv = op.get("d_iv_5d") or 0.0

    accumulating = d_c10 >= CONC_ALERT or d_c10_m >= CONC_ALERT * 2
    distributing = d_c10 <= -CONC_ALERT or d_c10_m <= -CONC_ALERT * 2
    call_hot = cvz >= VOL_Z_ALERT or d_coi >= OI_PCT_ALERT
    put_hot = pvz >= VOL_Z_ALERT or d_poi >= OI_PCT_ALERT

    signals: list[str] = []
    score = 0.0
    bias = "中性"

    if accumulating and call_hot:
        signals.append("大戶吸貨・Call 追注")
        score += 4
        bias = "看多"
        if ivr is not None and ivr <= 40:
            signals.append("IV 仍低・期權未反映")
            score += 2
    elif distributing and put_hot:
        signals.append("大戶派貨・Put 對沖")
        score += 4
        bias = "看空"
    elif accumulating and put_hot and not call_hot:
        signals.append("收貨但 Put 增・或係鎖倉對沖")
        score += 1.5
        bias = "分歧"
    elif distributing and call_hot and not put_hot:
        signals.append("派貨中・小心 Call 陷阱")
        score += 2.5
        bias = "看空"
    elif accumulating and (ivr is not None and ivr <= 30) and d_coi > 0:
        signals.append("靜吸未反映・Call 仍平")
        score += 3
        bias = "看多"
    elif call_hot or put_hot:
        signals.append("純期權異動・CCASS 未確認")
        score += 1
        call_force = cvz + d_coi / 5
        put_force = pvz + d_poi / 5
        bias = "看多" if call_force >= put_force else "看空"
    elif accumulating:
        signals.append("純 CCASS 歸邊・期權未跟")
        score += 0.8
        bias = "看多"
    elif distributing:
        signals.append("純 CCASS 派貨")
        score += 0.8
        bias = "看空"

    # 加分項
    if abs(cvz) >= 3 or abs(pvz) >= 3:
        signals.append(f"成交極端異動 (z={max(cvz, pvz):.1f})")
        score += 1
    if d_int <= -0.3 and accumulating:
        signals.append("街貨轉入大戶手")
        score += 1
    if (cc.get("d_participants_20d") or 0) <= -8 and accumulating:
        signals.append("持股人數收窄・洗籌")
        score += 0.8
    if d_iv >= 8 and accumulating:
        signals.append("IV 靜靜升・或有知情資金")
        score += 1.5

    return {
        "bias": bias,
        "signals": signals,
        "score": round(score, 1),
        "accumulating": bool(accumulating),
        "distributing": bool(distributing),
        "call_hot": bool(call_hot),
        "put_hot": bool(put_hot),
        "iv_rank": ivr,
    }


# ---------------------------------------------------------------- 主流程

def analyse(stock: str | None = None) -> list[dict]:
    opts = load_options()
    codes = sorted(opts.stock_code.unique())
    if stock:
        code = cs.pad_code(stock)
        codes = [c for c in codes if c == code]
        if not codes:
            raise SystemExit(f"{code} 唔喺期權標的名單內")

    ccass, _ = load_ccass_daily(codes)
    cc_groups = {c: g for c, g in ccass.groupby("stock_code")} if len(ccass) else {}

    rows: list[dict] = []
    for code, g in opts.groupby("stock_code"):
        if code not in codes:
            continue
        op = option_metrics(g)
        cg = cc_groups.get(code)
        cc = ccass_metrics(cg) if cg is not None and len(cg) else {}
        if not cc:
            continue
        ivr = iv_rank(g)
        verdict = classify(cc, op, ivr)
        rows.append({"stock_code": code, **op, **cc, **verdict})

    rows.sort(key=lambda r: (-r["score"], -(r.get("call_vol_z") or 0)))
    return rows


# ---------------------------------------------------------------- 輸出

def fmt_table(rows: list[dict], limit: int = 25) -> str:
    head = (f"{'代號':<7}{'名稱':<20}{'收市':>9}{'Δc10(5d)':>10}{'CallZ':>7}"
            f"{'PutZ':>7}{'ΔCallOI':>9}{'ΔPutOI':>9}{'IVR':>6}{'分':>5}  訊號")
    lines = [head, "─" * 150]
    for r in rows[:limit]:
        name = (r.get("name") or "")[:18]
        lines.append(
            f"{r['stock_code']:<7}{name:<20}{_s(r.get('close')):>9}"
            f"{_s(r.get('d_c10_5d')):>10}{_s(r.get('call_vol_z')):>7}"
            f"{_s(r.get('put_vol_z')):>7}{_s(r.get('d_call_oi_5d')):>9}"
            f"{_s(r.get('d_put_oi_5d')):>9}{_s(r.get('iv_rank')):>6}"
            f"{r['score']:>5}  {r['bias']}｜{'、'.join(r['signals'])}"
        )
    return "\n".join(lines)


def _s(v):
    return "—" if v is None else f"{v}"


def detail(r: dict) -> str:
    L = [
        f"{r['stock_code']} {r.get('name')}    收市 {r.get('close')}    "
        f"期權 {r.get('date')} / CCASS {r.get('ccass_date')}",
        "",
        "  ── CCASS 歸邊 ──",
        f"  前 5 / 前 10 大佔比      {r.get('c5_pct')}% / {r.get('c10_pct')}%",
        f"  Δ前 10 大 (5日 / 20日)   {_s(r.get('d_c10_5d'))} / {_s(r.get('d_c10_20d'))} 百分點",
        f"  中介總持股佔比           {r.get('intermed_pct')}%  (5日 {_s(r.get('d_intermed_5d'))})",
        f"  參與者數目               {r.get('participants')}  (20日 {_s(r.get('d_participants_20d'))})",
        "",
        "  ── 期權資金流 ──",
        f"  ATM IV                   {r.get('iv')}%   (5日 {_s(r.get('d_iv_5d'))})   IV Rank {_s(r.get('iv_rank'))}",
        f"  Call / Put 成交          {r.get('call_vol'):,} / {r.get('put_vol'):,}",
        f"  成交異動 z (Call/Put)    {_s(r.get('call_vol_z'))} / {_s(r.get('put_vol_z'))}",
        f"  Call / Put 未平倉        {r.get('call_oi'):,} / {r.get('put_oi'):,}",
        f"  ΔOI 5日 (Call/Put)       {_s(r.get('d_call_oi_5d'))}% / {_s(r.get('d_put_oi_5d'))}%",
        f"  Put/Call (成交 / OI)     {r.get('pcr_vol')} / {r.get('pcr_oi')}  (OI 5日 {_s(r.get('d_pcr_oi_5d'))})",
        "",
        f"  ⇒ 交叉判讀   【{r['bias']}】 分數 {r['score']}",
    ]
    for s in r["signals"]:
        L.append(f"     · {s}")
    if not r["signals"]:
        L.append("     · 兩邊都無明顯異動")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="CCASS × 期權資金流交叉分析")
    ap.add_argument("--stock", help="單隻股票代號")
    ap.add_argument("--json", action="store_true", help="出 JSON")
    ap.add_argument("--bullish", action="store_true", help="只睇看多")
    ap.add_argument("--bearish", action="store_true", help="只睇看空")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args()

    rows = analyse(args.stock)

    if args.json:
        OUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str))
        print(json.dumps(rows, ensure_ascii=False, default=str))
        return

    if args.stock:
        print(detail(rows[0]) if rows else "冇數據")
        return

    if args.bullish:
        rows = [r for r in rows if r["bias"] == "看多"]
        print("=== CCASS × 期權 看多共振 ===\n")
    elif args.bearish:
        rows = [r for r in rows if r["bias"] == "看空"]
        print("=== CCASS × 期權 看空共振 ===\n")
    else:
        d = rows[0]["date"] if rows else "?"
        cd = rows[0].get("ccass_date") if rows else "?"
        print(f"=== CCASS × 期權交叉掃描   期權 {d} / CCASS {cd}   {len(rows)} 隻 ===\n")

    print(fmt_table(rows, args.limit))


if __name__ == "__main__":
    main()
