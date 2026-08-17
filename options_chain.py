"""options_chain.py — 由 HKEX 每日報告拆出逐個行使價嘅期權鏈。

`options_scraper.py` 只留 class summary（每隻股票一行 ATM IV）。
呢個模組讀返同一份 raw 報告，拆出每條 contract：
到期日 / 行使價 / C or P / 結算價 / 該行使價自己嘅 IV% / 成交 / 未平倉。

有咗逐個行使價嘅結算價，就可以真正計策略成本、盈虧平衡、最大蝕。

CLI:
    python3 options_chain.py 00700                  # 最近交易日全鏈
    python3 options_chain.py 00700 --expiry 28AUG26
    python3 options_chain.py 00700 --date 2026-08-06
"""

from __future__ import annotations

import argparse
import gzip
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

BASE = Path(__file__).parent
RAW_DIR = BASE / "options_data" / "raw"
HISTORY = BASE / "options_data" / "iv_history.parquet"

CLASS_HDR = re.compile(
    r"^CLASS\s+([A-Z0-9]{3})\s+-\s+(.+?)\s+CLOSING PRICE HK\$\s*([\d,.]+)\s*$"
)
CONTRACT = re.compile(
    r"^(\d{2}[A-Z]{3}\d{2})\s+"          # 到期
    r"([\d,]+\.\d{2})\s+([CP])\s+"        # 行使價 + C/P
    r"([\d,]+\.\d{2})\s+"                 # opening
    r"([\d,]+\.\d{2})\s+"                 # high
    r"([\d,]+\.\d{2})\s+"                 # low
    r"([\d,]+\.\d{2})\s+"                 # settlement
    r"([+\-][\d,]+\.\d{2}|[\d,]+\.\d{2})\s+"  # change in settle
    r"(\d+|N/A)\s+"                       # IV%
    r"([\d,]+)\s+([\d,]+)\s+([+\-][\d,]+|[\d,]+)\s*$"  # vol / OI / ΔOI
)
DATE_RE = re.compile(r"STOCK OPTIONS DAILY MARKET REPORT AS AT\s+(\d{2} \w{3} \d{4})")


