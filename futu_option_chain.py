"""futu_option_chain.py — 由 Futu OpenD 攞期權鏈 + 真實 bid/ask。

## 為咩要呢個模組

整套系統嘅期權數據來源係 HKEX 每日報告（`options_scraper.py` →
`options_chain.py`），即係：

  · 只有**收市後**數據，冇 intraday
  · 只有**結算價（settlement）**，冇真實 bid/ask
  · 只有交易所公佈嘅 ATM IV，冇逐個行使價嘅可成交價

而審查報告最致命嘅一點（A4）就係：結算價唔係可成交價。4 條腳每腳
食中價 3% 就等於 12%，剛好等於 `MIN_CREDIT_RATIO` 全部門檻。**冇真實
bid/ask，就永遠無法證明策略有 edge。**

富途 OpenD 嘅 `OpenQuoteContext` 本身有齊期權鏈同買賣盤：

    get_option_expiration_date()  → 到期日清單
    get_option_chain()            → 逐個行使價嘅合約代碼
    subscribe(ORDER_BOOK)         → 逐檔買賣盤
    get_market_snapshot()         → IV / greeks / 未平倉

呢個模組就係把上面四樣包成一個 append-only 快照器，寫入
`options_data/chain_live.parquet`，再由 `costs.from_measured_spreads()`
反推真實 `slippage_per_leg` 餵返回測。**呢個係 Stage 2 唯一嘅驗證路徑。**

## 前置

  1. OpenD 已喺 127.0.0.1:11111 行緊兼登入（同 `futu-signal-hub` 一樣）
  2. pip install futu-api
  3. 帳戶有港股期權行情權限（order book 需要 LV2；冇就自動降級成
     只有 last / IV，並喺輸出標記 `has_book=False`）

## 配額注意

  · `get_option_chain` 屬行情查詢，有每 30 秒次數限制 → 內建 sleep
  · ORDER_BOOK 訂閱佔用訂閱額度，逐批訂閱 → 讀 → 退訂
  · 同 `hk-stock-analysis/futu_data_importer.py` 一樣嘅 chunk + sleep 節奏

CLI:
    python3 futu_option_chain.py --expiries 00700
    python3 futu_option_chain.py --chain 00700 --min-dte 20 --max-dte 60
    python3 futu_option_chain.py --snapshot 00700 09988 --min-dte 20 --max-dte 60
    python3 futu_option_chain.py --snapshot-underlyings   # 用 condor 候選池
    python3 futu_option_chain.py --spread-report
    python3 futu_option_chain.py --health
"""

from __future__ import annotations

import argparse
import os
import socket
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

BASE = Path(__file__).parent
DATA_DIR = BASE / "options_data"
LIVE_CHAIN = DATA_DIR / "chain_live.parquet"

HOST = os.environ.get("OPEND_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPEND_PORT", "11111"))

CHAIN_SLEEP = 0.6          # 每次 get_option_chain 之間
BOOK_SLEEP = 0.12          # 每次 get_order_book 之間
SUB_CHUNK = 100            # 每批訂閱幾多張合約（order book 額度）
SNAP_CHUNK = 200           # get_market_snapshot 每批
MAX_STRIKES_PER_SIDE = 12  # 只要現價附近 N 個行使價（避免燒配額）

COLUMNS = [
    "ts", "date", "owner_code", "code", "expiry", "dte", "strike", "type",
    "bid", "bid_vol", "ask", "ask_vol", "mid", "spread", "spread_pct",
    "last", "iv", "delta", "gamma", "vega", "theta", "oi", "volume",
    "underlying_px", "has_book",
]


# ─────────────────────────────────────────────────────────────
# 連線
# ─────────────────────────────────────────────────────────────
def opend_alive(timeout: float = 3.0) -> bool:
    """同 hk-stock-analysis/futu_health_check.py 一致嘅探活方式。"""
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout):
            return True
    except OSError:
        return False


def connect():
    """回傳 OpenQuoteContext。futu-api 未裝／OpenD 未開會拋清晰錯誤。"""
    try:
        import futu
    except ImportError as e:
        raise RuntimeError("未安裝 futu-api：pip install futu-api") from e
    if not opend_alive():
        raise RuntimeError(f"OpenD 連唔到（{HOST}:{PORT}）—— 請先啟動並登入 OpenD")
    return futu.OpenQuoteContext(host=HOST, port=PORT)


def _ok(ret) -> bool:
    import futu
    return ret == futu.RET_OK


