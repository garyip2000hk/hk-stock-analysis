"""Refresh public short-position and Companies Registry datasets without losing local history."""

import csv
import io
import json
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SFC_LATEST_CSV = (
    "https://www.sfc.hk/en/Regulatory-functions/Market/Short-position-reporting/"
    "Aggregated-reportable-short-positions-of-specified-shares/Latest-CSV"
)
CR_WEEKLY_INDEX = "https://www.cr.gov.hk/en/publication/fact-stat/statistics/registered-companies.htm"
CR_WCAG_ROOT = "https://www.cr.gov.hk/docs/wrpt/"
CR_WEEKLY_PATTERN = re.compile(r"RNC063_(\d{4}\.\d{2}\.\d{2}-\d{4}\.\d{2}\.\d{2})\.pdf")
CR_RECORD_PATTERN = re.compile(
    r"^\s*\d+\.\s+(.+?)\s+(\d{8})\s+(Nil|\d{2}-\d{2}-\d{4})\s+(Nil|\d{2}-\d{2}-\d{4})\s*$"
)


def fetch_bytes(url: str, timeout: int = 30, attempts: int = 3) -> bytes:
    """Retrieve a public resource with browser-compatible headers and bounded retries."""
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Gsmart-BoxDataSync/1.0)",
            "Accept": "text/csv,application/pdf,text/html;q=0.9,*/*;q=0.8",
        },
    )
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:  # nosec B310 -- URLs are fixed public sources
                return response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"Public source download failed after {attempts} attempts: {last_error}")


def _read_json(path: Path, fallback):
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_sfc_date(value: str) -> str:
    return datetime.strptime(value.strip(), "%d/%m/%Y").date().isoformat()


def _as_int(value: str) -> int:
    return int((value or "0").replace(",", "").strip() or 0)


def parse_sfc_short_positions(csv_text: str, source_url: str = SFC_LATEST_CSV) -> list[dict]:
    records = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        try:
            records.append(
                {
                    "date": _parse_sfc_date(row["Date"]),
                    "code": row["Stock Code"].strip().zfill(5),
                    "name": row["Stock Name"].strip(),
                    "short_shares": _as_int(row["Aggregated Reportable Short Positions (Shares)"]),
                    "short_hkd": _as_int(row["Aggregated Reportable Short Positions (HK$)"]),
                    "source_file": "SFC Latest CSV",
                    "source_url": source_url,
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return records


def sync_short_positions(output_dir: Path, fetcher=fetch_bytes) -> dict:
    """Merge the latest official SFC short-position CSV into existing history."""
    remote = parse_sfc_short_positions(fetcher(SFC_LATEST_CSV).decode("utf-8-sig"))
    if not remote:
        raise RuntimeError("SFC latest short-position CSV contained no valid records")

    output = output_dir / "short_positions.json"
    existing = _read_json(output, [])
    by_key = {(item.get("date"), item.get("code")): item for item in existing}
    added = 0
    for item in remote:
        key = (item["date"], item["code"])
        if by_key.get(key) != item:
            added += int(key not in by_key)
            by_key[key] = item

    merged = sorted(by_key.values(), key=lambda item: (item["date"], item["code"]))
    _write_json(output, merged)
    return {"status": "updated" if added else "unchanged", "added": added, "latest_date": remote[0]["date"]}


def latest_cr_weekly_url(index_html: str) -> str:
    match = CR_WEEKLY_PATTERN.search(index_html)
    if not match:
        raise RuntimeError("Companies Registry weekly WCAG report URL was not found")
    return f"{CR_WCAG_ROOT}RNC063_{match.group(1)}.pdf"


def _cr_date(value: str) -> str | None:
    if value == "Nil":
        return None
    return datetime.strptime(value, "%d-%m-%Y").date().isoformat()


def parse_cr_weekly_text(text: str, source_url: str) -> list[dict]:
    signals = []
    for line in text.splitlines():
        match = CR_RECORD_PATTERN.match(line)
        if not match:
            continue
        name, br, incorporation, name_change = match.groups()
        for signal_type, raw_date in (("new_incorporation", incorporation), ("name_change", name_change)):
            event_date = _cr_date(raw_date)
            if event_date:
                signals.append(
                    {
                        "date": event_date,
                        "name": name.strip(),
                        "br": br,
                        "type": signal_type,
                        "source_file": "CR weekly WCAG report",
                        "source_url": source_url,
                    }
                )
    return signals


def sync_cr_signals(output_dir: Path, fetcher=fetch_bytes, text_extractor=None) -> dict:
    """Merge the latest public CR weekly report into existing company-registry signals."""
    weekly_url = latest_cr_weekly_url(fetcher(CR_WEEKLY_INDEX).decode("utf-8", errors="replace"))
    pdf_bytes = fetcher(weekly_url)

    if text_extractor is None:
        if not shutil.which("pdftotext"):
            raise RuntimeError("pdftotext is required to parse the Companies Registry weekly report")
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "cr-weekly.pdf"
            text_path = Path(directory) / "cr-weekly.txt"
            pdf_path.write_bytes(pdf_bytes)
            subprocess.run(["pdftotext", "-layout", str(pdf_path), str(text_path)], check=True, capture_output=True)
            text = text_path.read_text(encoding="utf-8", errors="replace")
    else:
        text = text_extractor(pdf_bytes)

    remote = parse_cr_weekly_text(text, weekly_url)
    if not remote:
        raise RuntimeError("Companies Registry weekly report contained no parsable records")

    output = output_dir / "cr_signals.json"
    existing = _read_json(output, [])
    by_key = {(item.get("type"), item.get("date"), item.get("br")): item for item in existing}
    added = 0
    for item in remote:
        key = (item["type"], item["date"], item["br"])
        if key not in by_key:
            added += 1
        by_key[key] = item

    merged = sorted(by_key.values(), key=lambda item: (item["date"], item["type"], item["br"]), reverse=True)
    _write_json(output, merged[:5000])
    return {"status": "updated" if added else "unchanged", "added": added, "latest_date": max(item["date"] for item in remote)}
