"""market_chains.py — 多市場期權鏈數據源（方向顧問用）。

統一輸出 context dict：
    ok / spot / name / date / atm_iv(%) / hv20(%) / size(每張乘數) /
    currency / min_oi / exps[{expiry,dte,oi,bucket}] / chain_fn(expiry)→DataFrame

DataFrame 欄位同 HKEX 鏈一致：strike / type(C|P) / settle / oi / volume / iv(%)
＋ futu_code（富途期權代碼）。

市場：
  hk_index  恒指／迷你恒指期權 — OpenD 即市（bid/ask 中價做 settle，
            IV 用 snapshot option_implied_volatility，缺就用中價反解）
  us_stock  美股個股期權 — Yahoo 鏈（Yahoo 自帶 IV 欄經常壞，
            一律用 bs.implied_vol 由中價反解）
"""

from __future__ import annotations

import math
from datetime import date, timedelta, timezone
from pathlib import Path

import pandas as pd

import bs

BASE = Path(__file__).parent
HSI_KLINE = Path("/home/workspace/Desktop/db/Futu/Kline/kline_index.parquet")
HKT = timezone(timedelta(hours=8))

EXP_BUCKETS = [(10, 35, "短線"), (35, 70, "中線"), (70, 130, "長線")]

MARKET_LABEL = {"hk_stock": "港股期權", "hk_index": "港股期指",
                "us_stock": "美股期權"}

MARKET_LABEL = {"hk_stock": "港股期權", "hk_index": "港股期指", "us_stock": "美股期權"}

INDEX_SPECS = {
    "HSI": {"underlying": "HK.800000", "name": "恒生指數", "mult": 50,
            "small": False, "prefix": "HSI", "mult_note": "$50/點"},
    "MHI": {"underlying": "HK.800000", "name": "迷你恒生指數", "mult": 10,
            "small": True, "prefix": "MHI", "mult_note": "$10/點"},
}

INDEX_INSTRUMENTS = INDEX_SPECS
INDEX_INSTRUMENTS = INDEX_SPECS

US_UNIVERSE = US_POPULAR = [
    {"code": "AAPL", "name": "Apple"}, {"code": "MSFT", "name": "Microsoft"},
    {"code": "NVDA", "name": "Nvidia"}, {"code": "GOOGL", "name": "Alphabet"},
    {"code": "AMZN", "name": "Amazon"}, {"code": "META", "name": "Meta"},
    {"code": "TSLA", "name": "Tesla"}, {"code": "NFLX", "name": "Netflix"},
    {"code": "AMD", "name": "AMD"}, {"code": "AVGO", "name": "Broadcom"},
    {"code": "MU", "name": "Micron"}, {"code": "QCOM", "name": "Qualcomm"},
    {"code": "INTC", "name": "Intel"}, {"code": "PLTR", "name": "Palantir"},
    {"code": "SMCI", "name": "Super Micro"}, {"code": "ARM", "name": "ARM"},
    {"code": "COIN", "name": "Coinbase"}, {"code": "UBER", "name": "Uber"},
    {"code": "DIS", "name": "Disney"}, {"code": "BA", "name": "Boeing"},
    {"code": "BABA", "name": "阿里巴巴"}, {"code": "PDD", "name": "拼多多"},
    {"code": "JD", "name": "京東"}, {"code": "BIDU", "name": "百度"},
    {"code": "NIO", "name": "蔚來"}, {"code": "LI", "name": "理想汽車"},
    {"code": "XPEV", "name": "小鵬汽車"},
]

US_UNIVERSE = US_POPULAR


def _fnum(v) -> float | None:
    try:
        f = float(v)
        return None if f != f else f
    except Exception:
        return None


# ---------------------------------------------------------------- 港股期指
def _od_quote_ctx():
    import futu
    return futu.OpenQuoteContext(host="127.0.0.1", port=11111)


