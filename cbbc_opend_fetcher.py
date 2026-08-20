# -*- coding: utf-8 -*-
"""
cbbc_opend_fetcher.py — 由 OpenD 直接攞牛熊證數據（取代 Manus Google Sheets scrape）

輸出同舊 scrape 檔（Desktop/db/CBBC/cbbc_*.parquet）完全同一個 schema：
  un (HSI 或無零股票碼), cprice (收回價字串), qu (街貨量),
  cp ('Bull'/'Bear'), issuer (發行商代碼)

策略：
  1. get_warrant(HSI + 135 期權標的) — BULL+BEAR 全分頁, status==NORMAL
  2. get_market_snapshot batch — 現價（07:30 跑 = 上一交易日收市價）
  3. request_history_kline — 14日 ATR（同 importer 共享 30 日窗口，唔扣新額度）

外國指數（SPX/DJI/NDX）futu get_warrant 唔支援，由 scrape 檔後備補。

⚠️ 一定要用 /usr/local/bin/python3（futu module 只裝喺呢個直譯器）
"""
import json
import os
import time

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SPECS_PATH = os.path.join(BASE_DIR, "options_data", "contract_specs.json")
OPEN_GAP = float(os.environ.get("CBBC_OPEND_GAP", "0.35"))   # get_warrant 每隻之間 sleep
KLINE_GAP = float(os.environ.get("CBBC_OPEND_KLINE_GAP", "0.30"))

HSI_CODE = "HK.800000"


def _underlyings():
    """[(futu_code, un_str)] — HSI + 135 期權標的。un 用無零格式（同 scrape 一致）。"""
    out = [(HSI_CODE, "HSI")]
    try:
        specs = json.load(open(SPECS_PATH, encoding="utf-8"))
        for code in specs:
            if isinstance(code, str) and code.strip().isdigit():
                out.append((f"HK.{code}", str(int(code))))
    except Exception as e:
        print(f"⚠️ 讀 contract_specs.json 失敗: {e}")
    return out


def _opend_ctx():
    from futu import OpenQuoteContext
    return OpenQuoteContext(host="127.0.0.1", port=11111)


def _hket_qu(r):
    """street_vol(份) ÷ (conversion_ratio×100) → hket 正規化 qu（百萬份 @10,000:1 基準）。"""
    try:
        sv = float(r.get("street_vol") or 0)
        cr = float(r.get("conversion_ratio") or 0)
        if sv <= 0 or cr <= 0:
            return 0.0
        return sv / (cr * 100)
    except (TypeError, ValueError):
        return 0.0


def fetch_universe(verbose=True):
    """HSI + 135 期權標的全部牛熊證 → scrape schema DataFrame。失敗回 None。"""
    from futu.quote.quote_get_warrant import Request
    from futu import RET_OK, SortField

    ctx = None
    frames = []
    try:
        ctx = _opend_ctx()
        for futu_code, un in _underlyings():
            try:
                rows = _warrant_pages(ctx, futu_code, Request, SortField)
            except Exception as e:
                msg = str(e)
                if "頻率" in msg or "rate" in msg.lower():
                    if verbose:
                        print(f"⏳ get_warrant 限頻，等 20s 重試 {un} …")
                    time.sleep(20)
                    rows = _warrant_pages(ctx, futu_code, Request, SortField)
                else:
                    if verbose:
                        print(f"⚠️ {un} get_warrant 失敗: {e}")
                    rows = []
            for r in rows:
                frames.append({
                    "un": un,
                    "cp": "Bull" if "BULL" in str(r.get("type", "")).upper() else "Bear",
                    "cprice": f"{r.get('recovery_price')}",
                    # hket qu = street_vol ÷ (conversion_ratio×100)，同舊 scrape 檔同 scale
                    # （strategy_lab 門檻 heavy_qu>150/20/10 就係用呢個 scale 校準）
                    "qu": _hket_qu(r),
                    "issuer": r.get("issuer"),
                })
            time.sleep(OPEN_GAP)
    except Exception as e:
        print(f"❌ OpenD 連線失敗: {e}")
        return None
    finally:
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass

    if not frames:
        return None
    df = pd.DataFrame(frames)
    df["cprice"] = df["cprice"].astype(str)
    for col in ("qu",):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    if verbose:
        print(f"✅ OpenD 牛熊證: {len(df)} 隻 / {df['un'].nunique()} 個標的")
    return df