def _norm(code: str) -> str:
    """00700 / 700 / HK.00700 → HK.00700（同 futu-trader-extension 一致）。"""
    c = str(code).strip().upper().replace(".HK", "")
    if c.startswith("HK."):
        c = c[3:]
    return f"HK.{c.zfill(5)}"


def _owner5(code: str) -> str:
    return _norm(code)[3:]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────
# 到期日 / 鏈
# ─────────────────────────────────────────────────────────────
def expiration_dates(ctx, owner: str) -> list[dict]:
    """該標的所有期權到期日 + 剩餘日數。"""
    ret, df = ctx.get_option_expiration_date(code=_norm(owner))
    if not _ok(ret):
        log(f"✗ {owner} get_option_expiration_date: {df}")
        return []
    out = []
    for r in df.to_dict("records"):
        st = r.get("strike_time")
        if not st:
            continue
        out.append({
            "expiry": date.fromisoformat(str(st)[:10]),
            "dte": int(r.get("option_expiry_date_distance") or 0),
        })
    return sorted(out, key=lambda x: x["expiry"])


def chain(ctx, owner: str, min_dte: int = 20, max_dte: int = 60,
          index_option: bool = False) -> pd.DataFrame:
    """該標的喺 DTE 範圍內全部合約（每個到期月一次 API call）。"""
    import futu

    exps = [e for e in expiration_dates(ctx, owner)
            if min_dte <= e["dte"] <= max_dte]
    if not exps:
        log(f"  {owner} 冇到期月落喺 DTE {min_dte}-{max_dte}")
        return pd.DataFrame()

    kw = {}
    if index_option and hasattr(futu, "IndexOptionType"):
        kw["index_option_type"] = futu.IndexOptionType.NORMAL

    frames = []
    for e in exps:
        d = e["expiry"].isoformat()
        ret, df = ctx.get_option_chain(
            code=_norm(owner), start=d, end=d,
            option_type=futu.OptionType.ALL,
            option_cond_type=futu.OptionCondType.ALL, **kw)
        time.sleep(CHAIN_SLEEP)
        if not _ok(ret):
            log(f"  ✗ {owner} {d} get_option_chain: {df}")
            continue
        if df is None or df.empty:
            continue
        df = df.copy()
        df["expiry"] = e["expiry"]
        df["dte"] = e["dte"]
        frames.append(df)

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if "suspension" in out.columns:
        out = out[out.suspension != True]      # noqa: E712 — futu 回 bool/None
    return out.reset_index(drop=True)


def _near_the_money(ch: pd.DataFrame, spot: float,
                    per_side: int = MAX_STRIKES_PER_SIDE) -> pd.DataFrame:
    """只保留現價上下各 N 個行使價 —— 深度價外冇人報價，訂閱佢係浪費配額。"""
    if ch.empty or "strike_price" not in ch.columns or not spot:
        return ch
    keep = []
    for _, g in ch.groupby("expiry"):
        ks = sorted(g.strike_price.dropna().unique())
        below = [k for k in ks if k <= spot][-per_side:]
        above = [k for k in ks if k > spot][:per_side]
        keep.append(g[g.strike_price.isin(set(below) | set(above))])
    return pd.concat(keep, ignore_index=True) if keep else ch


def underlying_price(ctx, owner: str) -> float | None:
    ret, df = ctx.get_market_snapshot([_norm(owner)])
    if not _ok(ret) or df is None or df.empty:
        return None
    v = df.last_price.iloc[0]
    return None if pd.isna(v) else float(v)


# ─────────────────────────────────────────────────────────────
# 真實 bid/ask
# ─────────────────────────────────────────────────────────────
def _subscribe(ctx, codes: list[str], want_book: bool = True) -> bool:
    import futu
    subs = [futu.SubType.QUOTE]
    if want_book:
        subs.append(futu.SubType.ORDER_BOOK)
    ret, msg = ctx.subscribe(codes, subs)
    if not _ok(ret):
        if want_book:
            log(f"  ⚠ ORDER_BOOK 訂閱失敗（{msg}）→ 降級只用 QUOTE，冇真 bid/ask")
            ret2, msg2 = ctx.subscribe(codes, [futu.SubType.QUOTE])
            if not _ok(ret2):
                log(f"  ✗ QUOTE 訂閱都失敗: {msg2}")
                return False
            return False
        log(f"  ✗ 訂閱失敗: {msg}")
        return False
    return want_book


