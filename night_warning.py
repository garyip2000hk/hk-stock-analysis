"""night_warning.py — ES/NQ 夜盤早段預警（恒指 Short Strangle 風險監察）.

美股收市後、恒指開市前（22:30–03:00 HKT），用 IB 小時 bar（useRTH=False，
包 Globex 夜盤）計 ES/NQ 對上一個美股日市收市價嘅隔夜變化。大單向隔夜跳空
會威脅 Short Strangle 行使價，呢個腳本出預警分級：

  GREEN  |ES| < 0.6% 且 |NQ| < 0.8%   — 正常，唔通知
  AMBER  |ES| ≥ 0.6% 或 |NQ| ≥ 0.8%   — 留意（通知）
  RED    |ES| ≥ 1.0% 或 |NQ| ≥ 1.3%   — 高風險（通知，建議朝早開市前檢查倉）

參考價唔使時區計算：kline_ib_day.parquet ES/NQ 尋日日市收市（RTH settle）
直接做 base；最新價 = 最近一小時 bar close（包夜盤）。

用法：python3 night_warning.py [--json]
輸出：options_data/night_warning.json + stdout 人類可讀報告
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

KLINE_PATH = Path("/home/workspace/Desktop/db/IB/Kline/kline_ib_day.parquet")
OUT_PATH = Path("/home/workspace/stock-analysis/options_data/night_warning.json")
HOST, PORT = "127.0.0.1", 4001

FUT = {"ES": ("CME", "USD"), "NQ": ("CME", "USD")}

# 預警門檻（%）
AMBER = {"ES": 0.6, "NQ": 0.8}
RED = {"ES": 1.0, "NQ": 1.3}

# HSI 對 ES 隔夜 % 變化嘅歷史 beta（粗略，自動化報告用）
HSI_BETA = 0.85


def now_hkt() -> datetime:
    return datetime.utcnow() + timedelta(hours=8)


def base_close(sym: str) -> tuple[float, str] | None:
    """尋日美股日市收市價（kline_ib_day RTH settle）。"""
    if not KLINE_PATH.exists():
        return None
    df = pd.read_parquet(KLINE_PATH)
    df = df[(df.symbol == sym) & (df.sec_type == "future")]
    if not len(df):
        return None
    df = df.sort_values("date")
    today = now_hkt().strftime("%Y-%m-%d")
    df = df[df.date < today]
    if not len(df):
        return None
    row = df.iloc[-1]
    return float(row.close), str(row.date)


async def resolve_front(ib, sym: str, exch: str, cur: str):
    """揀 front month：未過期入面最近到期嗰隻。"""
    from ib_async import Contract

    det = await ib.reqContractDetailsAsync(
        Contract(symbol=sym, secType="FUT", exchange=exch, currency=cur))
    today = now_hkt().strftime("%Y%m%d")
    rows = []
    for d in det or []:
        c = d.contract
        ltd = getattr(c, "lastTradeDate", None) or c.lastTradeDateOrContractMonth or ""
        if ltd[:8] >= today:
            rows.append((ltd, c))
    rows.sort(key=lambda r: r[0])
    return rows[0][1] if rows else None


async def overnight_move(ib, sym: str) -> dict:
    from ib_async import Contract

    exch, cur = FUT[sym]
    c = await resolve_front(ib, sym, exch, cur)
    if c is None:
        return {"symbol": sym, "ok": False, "error": "搵唔到 front month"}

    bars = await ib.reqHistoricalDataAsync(
        c, endDateTime="", barSizeSetting="1 hour",
        durationStr="2 D", useRTH=False, whatToShow="TRADES", formatDate=1)
    if not bars:
        return {"symbol": sym, "ok": False, "error": "冇夜盤數據"}

    b = base_close(sym)
    if b is None:
        return {"symbol": sym, "ok": False, "error": "kline 冇 base close"}
    base, base_date = b
    last = bars[-1]
    move = (last.close / base - 1.0) * 100.0

    # 夜盤高點／低點（最近 12 個鐘 bar，覆盖美股收市後）
    recent = [x for x in bars if x.date >= last.date - timedelta(hours=12)]
    hi = max(x.high for x in recent)
    lo = min(x.low for x in recent)

    level = "GREEN"
    if abs(move) >= RED[sym]:
        level = "RED"
    elif abs(move) >= AMBER[sym]:
        level = "AMBER"

    return {
        "symbol": sym, "ok": True,
        "front_month": c.localSymbol,
        "base_close": round(base, 2), "base_date": base_date,
        "last_price": round(last.close, 2),
        "last_bar_utc": str(last.date),
        "move_pct": round(move, 2),
        "night_high": round(hi, 2), "night_low": round(lo, 2),
        "level": level,
    }


def build_report(moves: dict[str, dict]) -> dict:
    es, nq = moves.get("ES", {}), moves.get("NQ", {})
    overall = "GREEN"
    for m in (es, nq):
        if m.get("level") == "RED":
            overall = "RED"
        elif m.get("level") == "AMBER" and overall != "RED":
            overall = "AMBER"

    est = None
    if es.get("ok"):
        est = round(es["move_pct"] * HSI_BETA, 1)  # 估恒指開市 % 變化

    rep = {
        "ts_utc": datetime.utcnow().isoformat(timespec="seconds"),
        "ts_hkt": now_hkt().strftime("%Y-%m-%d %H:%M"),
        "overall": overall,
        "hsi_gap_estimate_pct": est,
        "moves": moves,
        "action": {
            "GREEN": "隔夜市況平靜，Short Strangle 倉照舊。",
            "AMBER": "隔夜有暗流——朝早 09:30 tick 前檢查 SP/SC 行使價同指數距離。",
            "RED": "隔夜大單向——恒指料跳空開，Short Strangle 有被穿風險。"
                  "朝早必須先睇 /signal 再決定平唔平倉／加對沖。",
        }[overall],
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    return rep


def print_report(rep: dict) -> str:
    lines = [f"🌙 夜盤預警 {rep['ts_hkt']} HKT — {rep['overall']}"]
    for sym in ("ES", "NQ"):
        m = rep["moves"].get(sym, {})
        if m.get("ok"):
            arrow = "▲" if m["move_pct"] > 0 else "▼"
            lines.append(
                f"  {sym} {m['front_month']}: {m['last_price']} "
                f"{arrow}{m['move_pct']:+.2f}% vs {m['base_date']} 收市 {m['base_close']}"
                f"（夜盤 {m['night_low']}–{m['night_high']}）{m['level']}")
        else:
            lines.append(f"  {sym}: 攞唔到數據（{m.get('error')}）")
    est = rep.get("hsi_gap_estimate_pct")
    if est is not None:
        lines.append(f"  恒指開市估算: {est:+.1f}%")
    if rep.get("action"):
        lines.append(f"  → {rep['action']}")
    return "\n".join(lines)


async def run() -> dict:
    from ib_async import IB

    ib = IB()
    try:
        await asyncio.wait_for(
            ib.connectAsync(HOST, PORT, clientId=95, timeout=15), timeout=25)
    except Exception as e:
        rep = {"ts_hkt": now_hkt().strftime("%Y-%m-%d %H:%M"),
               "overall": "UNKNOWN", "error": f"IB Gateway 連唔到: {e}",
               "moves": {}}
        return rep

    moves = {}
    for sym in ("ES", "NQ"):
        try:
            moves[sym] = await overnight_move(ib, sym)
        except Exception as e:
            moves[sym] = {"symbol": sym, "ok": False, "error": str(e)}
        await asyncio.sleep(2)
    ib.disconnect()

    return build_report(moves) if any(m.get("ok") for m in moves.values()) else {
        "ts_hkt": now_hkt().strftime("%Y-%m-%d %H:%M"), "overall": "UNKNOWN",
        "error": "兩隻都攞唔到數據", "moves": moves}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rep = asyncio.run(run())
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(print_report(rep) if rep.get("moves") else json.dumps(
            rep, ensure_ascii=False))


if __name__ == "__main__":
    main()
