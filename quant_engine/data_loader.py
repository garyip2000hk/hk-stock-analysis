"""
data_loader.py — 載入港股報價數據
從 imported/quotes.json 讀取，轉換成 DataFrame + pivot
兼容新舊格式：
  舊：{date: {code: {close, ...}}}
  新：{meta: {...}, names: {code: name}, quotes: {date: {code: {close, ...}}}}
"""
import json
import pandas as pd
from pathlib import Path

BASE = Path(__file__).parent.parent / "imported"


def load_quotes():
    path = BASE / "quotes.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到 {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("quotes.json 格式不符預期 (應為 dict)")

    # 兼容新格式
    if "quotes" in data and isinstance(data["quotes"], dict):
        quotes_raw = data["quotes"]
        names_map = data.get("names", {})
    else:
        quotes_raw = data
        names_map = {}

    # 嘗試從 stock_names.json 補充
    if not names_map:
        names_path = BASE / "stock_names.json"
        if names_path.exists():
            with open(names_path, "r", encoding="utf-8") as f:
                names_map = json.load(f)

    rows = []
    for date, stocks in quotes_raw.items():
        if not isinstance(stocks, dict):
            continue
        for code, info in stocks.items():
            if not isinstance(info, dict):
                continue
            c = info.get("close")
            if c is None or c == 0:
                continue
            rows.append({
                "date": date,
                "code": code,
                "close": c,
                "high": info.get("high"),
                "low": info.get("low"),
                "vol": info.get("vol"),
                "turnover": info.get("turnover"),
            })

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce")
    df = df.dropna(subset=["close"])

    df = df.sort_values(["date", "code"]).reset_index(drop=True)

    close_pivot = df.pivot_table(index="date", columns="code", values="close", aggfunc="last")
    close_pivot = close_pivot.sort_index()

    print(f"  ✓ 載入 {len(df)} 條報價記錄")
    print(f"  ✓ 交易日: {close_pivot.index[0].date()} → {close_pivot.index[-1].date()} ({len(close_pivot)} 日)")
    print(f"  ✓ 股票數: {len(close_pivot.columns)} 隻")

    return df, close_pivot, names_map
