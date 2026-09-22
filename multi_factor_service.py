"""multi_factor_service.py — 多因子 40 燈交叉驗證服務（port 8896）。

照《多因子量化交叉驗證交易策略手冊》：4 模組 × 10 燈，
模組①板塊與熱度（日線口徑）→ ②趨勢確立 → ③動能續航 → ④支撐與入場（跟 strategy 時間框架）。

端點：
  GET /health
  GET /score?code=700&strategy=short|mid|long
       → 逐盞燈明細 + 總分 + 評級 + 一票否決 + SL/TP/HL/ATR + 圖表 overlay levels

評級：34-40 A+ 100%倉；28-33 B+ 50%倉；20-27 觀望；<20 放棄。
一票否決：任何模組 <6 燈禁止下單。
過關：模組 ≥8 燈 → Title 金色（前端渲染）。
燈口徑：manual 燈（熱門敘事／板塊未覆蓋）由用戶 toggle；其餘自動。
數據源：OpenD（127.0.0.1:11111）。用 /usr/local/bin/python3 跑。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

PORT = 8896
OPEND_HOST = os.environ.get("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.environ.get("OPEND_PORT", "11111"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONST_FILE = "/home/workspace/futu-warrant-api/hsi_constituents.json"
SPEC_FILE = os.path.join(BASE_DIR, "options_data", "contract_specs.json")
HSI_CONST_URL = "https://www.hsi.com.hk/data/schi/rt/index-series/hsi/constituents.do"

HKT = timezone(timedelta(hours=8))

SCORE_CACHE: dict[tuple, tuple[float, dict]] = {}
SCORE_TTL = 600.0
FLOW_CACHE: dict[str, tuple[float, float]] = {}   # code -> (ts, main_net)
FLOW_TTL = 600.0
SECTOR_CACHE: dict[str, tuple[float, dict]] = {}
SECTOR_TTL = 600.0
UNIVERSE_LOCK = threading.Lock()
_ctx = None
_ctx_lock = threading.Lock()

STRATEGY_TF = {
    "short": {"ktype": "K_15M", "label": "短期（日內）", "span_days": 40,
              "trio": {"context": "1h", "exec": "15m", "trigger": "5m"}},
    "mid":   {"ktype": "K_60M", "label": "中期（1日至1星期）", "span_days": 120,
              "trio": {"context": "4h", "exec": "1h", "trigger": "15m"}},
    "long":  {"ktype": "K_DAY", "label": "長期（1星期以上）", "span_days": 500,
              "trio": {"context": "1d", "exec": "4h", "trigger": "1h"}},
}

MODULE_TITLES = ["板塊與熱度", "趨勢確立", "動能續航", "支撐與入場"]
FUNNEL_ORDER = [3, 0, 1, 2]   # 步驟4→步驟1→步驟2→步驟3


def quote_ctx():
    global _ctx
    with _ctx_lock:
        if _ctx is None:
            from futu import OpenQuoteContext
            _ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
        return _ctx


def reset_ctx():
    global _ctx
    with _ctx_lock:
        try:
            if _ctx is not None:
                _ctx.close()
        except Exception:
            pass
        _ctx = None


def _f(x, default=np.nan) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v


# ---------------------------------------------------------------- 指標

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ru = up.ewm(alpha=1 / n, adjust=False).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False).mean()
    rs = ru / rd.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, 12) - ema(close, 26)
    sig = ema(line, 9)
    return line, sig, line - sig


def stoch(df: pd.DataFrame, n: int = 14, d: int = 3) -> tuple[pd.Series, pd.Series]:
    ll = df["low"].rolling(n).min()
    hh = df["high"].rolling(n).max()
    rng = (hh - ll).replace(0, np.nan)
    k = ((df["close"] - ll) / rng * 100).fillna(50)
    return k, k.rolling(d).mean()


def cci(df: pd.DataFrame, n: int = 20) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    m = tp.rolling(n).mean()
    dev = tp.rolling(n).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - m) / (0.015 * dev.replace(0, np.nan))


def roc(close: pd.Series, n: int = 12) -> pd.Series:
    prev = close.shift(n)
    return ((close - prev) / prev.replace(0, np.nan) * 100).fillna(0)


def awesome(df: pd.DataFrame) -> pd.Series:
    hl2 = (df["high"] + df["low"]) / 2
    return sma(hl2, 5) - sma(hl2, 34)


def cmo(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).rolling(n).sum()
    dn = (-d.clip(upper=0)).rolling(n).sum()
    tot = (up + dn).replace(0, np.nan)
    return ((up - dn) / tot * 100).fillna(0)


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_c = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_c).abs(),
                    (df["low"] - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx_dmi(df: pd.DataFrame, n: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr(df, n)
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / a.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / a.replace(0, np.nan)
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100)
    adx = dx.ewm(alpha=1 / n, adjust=False).mean()
    return adx, plus_di, minus_di


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.Series:
    a = atr(df, n)
    mid = (df["high"] + df["low"]) / 2
    ub = mid + mult * a
    lb = mid - mult * a
    st = pd.Series(index=df.index, dtype=float)
    direction = 1
    fub = ub.iloc[0]
    flb = lb.iloc[0]
    st.iloc[0] = flb
    for i in range(1, len(df)):
        c = df["close"].iloc[i]
        pc = df["close"].iloc[i - 1]
        cub = ub.iloc[i]
        clb = lb.iloc[i]
        fub = cub if (cub < fub or pc > fub) else fub
        flb = clb if (clb > flb or pc < flb) else flb
        if c > fub:
            direction = 1
        elif c < flb:
            direction = -1
        st.iloc[i] = flb if direction == 1 else fub
    return st


def psar(df: pd.DataFrame, af0: float = 0.02, max_af: float = 0.2) -> pd.Series:
    high = df["high"].values
    low = df["low"].values
    n = len(df)
    out = np.zeros(n)
    bull = True
    af = af0
    ep = high[0]
    sar = low[0]
    for i in range(2, n):
        sar = sar + af * (ep - sar)
        if bull:
            sar = min(sar, low[i - 1], low[i - 2])
            if low[i] < sar:
                bull = False
                sar = ep
                ep = low[i]
                af = af0
            elif high[i] > ep:
                ep = high[i]
                af = min(af + af0, max_af)
        else:
            sar = max(sar, high[i - 1], high[i - 2])
            if high[i] > sar:
                bull = True
                sar = ep
                ep = high[i]
                af = af0
            elif low[i] < ep:
                ep = low[i]
                af = min(af + af0, max_af)
        out[i] = sar
    out[1] = out[2]
    return pd.Series(out, index=df.index)


def donchian_mid(df: pd.DataFrame, n: int = 20) -> pd.Series:
    return (df["high"].rolling(n).max() + df["low"].rolling(n).min()) / 2


def ichimoku_cloud(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    tenkan = (df["high"].rolling(9).max() + df["low"].rolling(9).min()) / 2
    kijun = (df["high"].rolling(26).max() + df["low"].rolling(26).min()) / 2
    span_a = ((tenkan + kijun) / 2).shift(26)
    span_b = ((df["high"].rolling(52).max() + df["low"].rolling(52).min()) / 2).shift(26)
    return span_a, span_b


def bollinger(close: pd.Series, n: int = 20) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return mid, mid + 2 * sd, mid - 2 * sd


def keltner_mid(df: pd.DataFrame, n: int = 20) -> pd.Series:
    return ema((df["high"] + df["low"] + df["close"]) / 3, n)


def swing_points(df: pd.DataFrame, order: int = 2) -> tuple[list[int], list[int]]:
    """fractal swing 高／低點 index 清單（新到舊）。"""
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    sh: list[int] = []
    sl: list[int] = []
    for i in range(n - order - 1, order - 1, -1):
        win_h = highs[i - order:i + order + 1]
        win_l = lows[i - order:i + order + 1]
        if highs[i] >= win_h.max():
            sh.append(i)
        if lows[i] <= win_l.min():
            sl.append(i)
    return sh, sl


# ---------------------------------------------------------------- 板塊／資金

_const_lock = threading.Lock()


def constituents() -> list[dict]:
    with _const_lock:
        try:
            st = os.stat(CONST_FILE)
            if time.time() - st.st_mtime < 24 * 3600:
                with open(CONST_FILE) as f:
                    items = json.load(f).get("items") or []
                if items:
                    return items
        except Exception:
            pass
        import urllib.request
        try:
            raw = urllib.request.urlopen(HSI_CONST_URL, timeout=30).read().decode("utf-8")
            d = json.loads(raw)
            items = []
            for sub in d["indexSeriesList"][0]["indexList"][0]["subIndexList"]:
                cat = (str(sub["indexName"]).replace("恒生", "")
                       .replace("分類指數", "").replace("分类指数", ""))
                cat = cat.replace("公用事业", "公用事業").replace("地产", "地產").replace("工商业", "工商業")
                for c in sub.get("constituentContent", []):
                    items.append({"code": str(c["code"]), "name": c.get("constituentName", ""), "cat": cat})
            if items:
                os.makedirs(os.path.dirname(CONST_FILE), exist_ok=True)
                with open(CONST_FILE, "w") as f:
                    json.dump({"fetched": time.time(), "items": items}, f, ensure_ascii=False)
                return items
        except Exception:
            try:
                with open(CONST_FILE) as f:
                    return json.load(f)["items"]
            except Exception:
                return []
    return []


def universe_codes() -> list[str]:
    codes = set()
    for it in constituents():
        codes.add(it["code"])
    try:
        specs = json.load(open(SPEC_FILE))
        for k in specs:
            codes.add(k)
    except Exception:
        pass
    return sorted(codes)


def sector_of(code: str) -> str | None:
    bare = code.lstrip("0")
    for it in constituents():
        if it["code"].lstrip("0") == bare:
            return it["cat"]
    return None


def main_net_flow(code: str) -> float:
    """今日主力淨流入（HKD），10 分鐘 cache。"""
    hit = FLOW_CACHE.get(code)
    if hit and time.time() - hit[0] < FLOW_TTL:
        return hit[1]
    try:
        from futu import RET_OK
        ret, df = quote_ctx().get_capital_distribution(code)
        if ret == RET_OK and len(df) > 0:
            r = df.iloc[-1]
            big_in = _f(r.get("capital_in_big"), 0.0) + _f(r.get("capital_in_super"), 0.0)
            big_out = _f(r.get("capital_out_big"), 0.0) + _f(r.get("capital_out_super"), 0.0)
            net = big_in - big_out
        else:
            net = 0.0
    except Exception:
        net = 0.0
    finally:
        time.sleep(0.1)  # 富途分單限頻 pacing
    FLOW_CACHE[code] = (time.time(), net)
    return net


def sector_snapshot(cat: str) -> dict:
    """同板塊成員今日平均升跌 % + 主力淨流總和 + 頭三大龍頭升跌。"""
    hit = SECTOR_CACHE.get(cat)
    if hit and time.time() - hit[0] < SECTOR_TTL:
        return hit[1]
    members = [it["code"] for it in constituents() if it["cat"] == cat]
    if not members:
        return {"ok": False}
    futu_codes = [f"HK.{c.zfill(5)}" for c in members]
    snap = snapshot(futu_codes)
    rows = []
    total_flow = 0.0
    for c, r in zip(members, snap):
        if r is None:
            continue
        last = _f(r.get("last_price"))
        prev = _f(r.get("prev_close_price"))
        pct = (last / prev - 1) * 100 if last and prev else 0.0
        rows.append({"code": c, "turnover": _f(r.get("turnover"), 0.0), "pct": pct})
    rows.sort(key=lambda x: x["turnover"], reverse=True)
    leaders = rows[:3]
    for r in rows:
        total_flow += main_net_flow(r["code"])
    avg_pct = float(np.mean([r["pct"] for r in rows])) if rows else 0.0
    res = {"ok": True, "members": len(rows), "avg_pct": round(avg_pct, 2),
           "flow": round(total_flow, 0), "leaders": leaders}
    SECTOR_CACHE[cat] = (time.time(), res)
    return res


def snapshot(futu_codes: list[str]) -> list[dict | None]:
    from futu import RET_OK
    out: list[dict | None] = [None] * len(futu_codes)
    batch = 200
    for i in range(0, len(futu_codes), batch):
        chunk = futu_codes[i:i + batch]
        idx = list(range(i, min(i + batch, len(futu_codes))))
        for attempt in range(3):
            try:
                ret, df = quote_ctx().get_market_snapshot(chunk)
            except Exception:
                ret, df = -1, None
            if ret == RET_OK:
                cmap = {str(r["code"]): r.to_dict() for _, r in df.iterrows()}
                for j, fc in zip(idx, chunk):
                    out[j] = cmap.get(fc)
                break
            time.sleep(2 + 2 * attempt)
    return out


# ---------------------------------------------------------------- K 線

def fetch_kline(code: str, ktype: str, span_days: int) -> pd.DataFrame:
    from futu import KLType, RET_OK
    kt = getattr(KLType, ktype)
    start = (datetime.now(HKT) - timedelta(days=span_days)).strftime("%Y-%m-%d")
    end = (datetime.now(HKT) + timedelta(days=1)).strftime("%Y-%m-%d")
    frames = []
    cur_end = end
    for _ in range(12):
        try:
            _out = quote_ctx().request_history_kline(code, start=start, end=cur_end,
                                                     ktype=kt, autype="qfq", max_count=1000)
            ret, df = _out[0], _out[1]
        except Exception:
            ret, df = -1, None
        if ret != RET_OK or df is None or df.empty:
            break
        frames.append(df)
        first = pd.to_datetime(df["time_key"].iloc[0])
        if first <= pd.Timestamp(start):
            break
        cur_end = first.strftime("%Y-%m-%d")
        time.sleep(0.15)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames).drop_duplicates(subset="time_key").sort_values("time_key").reset_index(drop=True)
    for col in ("open", "close", "high", "low", "volume", "turnover"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------- 燈

def light(name: str, short: str, passed: bool | None, value: str, threshold: str,
          reason: str, manual: bool = False) -> dict:
    passed = None if passed is None else bool(passed)
    return {"name": name, "short": short, "manual": manual,
            "auto": passed, "passed": passed,
            "value": value, "threshold": threshold, "reason": reason}


def _fmt(v, nd=2) -> str:
    return f"{v:.{nd}f}"


def module1(code: str, futu_code: str, snap: dict | None, daily: pd.DataFrame,
            hsi_daily: pd.DataFrame, sector: str | None) -> list[dict]:
    lights = []
    c = daily["close"].iloc[-1]
    c20 = daily["close"].iloc[-21] if len(daily) > 20 else daily["close"].iloc[0]
    stock20 = (c / c20 - 1) * 100
    h = hsi_daily["close"].iloc[-1]
    h20 = hsi_daily["close"].iloc[-21] if len(hsi_daily) > 20 else hsi_daily["close"].iloc[0]
    hsi20 = (h / h20 - 1) * 100

    lights.append(light("RS 相對強度", "RS 強度", stock20 > hsi20,
                        f"20日 {stock20:+.2f}%", f"> 大盤 {hsi20:+.2f}%",
                        f"標的 20 日漲 {stock20:+.2f}%，恒指 {hsi20:+.2f}%，{'跑贏' if stock20 > hsi20 else '跑輸'}大盤"))
    if len(daily) > 20 and len(hsi_daily) > 20:
        m = min(len(daily), len(hsi_daily))
        ratio = (daily["close"].tail(m).values / hsi_daily["close"].tail(m).values)
        is_high = ratio[-1] >= ratio[-20:].max() * 0.999
        lights.append(light("RS Line 創高", "RS 創高", bool(is_high),
                            f"比值 {ratio[-1]:.4f}", "創 20 日新高",
                            f"RS 線比值 {ratio[-1]:.4f}，20 日最高 {ratio[-20:].max():.4f}"))
    else:
        lights.append(light("RS Line 創高", "RS 創高", False, "數據不足", "20 日比值新高", "K 線數據不足 20 日"))

    if sector:
        ss = sector_snapshot(sector)
        if ss.get("ok"):
            flow_pos = ss["flow"] > 0
            candle_up = ss["avg_pct"] > 0
            lights.append(light("板塊資金流向", "板塊流入", flow_pos and candle_up,
                                f"板塊 {sector} 均值 {ss['avg_pct']:+.2f}%／主力淨流 {ss['flow']:+,.0f}",
                                "陽線＋淨流入",
                                f"板塊 {sector} {ss['members']} 隻成員平均 {ss['avg_pct']:+.2f}%，主力淨流 {ss['flow']:+,.0f} HKD"))
            up_leaders = sum(1 for l in ss["leaders"] if l["pct"] > 0)
            names = "／".join(l["code"] for l in ss["leaders"])
            lights.append(light("板塊龍頭同步", "龍頭同步", up_leaders >= 2,
                                f"頭3龍頭 {up_leaders}/3 升", "≥2 隻升",
                                f"板塊頭三龍頭（{names}）有 {up_leaders} 隻上升"))
        else:
            lights.append(light("板塊資金流向", "板塊流入", None, "—", "陽線＋淨流入",
                                "板塊數據暫時攞唔到", manual=True))
            lights.append(light("板塊龍頭同步", "龍頭同步", None, "—", "≥2 隻升",
                                "板塊數據暫時攞唔到", manual=True))
    else:
        lights.append(light("板塊資金流向", "板塊流入", None, "—", "陽線＋淨流入",
                            "非恒指成份股，板塊分類未覆蓋", manual=True))
        lights.append(light("板塊龍頭同步", "龍頭同步", None, "—", "≥2 隻升",
                            "非恒指成份股，板塊分類未覆蓋", manual=True))

    uni = universe_codes()
    uni.remove(code) if code in uni else None
    uni.insert(0, code)
    snaps = snapshot([f"HK.{c.zfill(5)}" for c in uni])
    turnover = _f(snap.get("turnover"), 0.0) if snap else 0.0
    ranks = []
    for cc, r in zip(uni, snaps):
        if r is not None:
            ranks.append((_f(r.get("turnover"), 0.0), cc))
    ranks.sort(reverse=True)
    rank = next((i + 1 for i, (t, cc) in enumerate(ranks) if cc == code), len(ranks))
    cut = max(1, int(len(ranks) * 0.15))
    lights.append(light("成交額市場排名", "成交排名", rank <= cut,
                        f"#{rank}／{len(ranks)}（活躍 universe）", f"前 15%（≤#{cut}）",
                        f"今日成交 {turnover:,.0f} HKD，喺主活躍 universe 排 #{rank}，前 15% 門檻係 #{cut}"))

    hsi_last = _f(snap.get("last_price")) if snap else 0
    beta = np.nan
    if len(daily) > 60 and len(hsi_daily) > 60:
        m = min(len(daily), len(hsi_daily), 252)
        sr = daily["close"].tail(m).pct_change().dropna()
        hr = hsi_daily["close"].tail(m).pct_change().dropna()
        n = min(len(sr), len(hr))
        if n >= 60:
            sr = sr.tail(n).values
            hr = hr.tail(n).values
            var = float(np.var(hr))
            beta = float(np.cov(sr, hr)[0][1] / var) if var > 0 else np.nan
    if np.isnan(beta):
        lights.append(light("Beta 波動彈性", "Beta", False, "數據不足", "> 1.1", "歷史數據不足計 Beta"))
    else:
        lights.append(light("Beta 波動彈性", "Beta", beta > 1.1, f"{beta:.2f}", "> 1.1",
                            f"252 日 Beta = {beta:.2f}，{'彈性大過' if beta > 1.1 else '唔夠'} 1.1"))

    if len(daily) > 10:
        w = daily.tail(252)
        hi52 = w["high"].max()
        dist = (hi52 / c - 1) * 100 if c else np.nan
        lights.append(light("52週高點距離", "距52週高", (not np.isnan(dist)) and dist <= 15,
                            f"距離 {dist:.1f}%", "≤ 15%",
                            f"52 週最高 {hi52:.2f}，現價 {c:.2f}，距離 {dist:.1f}%"))
    else:
        lights.append(light("52週高點距離", "距52週高", False, "數據不足", "≤ 15%", "日線數據不足"))

    if snap is not None:
        op = _f(snap.get("open_price"), 0.0)
        prev = _f(snap.get("prev_close_price"), 0.0)
        if op > 0 and prev > 0:
            gap = (op / prev - 1) * 100
            lights.append(light("開盤跳空強勢", "跳空", op > prev, f"開市 {gap:+.2f}%", "開盤 > 昨收",
                                f"今日開 {op:.2f} vs 昨收 {prev:.2f}，{'高開' if op > prev else '冇高開'} {gap:+.2f}%"))
        else:
            lights.append(light("開盤跳空強勢", "跳空", None, "未開市", "開盤 > 昨收",
                                "今日未開市，冇跳空數據", manual=True))
    else:
        lights.append(light("開盤跳空強勢", "跳空", False, "—", "開盤 > 昨收", "快照數據失敗"))

    lights.append(light("熱門敘事認證", "敘事", None, "—", "屬三大主線題材",
                        "無法自動判斷，請手動認證（例如 AI／數據中心／高股息）", manual=True))

    try:
        from futu import RET_OK
        ret, dist_df = quote_ctx().get_capital_distribution(futu_code)
        if ret == RET_OK and len(dist_df) > 0:
            r = dist_df.iloc[-1]
            big = sum(_f(r.get(k), 0.0) for k in ("capital_in_big", "capital_in_super",
                                                  "capital_out_big", "capital_out_super"))
            tot = sum(_f(r.get(k), 0.0) for k in ("capital_in_big", "capital_in_super",
                                                  "capital_in_mid", "capital_in_small",
                                                  "capital_out_big", "capital_out_super",
                                                  "capital_out_mid", "capital_out_small"))
            ratio = big / tot * 100 if tot > 0 else np.nan
            if np.isnan(ratio):
                lights.append(light("機構大單比率", "大單", False, "無數據", "> 30%", "今日暫時冇分單數據"))
            else:
                lights.append(light("機構大單比率", "大單", ratio > 30, f"{ratio:.1f}%", "> 30%",
                                    f"大單＋特大單佔今日成交 {ratio:.1f}%"))
        else:
            lights.append(light("機構大單比率", "大單", False, "無數據", "> 30%", "分單數據攞唔到"))
    except Exception:
        lights.append(light("機構大單比率", "大單", False, "無數據", "> 30%", "分單數據攞唔到"))

    return lights


def module234(daily: pd.DataFrame, tf: pd.DataFrame, vwap_daily: float, spot: float) -> tuple[list[dict], list[dict], list[dict], dict]:
    lights2: list[dict] = []
    lights3: list[dict] = []
    lights4: list[dict] = []
    levels: dict = {}

    c = tf["close"].iloc[-1]
    e20 = ema(tf["close"], 20).iloc[-1]
    e50 = ema(tf["close"], 50).iloc[-1]
    s20 = sma(tf["close"], 20).iloc[-1]
    s50 = sma(tf["close"], 50).iloc[-1]
    s200 = sma(tf["close"], 200).iloc[-1] if len(tf) >= 200 else np.nan
    a = atr(tf, 14)
    a_now = a.iloc[-1]

    lights2.append(light("EMA 20/50 交叉", "EMA 交叉", e20 > e50,
                         f"EMA20 {e20:.2f}", "> EMA50", f"EMA20 {e20:.2f} {'高於' if e20 > e50 else '低於'} EMA50 {e50:.2f}"))
    if not np.isnan(s200):
        multi = s20 > s50 > s200
        lights2.append(light("SMA 三線多頭", "三線多頭", bool(multi),
                             f"SMA20 {s20:.2f}／50 {s50:.2f}／200 {s200:.2f}", "20>50>200",
                             f"SMA 20/50/200 = {s20:.2f}/{s50:.2f}/{s200:.2f}，{'多頭排列' if multi else '未成多頭排列'}"))
        bull200 = c > s200
        lights2.append(light("200 SMA 牛熊線", "牛熊線", bool(bull200), f"收 {c:.2f}", "> SMA200",
                             f"現價 {c:.2f} {'站穩' if bull200 else '未站穩'} 200 SMA（{s200:.2f}）之上"))
        levels["sma200"] = round(float(s200), 2)
    else:
        lights2.append(light("SMA 三線多頭", "三線多頭", False, "數據不足", "20>50>200", "K 線不足 200 支"))
        lights2.append(light("200 SMA 牛熊線", "牛熊線", False, "數據不足", "> SMA200", "K 線不足 200 支"))

    st = supertrend(tf)
    st_now = st.iloc[-1]
    st_buy = c > st_now
    lights2.append(light("Supertrend", "Supertrend", bool(st_buy),
                         f"ST {st_now:.2f}", "Buy 狀態", f"Supertrend(10,3) 線 {st_now:.2f} 喺價格{'下' if st_buy else '上'}，{'Buy' if st_buy else 'Sell'}"))

    adx, pdi, mdi = adx_dmi(tf)
    adx_now = adx.iloc[-1]
    lights2.append(light("ADX 趨勢強度", "ADX", adx_now > 25, f"ADX {adx_now:.1f}", "> 25",
                         f"ADX(14) = {adx_now:.1f}，{'趨勢夠強' if adx_now > 25 else '趨勢未夠強'}"))
    lights2.append(light("DMI 方向", "DMI", pdi.iloc[-1] > mdi.iloc[-1],
                         f"+DI {pdi.iloc[-1]:.1f}", "> −DI",
                         f"+DI {pdi.iloc[-1]:.1f} vs −DI {mdi.iloc[-1]:.1f}"))

    sa, sb = ichimoku_cloud(tf)
    cloud_top = np.nanmax([sa.iloc[-1], sb.iloc[-1]])
    above_cloud = c > cloud_top if not np.isnan(cloud_top) else False
    lights2.append(light("Ichimoku 一目雲", "一目雲", bool(above_cloud), f"雲頂 {cloud_top:.2f}", "價 > 雲帶",
                         f"現價 {c:.2f}，雲帶頂 {cloud_top:.2f}，{'喺雲上' if above_cloud else '喺雲內或雲下'}"))

    sar = psar(tf).iloc[-1]
    below = sar < c
    lights2.append(light("Parabolic SAR", "PSAR", bool(below), f"SAR {sar:.2f}", "喺價下",
                         f"SAR {sar:.2f} 喺現價 {'下' if below else '上'}，{'多頭' if below else '空頭'}"))

    dmid = donchian_mid(tf, 20).iloc[-1]
    lights2.append(light("唐奇安通道", "唐奇安", c > dmid, f"中軌 {dmid:.2f}", "價 > 中軌",
                         f"現價 {c:.2f} vs 20 期中軌 {dmid:.2f}"))
    levels["donchian_mid"] = round(float(dmid), 2)

    sh, sl = swing_points(tf.tail(80).reset_index(drop=True))
    hh_hl = False
    if len(sh) >= 2 and len(sl) >= 2:
        t80 = tf.tail(80).reset_index(drop=True)
        h1, h2 = t80["high"].iloc[sh[1]], t80["high"].iloc[sh[0]]
        l1, l2 = t80["low"].iloc[sl[1]], t80["low"].iloc[sl[0]]
        hh_hl = (h2 > h1) and (l2 > l1)
        lights2.append(light("Price Action 結構", "HH/HL", hh_hl,
                             f"H {h1:.2f}→{h2:.2f}／L {l1:.2f}→{l2:.2f}", "HH＋HL",
                             f"高點 {h1:.2f}→{h2:.2f}，低點 {l1:.2f}→{l2:.2f}，{'連續更高高低點' if hh_hl else '未成 HH/HL 結構'}"))
        hl_level = float(t80["low"].iloc[sl[0]])
    else:
        lights2.append(light("Price Action 結構", "HH/HL", False, "數據不足", "HH＋HL", "搵唔到足夠 swing 點"))
        hl_level = float(tf["low"].iloc[-30:].min()) if len(tf) >= 30 else float(tf["low"].min())

    r = rsi(tf["close"], 14).iloc[-1]
    lights3.append(light("RSI 強勢區", "RSI", 50 <= r <= 70, f"RSI {r:.1f}", "50–70",
                         f"RSI(14) = {r:.1f}，{'喺強勢區' if 50 <= r <= 70 else '唔喺 50–70 強勢區'}"))

    line, sig, hist = macd(tf["close"])
    both_pos = line.iloc[-1] > 0 and sig.iloc[-1] > 0
    lights3.append(light("MACD 0軸上方", "MACD", bool(both_pos),
                         f"DIF {line.iloc[-1]:.2f}／DEA {sig.iloc[-1]:.2f}", "雙線 > 0",
                         f"DIF {line.iloc[-1]:.2f}，DEA {sig.iloc[-1]:.2f}，{'都喺 0 軸上' if both_pos else '未齊喺 0 軸上'}"))

    hist_now = hist.iloc[-1]
    hist_prev = hist.iloc[-2]
    expanding = hist_now > 0 and hist_now > hist_prev
    lights3.append(light("MACD 柱狀圖", "柱狀圖", bool(expanding),
                         f"Hist {hist_now:.2f}", "> 0 且擴大",
                         f"Hist {hist_prev:.2f} → {hist_now:.2f}，{'正向擴大' if expanding else '未正向擴大'}"))

    k, d = stoch(tf)
    kd_ok = k.iloc[-1] > d.iloc[-1] and k.iloc[-1] > 50
    lights3.append(light("Stochastic KD", "KD", bool(kd_ok),
                         f"K {k.iloc[-1]:.1f}／D {d.iloc[-1]:.1f}", "K>D 且 K>50",
                         f"%K {k.iloc[-1]:.1f}，%D {d.iloc[-1]:.1f}"))

    cc = cci(tf, 20).iloc[-1]
    lights3.append(light("CCI 順勢", "CCI", cc > 100, f"CCI {cc:.0f}", "> +100",
                         f"CCI(20) = {cc:.0f}"))
    ro = roc(tf["close"], 12).iloc[-1]
    lights3.append(light("ROC", "ROC", ro > 0, f"ROC {ro:+.2f}%", "> 0",
                         f"ROC(12) = {ro:+.2f}%"))
    ao = awesome(tf)
    ao_ok = ao.iloc[-1] > 0 and abs(ao.iloc[-1]) > abs(ao.iloc[-2])
    lights3.append(light("Awesome Osc", "AO", bool(ao_ok), f"AO {ao.iloc[-1]:.2f}", "> 0 且擴張",
                         f"AO {ao.iloc[-2]:.2f} → {ao.iloc[-1]:.2f}"))
    cm = cmo(tf["close"], 14).iloc[-1]
    lights3.append(light("CMO", "CMO", cm > 0, f"CMO {cm:.1f}", "> 0", f"CMO(14) = {cm:.1f}"))

    ob = obv(tf)
    obv_ma = sma(ob, 20).iloc[-1]
    lights3.append(light("OBV 能量潮", "OBV", ob.iloc[-1] > obv_ma, "OBV > 20日均", "OBV > MA20",
                         f"OBV {'喺' if ob.iloc[-1] > obv_ma else '跌穿'} 20 期均線{'上' if ob.iloc[-1] > obv_ma else ''}"))

    vol_ma = sma(tf["volume"], 20).iloc[-1]
    vol_now = tf["volume"].iloc[-1]
    lights3.append(light("Volume 放大", "放量", vol_now > vol_ma,
                         f"Vol {vol_now:,.0f}", "> VolMA20",
                         f"現成交量 {vol_now:,.0f} vs 20 期均量 {vol_ma:,.0f}（{vol_now / vol_ma * 100:.0f}%）"))

    dist20 = abs(c - e20)
    pullback = dist20 <= a_now if not np.isnan(a_now) else False
    lights4.append(light("EMA 20 回調支撐", "回調支撐", bool(pullback),
                         f"距 EMA20 {dist20:.2f}（{dist20 / a_now:.1f} ATR）" if not np.isnan(a_now) else "—",
                         "≤ 1×ATR",
                         f"價距 EMA20 {dist20:.2f}，ATR {a_now:.2f}，{dist20:.2f} ≤ ATR？" if not np.isnan(a_now) else "ATR 無數據"))

    vwap = vwap_daily if vwap_daily and vwap_daily > 0 else np.nan
    if not np.isnan(vwap):
        above_vwap = c > vwap
        lights4.append(light("VWAP", "VWAP", bool(above_vwap), f"VWAP {vwap:.2f}", "價 > VWAP",
                             f"現價 {c:.2f} {'高於' if above_vwap else '低於'} 今日 VWAP {vwap:.2f}"))
        levels["vwap"] = round(float(vwap), 2)
    else:
        lights4.append(light("VWAP", "VWAP", None, "無數據", "價 > VWAP", "今日冇成交，冇 VWAP", manual=True))

    bmid, bup, _ = bollinger(tf["close"], 20)
    in_band = c >= bmid.iloc[-1] and c <= bup.iloc[-1]
    lights4.append(light("Bollinger Bands", "BB", bool(in_band),
                         f"{bmid.iloc[-1]:.2f}–{bup.iloc[-1]:.2f}", "中軌至上軌",
                         f"現價 {c:.2f}，帶內 {bmid.iloc[-1]:.2f}–{bup.iloc[-1]:.2f}"))
    levels["bb_mid"] = round(float(bmid.iloc[-1]), 2)

    last = tf.iloc[-1]
    prev_bar = tf.iloc[-2]
    body = abs(last["close"] - last["open"])
    lower_shadow = min(last["close"], last["open"]) - last["low"]
    upper_shadow = last["high"] - max(last["close"], last["open"])
    rng = last["high"] - last["low"]
    hammer = rng > 0 and lower_shadow >= 2 * body and upper_shadow <= body
    engulf = (prev_bar["close"] < prev_bar["open"] and last["close"] > last["open"]
              and last["close"] >= prev_bar["open"] and last["open"] <= prev_bar["close"])
    long_lower = rng > 0 and lower_shadow >= 0.6 * rng
    bullish = bool(hammer or engulf or long_lower)
    pat = []
    if hammer:
        pat.append("錘子線")
    if engulf:
        pat.append("看漲吞沒")
    if long_lower:
        pat.append("長下影")
    lights4.append(light("看漲 K 線形態", "K 線形態", bullish,
                         "＋".join(pat) if pat else "冇形態", "錘子／吞沒／長下影",
                         f"最新 K 線{'出現' if bullish else '冇出現'}看漲形態（{'＋'.join(pat)}）" if pat else "最新 K 線冇出現錘子／吞沒／長下影形態"))

    r_prev = rsi(tf["close"], 14).iloc[-2]
    hook = r_prev < 50 and r > r_prev and r >= 48
    lights4.append(light("RSI 勾頭向上", "RSI 勾頭", bool(hook),
                         f"RSI {r_prev:.1f}→{r:.1f}", "由 50 下勾頭上",
                         f"RSI 由 {r_prev:.1f} 到 {r:.1f}，{'完成向上勾頭' if hook else '未完成向上勾頭'}"))

    k_prev, d_prev = k.iloc[-2], d.iloc[-2]
    gold = k_prev < d_prev and k.iloc[-1] > d.iloc[-1] and 18 <= k_prev <= 45
    lights4.append(light("KD 超賣金叉", "KD 金叉", bool(gold),
                         f"K {k_prev:.1f}→{k.iloc[-1]:.1f}", "20–40 區金叉",
                         f"KD 由 K {k_prev:.1f}/D {d_prev:.1f} 到 K {k.iloc[-1]:.1f}／D {d.iloc[-1]:.1f}，{'20–45 區金叉' if gold else '未喺超賣區金叉'}"))

    ranges3 = (tf["high"].tail(3) - tf["low"].tail(3))
    calm = ranges3.max() < 3 * a_now if not np.isnan(a_now) else False
    lights4.append(light("ATR 波動控制", "波幅控制", bool(calm),
                         f"近3根最大 {ranges3.max():.2f}", "< 3×ATR",
                         f"近 3 根最大波幅 {ranges3.max():.2f} vs 3×ATR = {3 * a_now:.2f}" if not np.isnan(a_now) else "ATR 無數據"))

    ph, pl, pc = prev_bar["high"], prev_bar["low"], prev_bar["close"]
    piv = (ph + pl + pc) / 3
    s1 = 2 * piv - ph
    above_piv = c > max(piv, s1)
    lights4.append(light("Pivot Point", "Pivot", bool(above_piv),
                         f"P {piv:.2f}／S1 {s1:.2f}", "價 > P 或 S1",
                         f"現價 {c:.2f}，Pivot {piv:.2f}，S1 {s1:.2f}"))
    levels["pivot"] = round(float(piv), 2)
    levels["pivot_s1"] = round(float(s1), 2)

    kmid = keltner_mid(tf, 20).iloc[-1]
    above_k = c > kmid
    lights4.append(light("Keltner 通道", "Keltner", bool(above_k), f"中軌 {kmid:.2f}", "持穩中軌上",
                         f"現價 {c:.2f} {'高於' if above_k else '低於'} Keltner 中軌 {kmid:.2f}"))

    t80 = tf.tail(80).reset_index(drop=True)
    sh2, _ = swing_points(t80)
    flip = False
    flip_level = np.nan
    for idx in sh2:
        h_val = t80["high"].iloc[idx]
        later_break = t80["close"].iloc[idx + 1:].max() > h_val if idx + 1 < len(t80) else False
        if later_break and h_val < c:
            flip = True
            flip_level = h_val
            break
    if not np.isnan(flip_level):
        lights4.append(light("S/R Flip 前高轉換", "S/R 轉換", flip,
                             f"前高 {flip_level:.2f}", "站上前高（轉支撐）",
                             f"前顯著高點 {flip_level:.2f} 已被突破，現價 {c:.2f} {'企穩' if flip else '未企穩'}之上（阻力轉支撐）"))
        levels["sr_flip"] = round(float(flip_level), 2)
    else:
        lights4.append(light("S/R Flip 前高轉換", "S/R 轉換", None, "—", "站上前高",
                             "近 80 根搵唔到「突破後回測」嘅前高，請手動判斷", manual=True))

    levels["ema20"] = round(float(e20), 2)
    levels["ema50"] = round(float(e50), 2)
    levels["atr"] = round(float(a_now), 2)
    levels["hl"] = round(hl_level, 2)
    levels["hl_sl"] = round(hl_level - 1.5 * a_now, 2) if not np.isnan(a_now) else None
    levels["hl_tp"] = round(c + 2 * (c - (hl_level - 1.5 * a_now)), 2) if not np.isnan(a_now) else None
    return lights2, lights3, lights4, levels


# ---------------------------------------------------------------- 主流程

def score(code: str, strategy: str) -> dict:
    code = (code or "").strip().zfill(5)
    strategy = strategy if strategy in STRATEGY_TF else "mid"
    key = (code, strategy)
    hit = SCORE_CACHE.get(key)
    if hit and time.time() - hit[0] < SCORE_TTL:
        return hit[1]

    futu_code = f"HK.{code}"
    cfg = STRATEGY_TF[strategy]
    from futu import KLType
    ktype = getattr(KLType, cfg["ktype"])

    daily = fetch_kline(futu_code, "K_DAY", 500)
    hsi_daily = fetch_kline("HK.800000", "K_DAY", 500)
    if daily.empty:
        raise RuntimeError(f"{code} 攞唔到日線數據（代號唔存在或無數據）")

    tf_span = cfg["span_days"]
    tf = fetch_kline(futu_code, cfg["ktype"], tf_span) if strategy != "long" else daily.copy()
    if tf.empty:
        tf = daily.copy()

    snaps = snapshot([futu_code, "HK.800000"])
    snap = snaps[0]
    if snap is not None:
        spot = _f(snap.get("last_price"))
        prev_c = _f(snap.get("prev_close_price"))
        vol = _f(snap.get("volume"), 0.0)
        turn = _f(snap.get("turnover"), 0.0)
        vwap_d = turn / vol if vol > 0 and turn > 0 else None
    else:
        spot = float(daily["close"].iloc[-1])
        prev_c = float(daily["close"].iloc[-2]) if len(daily) > 1 else None
        vwap_d = None

    sector = sector_of(code)
    l1 = module1(code, futu_code, snap, daily, hsi_daily, sector)
    l2, l3, l4, levels = module234(daily, tf, vwap_d, spot)

    modules = [
        {"id": 1, "title": MODULE_TITLES[0], "lights": l1},
        {"id": 2, "title": MODULE_TITLES[1], "lights": l2},
        {"id": 3, "title": MODULE_TITLES[2], "lights": l3},
        {"id": 4, "title": MODULE_TITLES[3], "lights": l4},
    ]
    for m in modules:
        m["count"] = int(sum(1 for L in m["lights"] if L["passed"] is True or L["passed"] == np.bool_(True)))

    total = sum(m["count"] for m in modules)
    if total >= 34:
        grade, pct, action = "A+", 100, "立即執行（標準倉 100%）"
    elif total >= 28:
        grade, pct, action = "B+", 50, "分階段試倉（半倉 50%）"
    elif total >= 20:
        grade, pct, action = "觀望", 0, "禁止下單，入 Watchlist"
    else:
        grade, pct, action = "放棄", 0, "無交易價值，放棄"

    veto_modules = [m for m in modules if m["count"] < 6]
    veto = {"triggered": bool(veto_modules), "modules": [m["title"] for m in veto_modules]}

    steps = []
    for m in [modules[i] for i in FUNNEL_ORDER]:
        steps.append({"id": m["id"], "title": MODULE_TITLES[m["id"] - 1],
                      "count": m["count"], "passed": m["count"] >= 8})
    stage = "已過全部漏鬥"
    for i, s in enumerate(steps):
        if not s["passed"]:
            stage = f"卡喺步驟{i + 1}：{s['title']}"
            break

    c = float(tf["close"].iloc[-1])
    atr_v = levels.get("atr")
    hl = levels.get("hl")
    sl = tp = None
    if atr_v and hl:
        sl = levels.get("hl_sl")
        tp = levels.get("hl_tp")
    risk = {"atr": atr_v, "hl": hl, "sl": sl, "tp": tp,
            "rr": 2.0, "sl_formula": "最近 HL − 1.5×ATR", "tp_formula": "入場 + 2×(入場 − SL)"}

    res = {"ok": True, "code": code, "futu_code": futu_code,
           "strategy": strategy, "strategy_label": cfg["label"],
           "tf_trio": cfg["trio"],
           "modules": modules, "total": total, "max": 40,
           "grade": grade, "grade_pct": pct, "action": action,
           "veto": veto, "funnel": {"steps": steps, "stage": stage},
           "risk": risk, "levels": levels,
           "spot": round(spot, 2) if spot else None, "prev_close": round(prev_c, 2) if prev_c else None,
           "vwap": levels.get("vwap"), "sector": sector,
           "time": datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S HKT")}
    SCORE_CACHE[key] = (time.time(), res)
    return res


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[multi-factor] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Ext-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Ext-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/health":
                ok = False
                try:
                    from futu import RET_OK
                    ret, _ = quote_ctx().get_global_state()
                    ok = (ret == RET_OK)
                except Exception:
                    ok = False
                self._send({"ok": True, "service": "multi-factor", "opend": ok,
                            "time": datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S HKT")})
                return
            if u.path == "/score":
                code = (q.get("code") or [""])[0]
                strategy = (q.get("strategy") or ["mid"])[0]
                if not code.strip():
                    self._send({"ok": False, "error": "需要 code 參數"}, 400)
                    return
                res = score(code, strategy)
                self._send(res)
                return
            if u.path == "/universe":
                self._send({"ok": True, "universe": universe_codes()})
                return
            self._send({"ok": False, "error": "unknown path"}, 404)
        except Exception as e:
            import traceback as _tb
            _tb.print_exc()
            reset_ctx()
            self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[multi-factor] listening on 127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