def _unsubscribe(ctx, codes: list[str], had_book: bool) -> None:
    import futu
    subs = [futu.SubType.QUOTE] + ([futu.SubType.ORDER_BOOK] if had_book else [])
    try:
        ctx.unsubscribe(codes, subs)
    except Exception:
        pass


def _order_book(ctx, code: str) -> dict:
    ret, data = ctx.get_order_book(code, num=1)
    if not _ok(ret) or not isinstance(data, dict):
        return {}
    bid = (data.get("Bid") or [None])[0]
    ask = (data.get("Ask") or [None])[0]
    out = {}
    if bid:
        out["bid"], out["bid_vol"] = float(bid[0]), float(bid[1])
    if ask:
        out["ask"], out["ask_vol"] = float(ask[0]), float(ask[1])
    return out


def _snapshot_fields(ctx, codes: list[str]) -> pd.DataFrame:
    frames = []
    for i in range(0, len(codes), SNAP_CHUNK):
        ret, df = ctx.get_market_snapshot(codes[i:i + SNAP_CHUNK])
        time.sleep(0.5)
        if _ok(ret) and df is not None and not df.empty:
            frames.append(df)
        else:
            log(f"  ✗ snapshot chunk {i}: {df}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def quote_chain(ctx, owner: str, min_dte: int = 20, max_dte: int = 60,
                per_side: int = MAX_STRIKES_PER_SIDE,
                want_book: bool = True,
                index_option: bool = False) -> pd.DataFrame:
    """一隻標的嘅完整實時鏈：合約 + IV/greeks/OI + 真實 bid/ask。"""
    owner_n = _norm(owner)
    spot = underlying_price(ctx, owner_n)
    ch = chain(ctx, owner_n, min_dte, max_dte, index_option)
    if ch.empty:
        return pd.DataFrame(columns=COLUMNS)
    ch = _near_the_money(ch, spot or 0, per_side)
    codes = ch.code.dropna().unique().tolist()
    if not codes:
        return pd.DataFrame(columns=COLUMNS)

    log(f"  {owner_n} 現價 {spot} · {len(codes)} 張合約")

    meta = ch.set_index("code")[[c for c in
                                 ("expiry", "dte", "strike_price", "option_type")
                                 if c in ch.columns]].to_dict("index")

    rows: list[dict] = []
    ts = datetime.now()
    for i in range(0, len(codes), SUB_CHUNK):
        batch = codes[i:i + SUB_CHUNK]
        has_book = _subscribe(ctx, batch, want_book)
        time.sleep(1.0)                       # 等第一次推送到齊

        snap = _snapshot_fields(ctx, batch)
        snap_by = ({r["code"]: r for r in snap.to_dict("records")}
                   if not snap.empty else {})

        for code in batch:
            s = snap_by.get(code, {})
            m = meta.get(code, {})
            book = _order_book(ctx, code) if has_book else {}
            if has_book:
                time.sleep(BOOK_SLEEP)

            bid, ask = book.get("bid"), book.get("ask")
            mid = ((bid + ask) / 2.0 if bid and ask and ask > 0 else None)
            if mid is None:
                lp = s.get("last_price")
                mid = float(lp) if lp and not pd.isna(lp) else None
            spread = (ask - bid) if (bid and ask) else None

            ot = str(m.get("option_type") or s.get("option_type") or "").upper()
            cp = "C" if "CALL" in ot or ot == "C" else ("P" if "PUT" in ot or ot == "P" else None)
            exp = m.get("expiry")

            rows.append({
                "ts": ts,
                "date": ts.date(),
                "owner_code": _owner5(owner_n),
                "code": code,
                "expiry": exp,
                "dte": m.get("dte"),
                "strike": (float(m["strike_price"])
                           if m.get("strike_price") is not None else None),
                "type": cp,
                "bid": bid, "bid_vol": book.get("bid_vol"),
                "ask": ask, "ask_vol": book.get("ask_vol"),
                "mid": mid,
                "spread": spread,
                "spread_pct": (round(spread / mid * 100, 2)
                               if spread and mid else None),
                "last": _f(s.get("last_price")),
                "iv": _f(s.get("option_implied_volatility")),
                "delta": _f(s.get("option_delta")),
                "gamma": _f(s.get("option_gamma")),
                "vega": _f(s.get("option_vega")),
                "theta": _f(s.get("option_theta")),
                "oi": _f(s.get("option_open_interest")),
                "volume": _f(s.get("volume")),
                "underlying_px": spot,
                "has_book": bool(has_book and bid and ask),
            })

        _unsubscribe(ctx, batch, has_book)

    df = pd.DataFrame(rows)
    return df.reindex(columns=COLUMNS)


