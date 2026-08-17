"""contract_specs_hkex.py — 由 HKEX 官方產品頁建 `options_data/contract_specs.json`。

為咩要呢個模組
--------------
`futu_data_importer.py` 本來由 Futu OpenD 攞 contract_size，但：

  1. OpenD 唔一定開住（雲端／CI 完全冇）；
  2. contract_size 係**交易所合約規格**，唔係行情數據 —— 由 HKEX
     產品頁攞係第一手來源，比經券商 API 更權威；
  3. 冇咗 contract_specs.json，`quant_engine/options_backtester.py`
     嘅 `codes = chain.stock_code ∩ specs` 會係空集，回測靜靜咁跑出零筆
     交易而唔會報錯 —— 呢個係最危險嘅失敗方式。

contract_size 直接乘落每一筆損益，同時決定固定費用（每張 HK$11.6）
佔毛利幾多百分比，所以錯一個數就足以令 tier 排名完全唔同。

來源：https://www.hkex.com.hk/Products/Listed-Derivatives/Single-Stock/Stock-Options?sc_lang=en
表 (a) = 合約張數大於一個買賣單位嘅類別（多一欄 Number of Board Lots）
表 (b) = 合約張數等於一個買賣單位嘅類別
兩張表欄位唔同，所以下面用「搵 HKATS 代號欄再向左右取數」而唔係固定欄位。

用法：
    python3 contract_specs_hkex.py            # 寫入 options_data/contract_specs.json
    python3 contract_specs_hkex.py --check    # 只列出，唔寫檔
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
OUT = BASE / "options_data" / "contract_specs.json"
URL = ("https://www.hkex.com.hk/Products/Listed-Derivatives/Single-Stock/"
       "Stock-Options?sc_lang=en")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36")
}
# HKATS 代號一律 3 個字符、第一個一定係字母（例 TCH、ANA、A50）。
# 唔加「第一個係字母」就會撞到表 (a) 嘅 Position Limit 等純數字欄，
# 令 00016 嘅 hkats 變成 "241"。
HKATS_RE = re.compile(r"^[A-Z][A-Z0-9]{2}$")

# 抽查用：呢幾隻嘅 contract_size 係公開已知，數值變咗就係解析出錯。
SANITY = {"00700": 100, "00005": 400, "01299": 1000, "02020": 200, "09988": 500}


def _cells(tr: str) -> list[str]:
    raw = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
    return [re.sub(r"<[^>]+>", " ", c)
            .replace("&nbsp;", " ").replace("\u200b", "").strip()
            for c in raw]


def parse(html: str) -> dict[str, dict]:
    specs: dict[str, dict] = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        c = _cells(tr)
        if len(c) < 5:
            continue
        idx = [i for i, x in enumerate(c) if HKATS_RE.fullmatch(x)]
        if not idx:
            continue
        i = idx[0]
        # SEHK 代號 = HKATS 欄左邊最後一個純數字欄（左邊仲有個 No. 序號欄）
        nums = [j for j in range(i) if re.fullmatch(r"\d{1,5}", c[j].replace(",", ""))]
        if not nums:
            continue
        code = c[nums[-1]].replace(",", "").zfill(5)
        # contract_size = HKATS 欄右邊第一個純數字欄
        size = None
        for y in c[i + 1:]:
            y2 = y.replace(",", "")
            if re.fullmatch(r"\d{1,6}", y2):
                size = int(y2)
                break
        if not size:
            continue
        name = c[nums[-1] + 1] if nums[-1] + 1 < i else ""
        specs[code] = {"hkats": c[i], "contract_size": size, "name": name}
    return dict(sorted(specs.items()))


def fetch() -> dict[str, dict]:
    r = requests.get(URL, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return parse(r.text)


def check(specs: dict[str, dict]) -> list[str]:
    """返回唔對數嘅抽查項目。空 list = 全部通過。"""
    bad = []
    for code, want in SANITY.items():
        got = (specs.get(code) or {}).get("contract_size")
        if got != want:
            bad.append(f"{code}: 應為 {want}，解析到 {got}")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser(description="由 HKEX 建 contract_specs.json")
    ap.add_argument("--check", action="store_true", help="只顯示，唔寫檔")
    a = ap.parse_args()

    specs = fetch()
    bad = check(specs)
    print(f"HKEX 期權類別：{len(specs)} 隻")
    for code in ("00700", "00005", "01299", "02020", "09988"):
        s = specs.get(code)
        if s:
            print(f"  {code} {s['hkats']:<4} {s['contract_size']:>6} 股  {s['name']}")
    if bad:
        print("\n⚠ 抽查唔對數，唔會寫檔：")
        for b in bad:
            print("   " + b)
        raise SystemExit(1)

    if a.check:
        print("\n抽查通過（--check 模式，未寫檔）")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(specs, ensure_ascii=False, indent=1))
    print(f"\n✅ {OUT}")


if __name__ == "__main__":
    main()
