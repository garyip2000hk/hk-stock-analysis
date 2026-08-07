"""
desktop_data_bridge.py
Import data from Desktop/db/ (synced from local Windows PC)
into structured formats for stock-analysis tools.

Data sources:
  DoD/          - HKEX announcements (911 stocks, 965 HTM + 9496 PDFs)
  CR/           - Company Registry weekly filings (F=carrying business, L=new incorporations)
  short positions/ - Weekly short selling data (55 files)
  BuybackList/  - GEM + MainBoard buyback lists
  quotes/       - Daily quotation HTM files (513 files)
  listingTeams.xlsx - Listing team data
"""

import csv, json, os, re, subprocess, sys
from datetime import datetime, date
from pathlib import Path
from html.parser import HTMLParser
from collections import defaultdict

DATA_ROOT = Path("/home/workspace/Desktop/db")
OUT_DIR = Path("/home/workspace/stock-analysis/imported")
OUT_DIR.mkdir(parents=True, exist_ok=True)
BASE_DIR = Path(__file__).parent


# ── Short Positions ─────────────────────────────────────────

def load_short_positions() -> dict:
    """Load all weekly short position CSVs → time series per stock."""
    sp_dir = DATA_ROOT / "short positions"
    if not sp_dir.exists():
        return {}

    records = []
    for f in sorted(sp_dir.glob("*.csv")):
        with open(f, "r", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    date_str = row.get("Date", "").strip()
                    if not date_str:
                        continue
                    dt = datetime.strptime(date_str, "%d/%m/%Y").date().isoformat()
                    code = row.get("Stock Code", "").strip().zfill(5)
                    shares = int(row.get("Aggregated Reportable Short Positions (Shares)", 0) or 0)
                    hk_dollars = int(row.get("Aggregated Reportable Short Positions (HK$)", 0) or 0)
                    records.append({
                        "date": dt,
                        "code": code,
                        "name": row.get("Stock Name", "").strip(),
                        "short_shares": shares,
                        "short_hkd": hk_dollars,
                        "source_file": f.name,
                    })
                except (ValueError, KeyError):
                    continue

    out = OUT_DIR / "short_positions.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)
    print(f"[short_positions] {len(records)} records → {out}")
    return {r["code"]: r for r in records[-len(records):]}


def get_short_analysis(records: list = None) -> list:
    """Top shorted stocks + week-over-week changes."""
    if records is None:
        f = OUT_DIR / "short_positions.json"
        if not f.exists():
            return []
        with open(f, "r", encoding="utf-8") as fh:
            records = json.load(fh)

    latest_date = max(r["date"] for r in records)
    latest = [r for r in records if r["date"] == latest_date]
    latest.sort(key=lambda x: x["short_hkd"], reverse=True)

    by_code = defaultdict(list)
    for r in records:
        by_code[r["code"]].append(r)

    result = []
    for r in latest[:50]:
        hist = sorted(by_code[r["code"]], key=lambda x: x["date"])
        prev = hist[-2] if len(hist) >= 2 else None
        change_pct = None
        if prev and prev["short_shares"] > 0:
            change_pct = round((r["short_shares"] - prev["short_shares"]) / prev["short_shares"] * 100, 2)
        result.append({
            **r,
            "prev_shares": prev["short_shares"] if prev else None,
            "change_pct": change_pct,
            "history_count": len(hist),
        })

    out = OUT_DIR / "short_analysis.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(f"[short_analysis] {len(result)} stocks → {out}")
    return result


# ── HKEX Announcements (DoD) ────────────────────────────────

class HTMParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.company = ""
        self.pdfs = []
        self._in_title = False
        self._in_h1 = False
        self._in_h2 = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
        elif tag == "h2":
            self._in_h2 = True
        elif tag == "a":
            href = dict(attrs).get("href", "")
            if href and not href.startswith("http"):
                self.pdfs.append(href)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        elif tag == "h2":
            self._in_h2 = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        elif self._in_h1:
            self.company += data.strip()
        elif self._in_h2:
            self.title += " " + data.strip()


def load_announcements() -> list:
    """公告：優先用 sync_announcements_cache.py（DoD 本地 + HKEXnews 增量）。"""
    if _run_sync("sync_announcements_cache.py", "announcements"):
        data = _read_out("announcements.json")
        if data is not None:
            return data
    return load_announcements_legacy()


def load_announcements_legacy() -> list:
    """Index all DoD HTM files → structured announcement list."""
    dod_dir = DATA_ROOT / "DoD"
    if not dod_dir.exists():
        return []

    announcements = []
    for stock_dir in sorted(dod_dir.iterdir()):
        if not stock_dir.is_dir():
            continue
        code = stock_dir.name
        for htm_file in stock_dir.rglob("*.htm"):
            try:
                raw = htm_file.read_text(encoding="utf-8", errors="ignore")
                p = HTMParser()
                p.feed(raw)
                fname = htm_file.stem
                dt_match = re.match(r"(\d{4})-(\d{2})-(\d{2})", fname)
                if dt_match:
                    dt = f"{dt_match.group(1)}-{dt_match.group(2)}-{dt_match.group(3)}"
                else:
                    dt_match = re.match(r"(\d{8})", fname)
                    dt = f"{dt_match.group(1)[:4]}-{dt_match.group(1)[4:6]}-{dt_match.group(1)[6:]}" if dt_match else None
                if not dt:
                    continue

                pdf_dir = htm_file.parent / htm_file.stem.replace(".htm", "")
                pdf_files = [f.name for f in pdf_dir.glob("*.pdf")] if pdf_dir.exists() else []

                announcements.append({
                    "stock_code": code,
                    "date": dt,
                    "company": p.company.strip(),
                    "title": p.title.strip(),
                    "htm_file": str(htm_file.relative_to(DATA_ROOT)),
                    "pdf_dir": str(pdf_dir.relative_to(DATA_ROOT)) if pdf_dir.exists() else None,
                    "pdf_files": pdf_files,
                })
            except Exception:
                continue

    announcements.sort(key=lambda x: (x["date"], x["stock_code"]))
    out = OUT_DIR / "announcements.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(announcements, fh, ensure_ascii=False, indent=2)
    print(f"[announcements] {len(announcements)} records → {out}")
    return announcements


def search_announcements(keyword: str, stock_code: str = None, days: int = 90) -> list:
    """Search announcements by keyword and optional stock code."""
    f = OUT_DIR / "announcements.json"
    if not f.exists():
        return []
    with open(f, "r", encoding="utf-8") as fh:
        announcements = json.load(fh)

    cutoff = (date.today() - __import__("datetime").timedelta(days=days)).isoformat()
    kw = keyword.lower()
    results = []
    for a in announcements:
        if a["date"] < cutoff:
            continue
        if stock_code and a["stock_code"] != stock_code:
            continue
        text = (a["title"] + " " + a["company"]).lower()
        if kw in text:
            results.append(a)
    return results


def get_announcement_keywords() -> dict:
    """Group announcements by common keywords."""
    f = OUT_DIR / "announcements.json"
    if not f.exists():
        return {}
    with open(f, "r", encoding="utf-8") as fh:
        announcements = json.load(fh)

    keywords = {
        "供股": "供股", "rights issue": "供股",
        "配售": "配售", "placing": "配售",
        "回購": "回購", "buy-back": "回購", "buyback": "回購",
        "合併": "合併", "merger": "合併",
        "收購": "收購", "acquisition": "收購",
        "重組": "重組", "restructur": "重組",
        "停牌": "停牌", "suspension": "停牌",
        "復牌": "復牌", "resumption": "復牌",
        "除牌": "除牌", "delisting": "除牌",
        "更名": "更名", "change of name": "更名",
        "派息": "派息", "dividend": "派息",
    }
    grouped = defaultdict(list)
    for a in announcements:
        text = (a["title"] + " " + a["company"]).lower()
        for kw, cat in keywords.items():
            if kw in text:
                grouped[cat].append(a)
                break

    out = OUT_DIR / "announcement_keywords.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in grouped.items()}, fh, ensure_ascii=False, indent=2)
    print(f"[announcement_keywords] {sum(len(v) for v in grouped.values())} categorized → {out}")
    return dict(grouped)


# ── Company Registry (CR) ──────────────────────────────────

def load_cr_data() -> dict:
    """Load CR weekly files: F = carrying business, L = new incorporations."""
    cr_dir = DATA_ROOT / "CR"
    if not cr_dir.exists():
        return {}

    f_files, l_files = {}, {}
    for f in sorted(cr_dir.glob("*.csv")):
        recs = []
        try:
            with open(f, "r", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    row = {k.strip(): v.strip() for k, v in row.items() if v is not None}
                    recs.append(row)
        except Exception:
            continue
        if f.name.startswith("F"):
            f_files[f.name] = recs
        else:
            l_files[f.name] = recs

    all_records = {"F": f_files, "L": l_files}
    out = OUT_DIR / "cr_data.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(all_records, fh, ensure_ascii=False, indent=2)

    total = sum(len(v) for v in f_files.values()) + sum(len(v) for v in l_files.values())
    print(f"[cr_data] F:{sum(len(v) for v in f_files.values())} L:{sum(len(v) for v in l_files.values())} total:{total} → {out}")
    return all_records


def get_cr_signals() -> list:
    """Detect new incorporations and name changes (shell company signals)."""
    f = OUT_DIR / "cr_data.json"
    if not f.exists():
        return []
    with open(f, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    signals = []
    for fname, recs in data.get("L", {}).items():
        for r in recs:
            date_str = r.get("Date of Incorporation / Re-domiciliation Date", "")
            name = r.get("Current Company Name in English", "")
            br = r.get("BR Number", "")
            if date_str and name:
                signals.append({
                    "date": date_str,
                    "name": name,
                    "br": br,
                    "type": "new_incorporation",
                    "source_file": fname,
                })

    for fname, recs in data.get("F", {}).items():
        for r in recs:
            change_date = r.get("Date of Change of name", "")
            if change_date:
                signals.append({
                    "date": change_date,
                    "name": r.get("Current Corporate Name / Other Corporate Name", ""),
                    "br": r.get("BR Number", ""),
                    "type": "name_change",
                    "source_file": fname,
                })

    signals.sort(key=lambda x: x["date"], reverse=True)
    out = OUT_DIR / "cr_signals.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(signals[:500], fh, ensure_ascii=False, indent=2)
    print(f"[cr_signals] {len(signals)} signals → {out}")
    return signals


# ── Quotes ──────────────────────────────────────────────────

def _run_sync(script: str, label: str) -> bool:
    """跑 sync_*.py（權威來源），失敗時回 False 讓 caller 用舊 parser 兜底。"""
    path = BASE_DIR / script
    if not path.exists():
        print(f"[{label}] {script} 唔存在，改用舊 parser")
        return False
    proc = subprocess.run([sys.executable, str(path)],
                          cwd=str(BASE_DIR), capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        print(f"[{label}] {script} 失敗 (rc={proc.returncode})，改用舊 parser")
        return False
    return True


def _read_out(name: str):
    f = OUT_DIR / name
    if f.exists():
        with open(f, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return None


def load_quotes() -> dict:
    """報價：優先用 sync_quotes_cache.py（HTM 報價段 + backend parquet 補日）。"""
    if _run_sync("sync_quotes_cache.py", "quotes"):
        data = _read_out("quotes.json")
        if data is not None:
            return data
    return load_quotes_legacy()


def load_quotes_legacy() -> dict:
    """Parse daily quotation HTM files → stock price data."""
    q_dir = DATA_ROOT / "quotes"
    if not q_dir.exists():
        return {}

    quotes = {}
    files = list(q_dir.glob("*.htm"))
    for web_dir in sorted(q_dir.glob("*_web")):
        if web_dir.is_dir():
            files.extend(web_dir.glob("*.htm"))
    for f in sorted(files):
        date_str = f.stem.replace("d", "").replace("e", "")
        try:
            year = int(date_str[:2]) + (2000 if int(date_str[:2]) < 50 else 1900)
            dt = f"{year}-{date_str[2:4]}-{date_str[4:]}"
        except:
            continue

        raw = f.read_text(encoding="utf-8", errors="ignore")
        pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", raw, re.DOTALL)
        if not pre_match:
            continue
        lines = pre_match.group(1).strip().split("\n")
        stocks = {}
        for line in lines:
            m = re.match(r"^\s*(\d{5})\s+(.*?)\s+([\d.,]+)\s*$", line)
            if m:
                code = m.group(1).strip()
                name = m.group(2).strip()
                close_str = m.group(3).replace(",", "").strip()
                try:
                    close = float(close_str)
                except:
                    continue
                stocks[code] = {"name": name, "close": close}
        if stocks:
            quotes[dt] = stocks

    out = OUT_DIR / "quotes.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(quotes, fh, ensure_ascii=False, indent=2)
    print(f"[quotes] {len(quotes)} days → {out}")
    return quotes


# ── Buyback List ────────────────────────────────────────────

def load_buyback_list() -> dict:
    """Load GEM + MainBoard buyback lists."""
    bb_dir = DATA_ROOT / "BuybackList"
    if not bb_dir.exists():
        return {}
    result = {}
    for market in ["MainBoard", "GEM"]:
        mdir = bb_dir / market
        if mdir.exists():
            files = list(mdir.glob("*"))
            if files:
                try:
                    raw = files[0].read_text(encoding="utf-8", errors="ignore")
                    result[market] = {"files": [f.name for f in files], "content_preview": raw[:2000]}
                except:
                    result[market] = {"files": [f.name for f in files]}
    out = OUT_DIR / "buyback_list.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(f"[buyback_list] {len(result)} markets → {out}")
    return result


# ── Main import ─────────────────────────────────────────────

def run_full_import():
    """Run all data imports."""
    print("=== Desktop Data Import ===")
    load_short_positions()
    get_short_analysis()
    # announcements.json / quotes.json 改由 sync_announcements_cache.py 同
    # sync_quotes_cache.py 負責（DoD 本地只到 2026-05-22，舊 parser 每日只
    # 抽到 10 個權證假代號）。呢兩個舊 loader 保留供手動修復用，
    # 但唔再喺 full import 跑，避免覆寫已同步嘅新數據。
    subprocess.run([sys.executable, str(BASE_DIR / "sync_announcements_cache.py")], check=False)
    get_announcement_keywords()
    load_cr_data()
    get_cr_signals()
    subprocess.run([sys.executable, str(BASE_DIR / "sync_quotes_cache.py")], check=False)
    load_buyback_list()
    print("=== Import complete ===")


if __name__ == "__main__":
    run_full_import()