def _n(s: str) -> float | None:
    s = str(s).replace(",", "").replace("+", "").strip()
    if not s or s in {"N/A", "-"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _hkats_map() -> dict[str, str]:
    """HKATS 3-letter code → 5 位股票代號（由 iv_history 反查）。"""
    if not HISTORY.exists():
        return {}
    df = pd.read_parquet(HISTORY, columns=["hkats", "stock_code"]).drop_duplicates()
    return dict(zip(df.hkats, df.stock_code))


def latest_raw() -> date | None:
    files = sorted(RAW_DIR.glob("dqe*.txt.gz"))
    if not files:
        return None
    stem = files[-1].name[3:9]
    return datetime.strptime(stem, "%y%m%d").date()


def _read_raw(d: date) -> str | None:
    f = RAW_DIR / f"dqe{d:%y%m%d}.txt.gz"
    if not f.exists():
        return None
    with gzip.open(f, "rt", encoding="utf-8", errors="ignore") as fh:
        return fh.read()


def parse_chains(d: date | None = None, stock_code: str | None = None) -> pd.DataFrame:
    """拆出指定日期嘅期權鏈。stock_code 為 None 就全部股票（大 DataFrame）。"""
    d = d or latest_raw()
    if d is None:
        return pd.DataFrame()
    txt = _read_raw(d)
    if not txt:
        return pd.DataFrame()

    m = DATE_RE.search(txt)
    rpt = datetime.strptime(m.group(1), "%d %b %Y").date() if m else d
    codes = _hkats_map()
    want = stock_code.zfill(5) if stock_code else None

    rows: list[dict] = []
    hkats = name = None
    close = None
    keep = True

    for line in txt.splitlines():
        line = line.rstrip()
        if not line:
            continue
        h = CLASS_HDR.match(line.strip())
        if h:
            hkats, name, close = h.group(1), h.group(2).strip(), _n(h.group(3))
            code = codes.get(hkats)
            keep = (want is None) or (code == want)
            continue
        if not keep or hkats is None:
            continue
        c = CONTRACT.match(line.strip())
        if not c:
            continue
        (exp, strike, cp, op, hi, lo, settle, chg, iv, vol, oi, doi) = c.groups()
        rows.append(
            {
                "date": rpt,
                "hkats": hkats,
                "stock_code": codes.get(hkats),
                "name": name,
                "close": close,
                "expiry": datetime.strptime(exp, "%d%b%y").date(),
                "strike": _n(strike),
                "type": cp,
                "settle": _n(settle),
                "settle_chg": _n(chg),
                "iv": _n(iv),
                "volume": _n(vol),
                "oi": _n(oi),
                "oi_chg": _n(doi),
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["dte"] = df.apply(lambda r: (r["expiry"] - r["date"]).days, axis=1)
    df["moneyness"] = df.strike / df.close
    return df


def chain(stock_code: str, as_of: date | None = None, expiry: date | None = None) -> pd.DataFrame:
    """單一股票期權鏈；expiry 為 None 就全部到期月。"""
    df = parse_chains(as_of, stock_code)
    if df.empty:
        return df
    if expiry is not None:
        df = df[df.expiry == expiry]
    return df.sort_values(["expiry", "strike", "type"]).reset_index(drop=True)


def expiries(stock_code: str, as_of: date | None = None) -> list[dict]:
    """該股票所有到期日 + 剩餘日數 + 該月成交／未平倉。"""
    df = parse_chains(as_of, stock_code)
    if df.empty:
        return []
    g = df.groupby(["expiry", "dte"]).agg(volume=("volume", "sum"), oi=("oi", "sum")).reset_index()
    return g.sort_values("expiry").to_dict("records")


def pick_expiry(stock_code: str, min_dte: int = 20, max_dte: int = 70,
                as_of: date | None = None) -> date | None:
    """揀最合適嘅到期月：DTE 落喺範圍內、未平倉最多（最流通）。"""
    exps = expiries(stock_code, as_of)
    cand = [e for e in exps if min_dte <= e["dte"] <= max_dte]
    if not cand:
        cand = [e for e in exps if e["dte"] >= 7]
    if not cand:
        return None
    return max(cand, key=lambda e: (e["oi"] or 0))["expiry"]


def nearest(df: pd.DataFrame, target: float, cp: str, min_oi: float = 0) -> pd.Series | None:
    """喺鏈度搵最接近 target 行使價嘅 C 或 P（可要求最低未平倉）。"""
    sub = df[(df.type == cp)]
    if min_oi:
        liq = sub[sub.oi >= min_oi]
        if not liq.empty:
            sub = liq
    if sub.empty:
        return None
    return sub.iloc[(sub.strike - target).abs().argsort().iloc[0]]


def _cell(row: dict | None, key: str, width: int, kind: str = "f2") -> str:
    v = None if row is None else row.get(key)
    if v is None or pd.isna(v):
        return "—".rjust(width)
    if kind == "f2":
        return f"{v:>{width}.2f}"
    if kind == "i0":
        return f"{v:>{width}.0f}"
    return f"{int(v):>{width},}"


def _fmt(df: pd.DataFrame) -> str:
    out = []
    for exp, g in df.groupby("expiry"):
        dte = g.dte.iloc[0]
        out.append(f"\n── 到期 {exp}  (DTE {dte})  成交 {int(g.volume.sum()):,}  未平倉 {int(g.oi.sum()):,}")
        out.append(f"{'行使價':>8} {'Call結算':>9} {'C IV':>5} {'C成交':>7} {'C未平':>8}   "
                   f"{'Put結算':>9} {'P IV':>5} {'P成交':>7} {'P未平':>8}")
        calls = {r["strike"]: r for r in g[g.type == "C"].to_dict("records")}
        puts = {r["strike"]: r for r in g[g.type == "P"].to_dict("records")}
        for k in sorted(set(calls) | set(puts)):
            c, p = calls.get(k), puts.get(k)
            act = sum((r.get("volume") or 0) + (r.get("oi") or 0) for r in (c, p) if r)
            if act == 0:
                continue
            out.append(
                f"{k:>8.2f} "
                f"{_cell(c, 'settle', 9)} {_cell(c, 'iv', 5, 'i0')} "
                f"{_cell(c, 'volume', 7, 'int')} {_cell(c, 'oi', 8, 'int')}   "
                f"{_cell(p, 'settle', 9)} {_cell(p, 'iv', 5, 'i0')} "
                f"{_cell(p, 'volume', 7, 'int')} {_cell(p, 'oi', 8, 'int')}"
            )
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="HKEX 期權鏈（逐個行使價）")
    ap.add_argument("stock", help="股票代號，例如 00700")
    ap.add_argument("--date", help="報告日 YYYY-MM-DD（預設最近有 raw 嘅日）")
    ap.add_argument("--expiry", help="到期日 28AUG26 或 2026-08-28")
    ap.add_argument("--expiries", action="store_true", help="只列所有到期月")
    a = ap.parse_args()

    as_of = date.fromisoformat(a.date) if a.date else None
    code = a.stock.zfill(5)

    if a.expiries:
        for e in expiries(code, as_of):
            print(f"{e['expiry']}  DTE {e['dte']:>4}  成交 {int(e['volume']):>9,}  未平倉 {int(e['oi']):>10,}")
        return

    exp = None
    if a.expiry:
        exp = (date.fromisoformat(a.expiry) if "-" in a.expiry
               else datetime.strptime(a.expiry, "%d%b%y").date())

    df = chain(code, as_of, exp)
    if df.empty:
        print(f"{code} 冇期權（或該日冇報告）")
        return
    r = df.iloc[0]
    print(f"{code} {r['name']}  HKATS {r['hkats']}  收市 {r['close']}  報告日 {r['date']}")
    print(_fmt(df))


if __name__ == "__main__":
    main()