def _warrant_pages(ctx, futu_code, Request, SortField):
    """get_warrant BULL+BEAR 全分頁（warrant_service.py 同款 unpack）。"""
    from futu import RET_OK, WrtType

    frames = []
    begin = 0
    while True:
        req = Request()
        req.begin = begin
        req.num = 200
        req.sort_field = SortField.CODE
        req.ascend = True
        req.type_list = [WrtType.BULL, WrtType.BEAR]
        ret, data = ctx.get_warrant(futu_code, req)
        if ret != RET_OK:
            raise RuntimeError(data)
        df, last_page, all_count = data
        if df is None or len(df) == 0:
            break
        frames.append(df)
        got = len(df)
        begin += got
        if last_page or (all_count is not None and begin >= all_count) or got < 200:
            break
    out = []
    for d in frames:
        for _, r in d.iterrows():
            if str(r.get("status", "")) != "NORMAL":
                continue
            rp = r.get("recovery_price")
            try:
                if rp is None or float(rp) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            out.append(r)
    return out


def fetch_spots(verbose=True):
    """{un: 現價} — HSI + 135 股 batch snapshot。失敗回 None。"""
    try:
        ctx = _opend_ctx()
    except Exception as e:
        print(f"❌ OpenD 連線失敗: {e}")
        return None
    try:
        pairs = _underlyings()
        spots = {}
        for i in range(0, len(pairs), 200):
            batch = [p[0] for p in pairs[i:i + 200]]
            ret, data = ctx.get_market_snapshot(batch)
            if ret != 0:
                raise RuntimeError(data)
            for _, r in data.iterrows():
                code = str(r.get("code", ""))
                px = r.get("last_price")
                try:
                    px = float(px)
                except (TypeError, ValueError):
                    continue
                if px and px > 0:
                    if code == HSI_CODE:
                        spots["HSI"] = px
                    elif code.startswith("HK."):
                        spots[str(int(code[3:]))] = px
        if verbose:
            print(f"✅ OpenD 現價: {len(spots)} 個標的")
        return spots or None
    except Exception as e:
        print(f"⚠️ snapshot 失敗: {e}")
        return None
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def fetch_atrs(uns, verbose=True):
    """{un: 14日 ATR} — request_history_kline（同 importer 共享額度窗口）。失敗回 {}。"""
    try:
        ctx = _opend_ctx()
    except Exception as e:
        print(f"❌ OpenD 連線失敗: {e}")
        return {}
    code_map = dict(_underlyings())
    atrs = {}
    try:
        for un in uns:
            futu_code = code_map.get(str(un))
            if not futu_code:
                continue
            try:
                ret, data = ctx.request_history_kline(
                    futu_code, start=None, end=None, max_count=25,
                    page_req_key=None, asc=False)
                if ret != 0:
                    continue
                d = data[0] if isinstance(data, tuple) else data
                if d is None or len(d) < 7:
                    continue
                tr = (d["high_price"].astype(float) - d["low_price"].astype(float))
                atr = tr.tail(14).mean()
                if pd.notna(atr):
                    atrs[str(un)] = round(float(atr), 2)
            except Exception as e:
                if "頻率" in str(e) or "rate" in str(e).lower():
                    time.sleep(15)
                else:
                    pass
            time.sleep(KLINE_GAP)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    if verbose:
        print(f"✅ OpenD ATR: {len(atrs)} 個標的")
    return atrs


def fetch_spot_atr_one(symbol):
    """單一標的 (spot, atr)：snapshot 現價 + kline 14日 ATR。失敗回 (None, None)。"""
    s = str(symbol).strip()
    if s == "HSI":
        code = HSI_CODE
    elif s.isdigit():
        code = f"HK.{int(s):05d}"
    else:
        return None, None
    ctx = None
    try:
        ctx = _opend_ctx()
        spot = None
        ret, data = ctx.get_market_snapshot([code])
        if ret == 0 and data is not None and len(data):
            px = data.iloc[0].get("last_price")
            try:
                px = float(px)
                if px > 0:
                    spot = px
            except (TypeError, ValueError):
                pass
        if spot is None:
            return None, None
        atr = None
        try:
            ret, data = ctx.request_history_kline(
                code, start=None, end=None, max_count=25,
                page_req_key=None, asc=False)
            if ret == 0:
                d = data[0] if isinstance(data, tuple) else data
                if d is not None and len(d) >= 7:
                    tr = d["high_price"].astype(float) - d["low_price"].astype(float)
                    a = tr.tail(14).mean()
                    if pd.notna(a):
                        atr = round(float(a), 2)
        except Exception:
            pass
        return spot, atr
    except Exception:
        return None, None
    finally:
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    df = fetch_universe()
    if df is not None:
        print(df.groupby("un").size().sort_values(ascending=False).head(8))
    spots = fetch_spots()
    print("HSI 現價:", spots and spots.get("HSI"))