def _f(v):
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────
# 落地（append-only，同 futu_data_importer 一致口徑）
# ─────────────────────────────────────────────────────────────
def save(df: pd.DataFrame, path: Path = LIVE_CHAIN) -> int:
    """append-only 寫入，用 (ts, code) 去重。回傳總行數。"""
    if df is None or df.empty:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    out = pd.concat([old, df], ignore_index=True) if len(old) else df
    out = out.drop_duplicates(subset=["ts", "code"], keep="last")
    out = out.sort_values(["ts", "owner_code", "expiry", "strike", "type"])
    out = out.reset_index(drop=True)
    out.to_parquet(path, index=False)
    return len(out)


def load_live(as_of: date | None = None, owner_code: str | None = None,
              path: Path = LIVE_CHAIN) -> pd.DataFrame:
    """讀返實時鏈快照。as_of 為 None 就最新一日。"""
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_parquet(path)
    if df.empty:
        return df
    if owner_code is not None:
        df = df[df.owner_code.astype(str).str.zfill(5) == str(owner_code).zfill(5)]
    if as_of is None:
        if df.empty:
            return df
        as_of = pd.to_datetime(df.date).max().date()
    return df[pd.to_datetime(df.date).dt.date == as_of].reset_index(drop=True)


def snapshot(owners: list[str], min_dte: int = 20, max_dte: int = 60,
             per_side: int = MAX_STRIKES_PER_SIDE,
             want_book: bool = True, index_option: bool = False) -> dict:
    """供 daily_pipeline / cron 呼叫。OpenD 未開回 status=skipped，唔會炸 pipeline。"""
    summary: dict = {"status": "ok", "owners": len(owners), "rows": 0,
                     "with_book": 0, "median_spread_pct": None, "errors": []}
    try:
        ctx = connect()
    except Exception as e:
        return {"status": "skipped", "error": str(e)}

    frames = []
    try:
        for o in owners:
            try:
                df = quote_chain(ctx, o, min_dte, max_dte, per_side,
                                 want_book, index_option)
                if not df.empty:
                    frames.append(df)
            except Exception as e:                     # 單隻失敗唔應該中斷全部
                summary["errors"].append(f"{o}: {e}")
                log(f"  ✗ {o}: {e}")
    finally:
        try:
            ctx.close()
        except Exception:
            pass

    if not frames:
        summary["status"] = "empty"
        return summary

    all_df = pd.concat(frames, ignore_index=True)
    total = save(all_df)
    sp = all_df.spread_pct.dropna()
    summary.update({
        "rows": int(len(all_df)),
        "with_book": int(all_df.has_book.sum()),
        "median_spread_pct": round(float(sp.median()), 2) if len(sp) else None,
        "total_rows_on_disk": total,
        "path": str(LIVE_CHAIN),
    })
    return summary


# ─────────────────────────────────────────────────────────────
# 價差報告 —— Stage 2 嘅實測滑價分佈
# ─────────────────────────────────────────────────────────────
def spread_report(owner_code: str | None = None) -> str:
    df = load_live(as_of=None, owner_code=owner_code) if owner_code else None
    if df is None:
        if not LIVE_CHAIN.exists():
            return "冇實測數據。先跑：python3 futu_option_chain.py --snapshot 00700"
        df = pd.read_parquet(LIVE_CHAIN)
    if df.empty:
        return "冇實測數據。"

    d = df.dropna(subset=["bid", "ask", "mid"])
    d = d[(d.bid > 0) & (d.ask > d.bid) & (d.mid > 0)]
    if d.empty:
        return ("有快照但冇一行有真 bid/ask —— 通常係冇港股期權 LV2 行情權限，"
                "或者收市後（order book 空）。開市時段再跑一次。")

    d = d.assign(half_pct=(d.ask - d.bid) / 2.0 / d.mid * 100)
    out = [
        f"實測期權價差（{d.ts.min()} → {d.ts.max()}，{len(d)} 個報價）",
        "",
        f"{'標的':>6} {'報價數':>7} {'半價差中位':>11} {'q75':>7} {'q90':>7} "
        f"{'→ slippage_per_leg':>20}",
    ]
    for owner, g in d.groupby("owner_code"):
        h = g.half_pct
        out.append(f"{owner:>6} {len(g):>7} {h.median():>10.2f}% "
                   f"{h.quantile(.75):>6.2f}% {h.quantile(.90):>6.2f}% "
                   f"{h.median()/100:>19.4f}")
    h = d.half_pct
    out += [
        "",
        f"全體：中位 {h.median():.2f}% · q75 {h.quantile(.75):.2f}% "
        f"· q90 {h.quantile(.90):.2f}%",
        "",
        "把中位數（或 q75 更保守）填入回測：",
        f"  python3 condor_engine.py --backtest-all --slippage {h.median()/100:.4f}",
        "",
        "4 條腳開倉 + 4 條腳平倉 = 跨 8 次價差，總蒸發 ≈ "
        f"{h.median()*8:.1f}% × 各腳中價。",
    ]
    return "\n".join(out)


