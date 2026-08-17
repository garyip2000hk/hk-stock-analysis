"""
ccass_loader.py — 從 CCASS dailylog 載入集中度數據，供回測策略使用
"""
import pandas as pd
import numpy as np
from pathlib import Path


CCASS_DB = Path(__file__).resolve().parent.parent.parent / "Desktop" / "db" / "CCASS"


def load_shortnames() -> pd.DataFrame:
    """issue_id → stock_code 映射"""
    sn = pd.read_parquet(CCASS_DB / "shortnames.parquet")
    # 只取最新嘅映射（一個 issue 可能有多個 from_date）
    sn = sn.sort_values("use_date").drop_duplicates("issue_id", keep="last")
    return sn[["issue_id", "stock_code"]].copy()


def load_dailylog_pivot() -> pd.DataFrame:
    """
    讀 dailylog，join shortnames，pivot 成以 (date, code) 為 index 嘅 DataFrame。
    欄位：c5, c10, intermed_hldg, intermed_cnt
    """
    dl = pd.read_parquet(CCASS_DB / "dailylog.parquet")
    sn = load_shortnames()
    dl["issue_id"] = dl["issue_id"].astype(str)
    sn["issue_id"] = sn["issue_id"].astype(str)
    df = dl.merge(sn, on="issue_id", how="inner")
    df["code"] = df["stock_code"].str.zfill(5)
    df["at_date"] = pd.to_datetime(df["at_date"])
    return df[["at_date", "code", "c5", "c10", "intermed_hldg", "intermed_cnt"]].copy()


def build_c10_changes(close_pivot_index=None) -> pd.DataFrame:
    """
    計算每隻股票 c10（頭 10 大佔比）嘅 5 日同 20 日變化。
    返回 DataFrame：index=date, columns=code, values=Δc10(5d)
    """
    dl = load_dailylog_pivot()
    # 需要 issued_shares 做分母
    issued = pd.read_parquet(CCASS_DB / "issued_shares.parquet")
    if "issued_shares" in issued.columns:
        issued = issued.rename(columns={"stock_code": "code"})
        issued["code"] = issued["code"].str.zfill(5)
        issued = issued[["code", "issued_shares"]].drop_duplicates("code", keep="last")
        dl = dl.merge(issued, on="code", how="left")
        dl["c10_pct"] = dl["c10"] / dl["issued_shares"] * 100
    else:
        # fallback: 用 intermed_hldg 做近似分母
        dl["c10_pct"] = dl["c10"] / dl["intermed_hldg"].replace(0, np.nan) * 100

    # pivot：index=at_date, columns=code, values=c10_pct
    pivot = dl.pivot_table(index="at_date", columns="code", values="c10_pct", aggfunc="last")
    pivot = pivot.sort_index().ffill()

    delta_5d = pivot.diff(5)
    delta_20d = pivot.diff(20)
    return delta_5d, delta_20d


def build_intermed_changes() -> tuple:
    """
    計算 intermediaries 持倉人數嘅 5 日同 20 日變化。
    人數收窄 = 洗籌 / 歸邊。
    回傳 (delta_5d, delta_20d) 兩個 DataFrame。
    """
    dl = load_dailylog_pivot()
    pivot = dl.pivot_table(index="at_date", columns="code", values="intermed_cnt", aggfunc="last")
    pivot = pivot.sort_index().ffill()
    delta_5d = pivot.diff(5)
    delta_20d = pivot.diff(20)
    return delta_5d, delta_20d


if __name__ == "__main__":
    print("Loading CCASS data...")
    d5, d20 = build_c10_changes()
    print(f"c10 5d delta: {d5.shape}, dates {d5.index.min()} → {d5.index.max()}")
    intermed = build_intermed_changes()
    print(f"intermed cnt 5d delta: {intermed.shape}")
    print("Done.")
