"""earnings_calendar.py — 由 HKEX 「董事會召開日期」公告抽出業績日。

期權策略最緊要知「幾時有大波幅事件」。港股冇官方業績日曆，
但上市規則 13.43 條要求公司事前公布董事會會議日期，
所以「公告及通告 - [董事會召開日期]」就係最準嘅業績日先行指標。

流程：
  1. 由 imported/announcements.json 揀出期權標的嘅董事會會議公告
  2. 下載 PDF，抽文字存 options_data/board_text/<news_id>.txt.gz（只下載一次）
  3. 由存好嘅文字 parse 出會議日期（可以隨時重新 parse，唔需要再下載）

注意：部分公告 PDF 用非標準字型，中文會變亂碼，但阿拉伯數字日期仍然可讀，
所以 parser 有寬鬆模式（數字 + 任意分隔符），再用公告標題判斷係唔係業績。

CLI:
    python3 earnings_calendar.py                 # 更新 + 列未來業績日
    python3 earnings_calendar.py --reparse       # 只重新 parse（唔下載）
    python3 earnings_calendar.py --stock 00700
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

BASE = Path(__file__).parent
SPECS = BASE / "options_data" / "contract_specs.json"
ANNS = BASE / "imported" / "announcements.json"
CACHE = BASE / "options_data" / "earnings_calendar.json"
TEXT_DIR = BASE / "options_data" / "board_text"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"}
BOARD_MEETING = "董事會召開日期"

RESULT_WORDS = ("業績", "中期", "年度", "季度", "全年", "末期", "業 績",
                "interim", "annual", "quarterly", "results")
DIV_WORDS = ("股息", "分派", "派息", "dividend")

DATE_CN = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
DATE_EN = re.compile(
    r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(20\d{2})", re.I
)
# 亂碼模式：2026 ϋ8˜20  →  年月日之間係非數字雜訊
DATE_LOOSE = re.compile(r"(20\d{2})\D{1,4}(\d{1,2})\D{1,4}(\d{1,2})")
CN_YEAR = re.compile(r"([二零一三四五六七八九十]{4})\s*年\s*"
                     r"([一二三四五六七八九十]{1,3})\s*月\s*"
                     r"([一二三四五六七八九十]{1,3})\s*日")
CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_num(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + CN_DIGITS.get(s[1:2], 0)
    if "十" in s:
        a, _, b = s.partition("十")
        return CN_DIGITS.get(a, 0) * 10 + (CN_DIGITS.get(b, 0) if b else 0)
    if len(s) == 4:
        out = 0
        for ch in s:
            if ch not in CN_DIGITS:
                return None
            out = out * 10 + CN_DIGITS[ch]
        return out
    return CN_DIGITS.get(s)


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _text_path(news_id: str) -> Path:
    return TEXT_DIR / f"{news_id}.txt.gz"


def _save_text(news_id: str, txt: str) -> None:
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(_text_path(news_id), "wt", encoding="utf-8") as fh:
        fh.write(txt)


def _read_text(news_id: str) -> str | None:
    p = _text_path(news_id)
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8", errors="ignore") as fh:
        return fh.read()


def _download_text(url: str) -> str | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=45)
        if r.status_code != 200:
            return None
        reader = PdfReader(io.BytesIO(r.content))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:3])
    except Exception:
        return None


def _candidates(text: str) -> tuple[list[date], bool]:
    """抽所有日期。回傳 (日期, 中文可讀)。"""
    flat = re.sub(r"[ \t\u3000]+", "", text)
    readable = ("年" in flat and "日" in flat) or "董事會" in flat
    out: set[date] = set()

    for y, m, d in DATE_CN.findall(flat):
        try:
            out.add(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    for y, m, d in CN_YEAR.findall(flat):
        yy, mm, dd = _cn_num(y), _cn_num(m), _cn_num(d)
        if yy and mm and dd:
            try:
                out.add(date(yy, mm, dd))
            except ValueError:
                pass
    for d, mon, y in DATE_EN.findall(text):
        try:
            out.add(datetime.strptime(f"{d} {mon[:3]} {y}", "%d %b %Y").date())
        except ValueError:
            pass
    if not readable or not out:
        for y, m, d in DATE_LOOSE.findall(flat):
            try:
                out.add(date(int(y), int(m), int(d)))
            except ValueError:
                pass
    return sorted(out), readable


def extract(text: str, ann_date: str, title: str = "") -> dict:
    """抽會議日期。會議日一定 > 公告日（公告尾嘅簽署日 = 公告日，要排除）。"""
    cands, readable = _candidates(text)
    ad = date.fromisoformat(ann_date)
    window = ad + timedelta(days=120)

    future = [c for c in cands if ad < c <= window]
    same_day = [c for c in cands if c == ad]
    meeting = future[0] if future else (same_day[0] if same_day else None)

    hay = f"{title} {text if readable else ''}".lower()
    is_results = any(w in hay for w in RESULT_WORDS)
    if not readable and not is_results:
        # 亂碼 + 標題冇講明 → 董事會會議按上市規則絕大多數係批業績
        is_results = True
    return {
        "meeting_date": meeting.isoformat() if meeting else None,
        "is_results": is_results,
        "has_dividend": any(w in hay for w in DIV_WORDS),
        "text_ok": readable,
        "confidence": "high" if (readable and future) else
                      ("mid" if future else "low"),
    }


def _pending(anns: list[dict], codes: set[str], since_days: int) -> list[dict]:
    cutoff = (date.today() - timedelta(days=since_days)).isoformat()
    return [
        a for a in anns
        if a["stock_code"] in codes
        and BOARD_MEETING in (a.get("doc_type") or "")
        and a["date"] >= cutoff
        and a.get("file_link")
        and a.get("news_id")
    ]


def build(since_days: int = 150, reparse_only: bool = False,
          verbose: bool = True) -> dict:
    specs = _load(SPECS)
    anns = _pending(_load(ANNS), set(specs), since_days)
    cache: dict[str, dict] = {}

    n_dl = 0
    for a in anns:
        nid = a["news_id"]
        txt = _read_text(nid)
        if txt is None:
            if reparse_only:
                continue
            txt = _download_text(a["file_link"])
            if txt:
                _save_text(nid, txt)
                n_dl += 1
        rec = {
            "stock_code": a["stock_code"],
            "company": a["company"],
            "ann_date": a["date"],
            "title": a["title"],
            "link": a["file_link"],
        }
        rec.update(extract(txt, a["date"], a["title"]) if txt else
                   {"meeting_date": None, "is_results": None,
                    "has_dividend": None, "text_ok": False, "confidence": "none"})
        cache[nid] = rec

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    if verbose:
        ok = sum(1 for r in cache.values() if r["meeting_date"])
        print(f"董事會公告 {len(cache)} 份（新下載 {n_dl}），成功抽出日期 {ok}")
    return cache


def upcoming(days: int = 45, results_only: bool = True,
             include_past: int = 0) -> list[dict]:
    """未來 N 日內嘅業績日；每隻股票只留最近一個。"""
    if not CACHE.exists():
        return []
    today = date.today()
    lo = today - timedelta(days=include_past)
    hi = today + timedelta(days=days)
    best: dict[str, dict] = {}
    for rec in _load(CACHE).values():
        md = rec.get("meeting_date")
        if not md or (results_only and not rec.get("is_results")):
            continue
        m = date.fromisoformat(md)
        if not (lo <= m <= hi):
            continue
        cur = best.get(rec["stock_code"])
        if cur is None or md < cur["meeting_date"]:
            best[rec["stock_code"]] = rec
    out = []
    for rec in best.values():
        m = date.fromisoformat(rec["meeting_date"])
        out.append({**rec, "days_to_event": (m - today).days})
    out.sort(key=lambda r: r["days_to_event"])
    return out


def event_map(days: int = 90) -> dict[str, dict]:
    """stock_code → 最近一個業績事件（供策略引擎用）。"""
    return {r["stock_code"]: r for r in upcoming(days)}


def next_event(stock_code: str) -> dict | None:
    return event_map(120).get(stock_code.zfill(5))


def main() -> None:
    ap = argparse.ArgumentParser(description="期權標的業績日曆")
    ap.add_argument("--reparse", action="store_true", help="只重新 parse 已存文字")
    ap.add_argument("--stock", help="只睇一隻")
    ap.add_argument("--days", type=int, default=45)
    a = ap.parse_args()

    build(reparse_only=a.reparse)

    if a.stock:
        r = next_event(a.stock)
        print(json.dumps(r, ensure_ascii=False, indent=1) if r
              else "未來 120 日冇已公布業績日")
        return

    rows = upcoming(a.days)
    print(f"\n=== 未來 {a.days} 日業績日（期權標的）===\n")
    print(f"{'日期':<12} {'DTE':>4} {'代號':>6} {'公司':<20} {'派息':<4} 可信")
    for r in rows:
        print(f"{r['meeting_date']:<12} {r['days_to_event']:>4} {r['stock_code']:>6} "
              f"{r['company'][:20]:<20} {'✓' if r['has_dividend'] else '':<4} "
              f"{r['confidence']}")
    print(f"\n共 {len(rows)} 隻")


if __name__ == "__main__":
    main()