def _hv20_hsi() -> float | None:
    try:
        df = pd.read_parquet(HSI_KLINE)
        c = df[df["code"] == "HK.800000"].sort_values("time_key")["close"]
        c = c.tail(21)
        if len(c) < 15:
            return None
        r = pd.np.log(c / c.shift(1)).dropna() if hasattr(pd, "np") else \
            pd.Series([math.log(a / b) for a, b in zip(c.iloc[1:], c.iloc[:-1])])
        return float(r.std() * math.sqrt(252) * 100)
    except Exception:
        return None


def hk_index_ctx(instrument: str = "HSI") -> dict:
    import futu
    spec = INDEX_SPECS.get(instrument.upper())
    if not spec:
        return {"ok": False, "error": f"唔識嘅指數期權 {instrument}（HSI／MHI）"}

    ctx = _od_quote_ctx()
    try:
        # 現價
        ret, snap = ctx.get_market_snapshot([spec["underlying"]])
        if ret != futu.RET_OK:
            return {"ok": False, "error": f"攞唔到恒指現價: {snap}"}
        spot = float(snap.iloc[0]["last_price"])

        # 到期日
        kw = {}
        if spec["small"]:
            kw["index_option_type"] = futu.IndexOptionType.SMALL
        ret, exp_df = ctx.get_option_expiration_date(spec["underlying"], **kw)
        if ret != futu.RET_OK or exp_df is None or exp_df.empty:
            return {"ok": False, "error": f"攞唔到期權到期日: {exp_df}"}
        today = pd.Timestamp(date.today())
        exps_raw = []
        for _, r in exp_df.iterrows():
            e = pd.to_datetime(r["strike_time"]).date()
            dte = (pd.Timestamp(e) - today).days
            cyc = str(r.get("expiration_cycle", "")).upper()
            exps_raw.append({"expiry": e, "dte": dte, "cycle": cyc})

        # 每桶揀一個月（優先 MONTH，冇就揀最接近桶中點）
        picked = []
        for lo, hi, label in EXP_BUCKETS:
            cand = [e for e in exps_raw if lo <= e["dte"] <= hi]
            if not cand:
                continue
            mid = (lo + hi) / 2
            monthly = [e for e in cand if e["cycle"] == "MONTH"]
            pool = monthly or cand
            e = min(pool, key=lambda x: abs(x["dte"] - mid))
            picked.append({"expiry": e["expiry"], "dte": e["dte"],
                           "oi": 0, "bucket": label, "cycle": e["cycle"]})
        if not picked:
            if exps_raw:
                e = min(exps_raw, key=lambda x: x["dte"])
                picked.append({"expiry": e["expiry"], "dte": e["dte"],
                               "oi": 0, "bucket": "最近", "cycle": e["cycle"]})
            else:
                return {"ok": False, "error": "冇可用到期月"}

        chain_cache: dict[date, pd.DataFrame] = {}

        def chain_fn(expiry, _ctx=ctx, _spec=spec):
            if expiry in chain_cache:
                return chain_cache[expiry]
            ymd = str(expiry)[:10]
            kw2 = dict(option_type=futu.OptionType.ALL,
                       option_cond_type=futu.OptionCondType.ALL)
            if _spec["small"]:
                kw2["index_option_type"] = futu.IndexOptionType.SMALL
            ret, chain = _ctx.get_option_chain(code=_spec["underlying"],
                                               start=ymd, end=ymd, **kw2)
            if ret != futu.RET_OK or chain is None or chain.empty:
                chain_cache[expiry] = pd.DataFrame()
                return chain_cache[expiry]
            codes = chain["code"].tolist()
            frames = []
            for i in range(0, len(codes), 400):
                ret2, s = _ctx.get_market_snapshot(codes[i:i + 400])
                if ret2 == futu.RET_OK and s is not None:
                    frames.append(s[["code", "last_price", "bid_price", "ask_price",
                                     "volume", "option_open_interest",
                                     "option_implied_volatility"]])
            if frames:
                chain = chain.merge(pd.concat(frames, ignore_index=True),
                                    on="code", how="left")
            else:
                chain_cache[expiry] = pd.DataFrame()
                return chain_cache[expiry]

            rows = []
            for _, r in chain.iterrows():
                bid, ask = _fnum(r.get("bid_price")), _fnum(r.get("ask_price"))
                last = _fnum(r.get("last_price"))
                if bid and ask and bid > 0 and ask > 0:
                    settle = (bid + ask) / 2
                elif last and last > 0:
                    settle = last
                else:
                    continue
                k = float(r["strike_price"])
                cp = "C" if str(r["option_type"]).upper().startswith("CALL") else "P"
                iv = _fnum(r.get("option_implied_volatility"))
                if not iv or iv <= 0:
                    t = bs.yearfrac(max((pd.Timestamp(expiry) -
                                         pd.Timestamp(date.today())).days, 1))
                    iv = bs.implied_vol(settle, spot, k, t, cp)
                    iv = iv * 100 if iv else None
                rows.append({"strike": k, "type": cp, "settle": settle,
                             "oi": int(r.get("option_open_interest") or 0),
                             "volume": int(r.get("volume") or 0),
                             "iv": iv, "futu_code": str(r["code"])})
            df = pd.DataFrame(rows)
            if not df.empty:
                df = df.sort_values(["strike", "type"]).reset_index(drop=True)
            chain_cache[expiry] = df
            return df

        # 全部桶揀嘅月都攞唔到鏈（例如最近月無報價）→ 逐個到期日試
        if all(chain_fn(e["expiry"]).empty for e in picked):
            cands = sorted([e for e in exps_raw if e["dte"] >= 3],
                           key=lambda x: x["dte"])
            for e in cands:
                if not chain_fn(e["expiry"]).empty:
                    picked = [{"expiry": e["expiry"], "dte": e["dte"], "oi": 0,
                               "bucket": "最近", "cycle": e["cycle"]}]
                    break
            else:
                return {"ok": False, "error": "期權鏈全部冇即市報價（可能週末／假期）"}

        # ATM IV：全鏈最近現價 6 張
        atm_iv = None
        first = chain_fn(picked[0]["expiry"])
        if not first.empty:
            first = first.assign(dist=(first.strike - spot).abs())
            near = first.nsmallest(6, "dist")
            ivs = [float(v) for v in near["iv"] if v and not pd.isna(v) and v > 0]
            if ivs:
                atm_iv = sum(ivs) / len(ivs)

        hv20 = _hv20_hsi()
        return {"ok": True, "market": "hk_index", "instrument": instrument.upper(),
                "spot": spot, "name": spec["name"], "date": date.today().isoformat(),
                "atm_iv": atm_iv or hv20 or 25.0, "hv20": hv20 or atm_iv or 25.0,
                "size": spec["mult"], "currency": "HKD", "min_oi": 10, "iv_rank": None,
                "note": "權金＝OpenD 即市 bid/ask 中價",
                "mult_note": f"${spec['mult']}／點",
                "codes_from_chain": True,
                "exps": picked, "chain_fn": chain_fn, "chain": chain_fn}
    finally:
        try:
            ctx.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 美股期權