def _default_owners() -> list[str]:
    """預設標的：condor 回測結果最好嘅幾隻，冇就用最流通大型股。"""
    import json
    bt = BASE / "condor_backtest.json"
    if bt.exists():
        try:
            rows = json.loads(bt.read_text())
            codes = [r["stock_code"] for r in rows[:8]]
            if codes:
                return codes
        except Exception:
            pass
    return ["00700", "09988", "03690", "00941", "01299"]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Futu OpenD 期權鏈 + 真實 bid/ask 快照器")
    ap.add_argument("--health", action="store_true", help="只探 OpenD 生死")
    ap.add_argument("--expiries", help="列該標的所有到期日")
    ap.add_argument("--chain", help="列該標的合約（唔訂閱，唔攞 bid/ask）")
    ap.add_argument("--snapshot", nargs="*", metavar="CODE",
                    help="攞完整實時鏈（含 bid/ask）並落地")
    ap.add_argument("--snapshot-underlyings", action="store_true",
                    help="用 condor_backtest.json 頭 8 隻做標的")
    ap.add_argument("--spread-report", action="store_true",
                    help="由已落地數據出實測價差分佈")
    ap.add_argument("--min-dte", type=int, default=20)
    ap.add_argument("--max-dte", type=int, default=60)
    ap.add_argument("--per-side", type=int, default=MAX_STRIKES_PER_SIDE,
                    help="現價上下各要幾個行使價")
    ap.add_argument("--no-book", action="store_true",
                    help="唔訂閱 order book（省配額，冇真 bid/ask）")
    ap.add_argument("--index-option", action="store_true",
                    help="標的係指數（HSI/HHI）")
    ap.add_argument("--stock", help="--spread-report 時只計某隻")
    a = ap.parse_args()

    if a.health:
        alive = opend_alive()
        print(f"OpenD {HOST}:{PORT} — {'生' if alive else '死'}")
        if alive:
            try:
                ctx = connect()
                ret, df = ctx.get_global_state()
                print(df if _ok(ret) else f"get_global_state 失敗: {df}")
                ctx.close()
            except Exception as e:
                print(f"連到 socket 但 API 唔通: {e}")
        return

    if a.spread_report:
        print(spread_report(a.stock))
        return

    if a.expiries:
        ctx = connect()
        try:
            for e in expiration_dates(ctx, a.expiries):
                print(f"{e['expiry']}  DTE {e['dte']:>4}")
        finally:
            ctx.close()
        return

    if a.chain:
        ctx = connect()
        try:
            df = chain(ctx, a.chain, a.min_dte, a.max_dte, a.index_option)
            if df.empty:
                print("冇合約")
                return
            cols = [c for c in ("code", "expiry", "dte", "strike_price",
                                "option_type", "name") if c in df.columns]
            print(df[cols].to_string(index=False))
            print(f"\n共 {len(df)} 張合約")
        finally:
            ctx.close()
        return

    owners = None
    if a.snapshot_underlyings:
        owners = _default_owners()
    elif a.snapshot is not None:
        owners = a.snapshot or _default_owners()

    if owners:
        log(f"標的: {', '.join(owners)}  DTE {a.min_dte}-{a.max_dte}")
        s = snapshot(owners, a.min_dte, a.max_dte, a.per_side,
                     want_book=not a.no_book, index_option=a.index_option)
        if s.get("status") == "skipped":
            print(f"⚠ 跳過: {s.get('error')}")
            return
        print(f"\n{s}")
        if s.get("rows") and not s.get("with_book"):
            print("\n⚠ 冇一行有真 bid/ask —— 檢查港股期權 LV2 行情權限，"
                  "或者現在係收市時段。")
        elif s.get("median_spread_pct"):
            print(f"\n中位價差 {s['median_spread_pct']}% → 跑 "
                  "`python3 futu_option_chain.py --spread-report` 睇分佈")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