def _us_hv20(hist: pd.DataFrame) -> float | None:
    try:
        c = hist["Close"].dropna().tail(21)
        if len(c) < 15:
            return None
        r = [math.log(a / b) for a, b in zip(c.iloc[1:], c.iloc[:-1])]
        sd = pd.Series(r).std()
        return float(sd * math.sqrt(252) * 100)
    except Exception:
        return None


def us_ctx(ticker: str) -> dict:
    import yfinance as yf

    ticker = ticker.strip().upper()
    if not ticker or len(ticker) > 8 or not ticker.isalnum():
        return {"ok": False, "error": f"美股代號無效：{ticker}"}

    try:
        t = yf.Ticker(ticker)
        try:
            spot = _fnum(t.fast_info["lastPrice"])
        except Exception:
            spot = None
        hist = t.history(period="2mo")
        if hist.empty:
            return {"ok": False, "error": f"{ticker} 攞唔到數據（檢查代號）"}
        if not spot:
            spot = float(hist["Close"].dropna().iloc[-1])

        name = ticker
        try:
            name = t.info.get("shortName") or ticker
        except Exception:
            pass

        exps_all = []
        today = date.today()
        for e in t.options:
            d = date.fromisoformat(e)
            dte = (d - today).days
            if dte >= 3:
                exps_all.append({"expiry": d, "dte": dte})

        picked = []
        for lo, hi, label in EXP_BUCKETS:
            cand = [e for e in exps_all if lo <= e["dte"] <= hi]
            if not cand:
                continue
            mid = (lo + hi) / 2
            e = min(cand, key=lambda x: abs(x["dte"] - mid))
            picked.append({"expiry": e["expiry"], "dte": e["dte"],
                           "oi": 0, "bucket": label})
        if not picked and exps_all:
            e = min(exps_all, key=lambda x: x["dte"])
            picked.append({"expiry": e["expiry"], "dte": e["dte"],
                           "oi": 0, "bucket": "最近"})
        if not picked:
            return {"ok": False, "error": f"{ticker} 冇可用期權到期月"}

        chain_cache: dict[date, pd.DataFrame] = {}

        def chain_fn(expiry, _t=t, _spot=spot, _ticker=ticker):
            if expiry in chain_cache:
                return chain_cache[expiry]
            dte = max((expiry - date.today()).days, 1)
            t_year = bs.yearfrac(dte)
            rows = []
            try:
                ch = _t.option_chain(expiry.isoformat())
            except Exception:
                chain_cache[expiry] = pd.DataFrame()
                return chain_cache[expiry]
            for cp, df in (("C", ch.calls), ("P", ch.puts)):
                for _, r in df.iterrows():
                    bid, ask = _fnum(r.get("bid")), _fnum(r.get("ask"))
                    last = _fnum(r.get("lastPrice"))
                    if bid and ask and ask > 0:
                        settle = (bid + ask) / 2
                    elif last and last > 0:
                        settle = last
                    else:
                        continue
                    k = float(r["strike"])
                    iv = bs.implied_vol(settle, _spot, k, t_year, cp)
                    iv_pct = round(iv * 100, 2) if iv else None
                    yymmdd = expiry.strftime("%y%m%d")
                    fc = f"US.{_ticker}{yymmdd}{cp}{int(round(k * 1000))}"
                    rows.append({"strike": k, "type": cp, "settle": settle,
                                 "oi": int(_fnum(r.get("openInterest")) or 0),
                                 "volume": int(_fnum(r.get("volume")) or 0),
                                 "iv": iv_pct, "futu_code": fc})
            out = pd.DataFrame(rows)
            if not out.empty:
                out = out.sort_values(["strike", "type"]).reset_index(drop=True)
            chain_cache[expiry] = out
            return out

        # ATM IV 用首選月兩邊貼價合約
        atm_iv = None
        first = chain_fn(picked[0]["expiry"])
        if not first.empty:
            first = first.assign(dist=(first.strike - spot).abs())
            near = first.nsmallest(4, "dist")
            ivs = [float(v) for v in near["iv"] if v and not pd.isna(v) and 1 < v < 300]
            if ivs:
                atm_iv = sum(ivs) / len(ivs)

        hv20 = _us_hv20(hist)
        return {"ok": True, "market": "us_stock", "instrument": ticker,
                "spot": spot, "name": name, "date": today.isoformat(),
                "atm_iv": atm_iv or hv20 or 30.0, "hv20": hv20 or atm_iv or 30.0,
                "size": 100, "currency": "USD", "min_oi": 0, "iv_rank": None,
                "note": "權金＝Yahoo bid/ask 中價（收市口徑）、IV 由中價反解",
                "codes_from_chain": True,
                "exps": picked, "chain_fn": chain_fn, "chain": chain_fn}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{ticker} 數據源錯誤: {e}"}


def stocks_for_market(market: str) -> list[dict]:
    if market == "hk_index":
        return [{"code": "HSI", "name": "恒生指數（$50/點）"},
                {"code": "MHI", "name": "迷你恒生指數（$10/點）"}]
    if market == "us_stock":
        return US_POPULAR
    return []
