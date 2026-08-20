#!/usr/bin/env python3
"""
數據健康總覽 — 每日 08:30 HKT 由「數據健康總覽」automation 跑。

Part A（數據源）＋ Part B（網站運作）一齊檢查，輸出：
  1. 逐項狀態報告（stdout）
  2. 最後一行 machine-readable 判決：
       OVERVIEW_OK / OVERVIEW_WARN / OVERVIEW_ALERT

純 filesystem + HTTP + 服務檢查，唔燒 AI 額度。
"""
import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request

HKT = timezone(timedelta(hours=8))
NOW = datetime.now(HKT)
TODAY = NOW.strftime("%Y-%m-%d")
BASE = Path("/home/workspace")
SA = BASE / "stock-analysis"
DB = BASE / "Desktop/db"
PY = "/usr/local/bin/python3"
SUP_CONF = "/etc/zo/supervisord-user.conf"

ROWS = []


def add(part, category, name, ok, detail=""):
    ROWS.append((part, category, name, "OK" if ok else "FAIL", detail))


def log(msg):
    print(f"[{NOW:%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def opend_alive(timeout=4):
    try:
        with socket.create_connection(("127.0.0.1", 11111), timeout=timeout):
            return True
    except Exception:
        return False


def restart_opend():
    try:
        subprocess.run(["supervisorctl", "-c", SUP_CONF, "restart", "futu-opend"],
                       capture_output=True, text=True, timeout=120)
    except Exception:
        return False
    for _ in range(12):
        time.sleep(5)
        if opend_alive():
            return True
    return False


def latest_trading_day():
    code = (
        "import json,datetime\n"
        "from futu import OpenQuoteContext\n"
        "q=OpenQuoteContext(host='127.0.0.1',port=11111)\n"
        "today=datetime.date.today()\n"
        "start=(today-datetime.timedelta(days=20)).isoformat()\n"
        "r,d=q.request_trading_days(market='HK',start=start,end=today.isoformat())\n"
        "q.close()\n"
        "days=[x['time'] for x in d] if r==0 else []\n"
        "print('TD='+json.dumps(days))\n"
    )
    try:
        proc = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=120)
        line = [l for l in proc.stdout.splitlines() if l.startswith("TD=")]
        days = json.loads(line[0][3:]) if line else []
        today = datetime.now(HKT).strftime("%Y-%m-%d")
        if days:
            closed = [d for d in days if d < today]
            return closed[-1] if closed else days[-1]
    except Exception:
        pass
    d = NOW
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def file_age_days(p):
    if not p.exists():
        return None
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=HKT)
    return (NOW - mtime).total_seconds() / 86400


def latest_dated(pattern_dir, prefix):
    files = sorted(Path(pattern_dir).glob(f"{prefix}*"))
    if not files:
        return None, None
    latest = files[-1]
    date_str = latest.stem[len(prefix):]
    return date_str, file_age_days(latest)


def check_file(part, category, name, path, max_age_days):
    p = Path(path)
    age = file_age_days(p)
    if age is None:
        add(part, category, name, False, "檔案唔存在")
        return
    ok = age <= max_age_days
    add(part, category, name, ok, f"{age:.1f} 日前更新")


def check_dated(part, category, name, dirpath, prefix, ltday):
    date_str, age = latest_dated(dirpath, prefix)
    if date_str is None:
        add(part, category, name, False, "冇任何檔案")
        return
    ok = date_str >= ltday.replace("-", "")
    add(part, category, name, ok, f"最新 {date_str}（應 {ltday}）")


def check_http(part, category, name, url, timeout=15):
    try:
        req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with request.urlopen(req, timeout=timeout) as r:
            add(part, category, name, 200 <= r.status < 400, f"HTTP {r.status}")
    except Exception as e:
        add(part, category, name, False, f"{type(e).__name__}: {e}")


# ── Part A：數據源 ──
def part_a(ltday):
    log("── Part A：數據源 ──")

    # Futu OpenD
    if not opend_alive():
        if restart_opend():
            add("A", "Futu OpenD", "OpenD 服務", True, "死咗，已自動重開成功")
        else:
            add("A", "Futu OpenD", "OpenD 服務", False, "死咗，自動重開失敗")
    else:
        add("A", "Futu OpenD", "OpenD 服務", True, "127.0.0.1:11111 正常")

    # Futu K線
    kp = DB / "Futu/Kline/kline_day.parquet"
    code = (
        "import pandas as pd\n"
        f"df=pd.read_parquet(r'{kp}')\n"
        "print('K='+str(df['time_key'].max())[:10]+'|'+str(len(df))+'|'+str(df['code'].nunique()))\n"
    )
    try:
        proc = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=180)
        line = [l for l in proc.stdout.splitlines() if l.startswith("K=")]
        if line:
            d, rows, stocks = line[0][2:].split("|")
            add("A", "Futu K線", "kline_day.parquet", d >= ltday,
                f"最新 {d}（應 {ltday}）· {rows} 行 / {stocks} 隻")
        else:
            add("A", "Futu K線", "kline_day.parquet", False, "讀唔到")
    except Exception as e:
        add("A", "Futu K線", "kline_day.parquet", False, f"{type(e).__name__}")

    # Futu 快照
    check_dated("A", "Futu 快照", "snapshot", str(DB / "Futu/Snapshot"), "snapshot_", ltday)

    # CCASS
    check_file("A", "CCASS", "holdings.parquet", DB / "CCASS/holdings.parquet", 3)
    check_file("A", "CCASS", "dailylog.parquet", DB / "CCASS/dailylog.parquet", 3)
    check_file("A", "CCASS", "shortnames.parquet", DB / "CCASS/shortnames.parquet", 3)

    # 牛熊 / 窩輪 / 現貨
    check_dated("A", "牛熊證", "cbbc", str(DB / "CBBC"), "cbbc_", ltday)
    check_dated("A", "窩輪", "warrants", str(DB / "Warrants"), "warrants_", ltday)
    check_dated("A", "現貨", "spot", str(DB / "Spot"), "spot_", ltday)

    # 期權數據
    check_file("A", "期權 IV", "iv_history.parquet", SA / "options_data/iv_history.parquet", 3)
    check_file("A", "期權 ATM", "atm_iv_history.parquet", SA / "options_data/atm_iv_history.parquet", 7)
    check_file("A", "期權鏈", "chain_history.parquet", SA / "options_data/chain_history.parquet", 3)
    check_file("A", "策略", "strategies.json", SA / "options_data/strategies.json", 3)
    check_file("A", "業績日曆", "earnings_calendar.json", SA / "options_data/earnings_calendar.json", 7)

    # imported 快取
    check_file("A", "imported", "quotes.json", SA / "imported/quotes.json", 3)
    check_file("A", "imported", "announcements.json", SA / "imported/announcements.json", 3)

    # 牛熊雷達
    check_file("A", "牛熊雷達", "dataset_latest.json", SA / "cbbc_radar/dataset_latest.json", 2)

    # 每日報告／波幅系統
    check_file("A", "每日報告", "daily_report.json", SA / "daily_report.json", 2)
    check_file("A", "波幅系統", "vol_system.json", SA / "vol_system.json", 5)
    check_file("A", "策略實驗室", "strategy_lab.json", SA / "strategy_lab.json", 3)

    # 公眾來源補抓（取代停咗嘅 Desktop 刮取；SFC/CR 每週公佈）
    check_file("A", "沽空", "short_positions.json (SFC 週報)", SA / "imported/short_positions.json", 14)
    check_file("A", "公司註冊", "cr_signals.json (CR 週報)", SA / "imported/cr_signals.json", 10)


# ── Part B：網站運作 ──
def part_b():
    log("── Part B：網站運作 ──")

    # 服務進程
    try:
        proc = subprocess.run(["supervisorctl", "-c", SUP_CONF, "status"],
                              capture_output=True, text=True, timeout=60)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        bad = [l for l in lines if "RUNNING" not in l]
        add("B", "服務進程", "supervisorctl", not bad,
            f"{len(lines)} 個服務，{'全部 RUNNING' if not bad else '異常: ' + '; '.join(bad)}")
    except Exception as e:
        add("B", "服務進程", "supervisorctl", False, f"{type(e).__name__}")

    # HTTP 端點
    check_http("B", "gsmart-box", "主站", "https://gsmart-box.manus.space/")
    check_http("B", "warrant-api", "futu-warrant-api", "https://futu-warrant-api-garysir.zocomputer.io/health")
    check_http("B", "hsi-strangle", "本機 API", "http://127.0.0.1:8891/signal")
    check_http("B", "zo.space", "主頁", "https://garysir.zo.space/")
    check_http("B", "zo.space", "stock-analysis", "https://garysir.zo.space/stock-analysis")
    check_http("B", "zo.space", "auto-trading", "https://garysir.zo.space/auto-trading")
    check_http("B", "zo.space", "fintech-course", "https://garysir.zo.space/fintech-course")
    check_http("B", "zo.space", "fx-exchange", "https://garysir.zo.space/fx-exchange")


def _write_report(ltday, verdict, n_fail):
    """寫 markdown 報告去 stock-analysis/reports/，供 automation 上傳 Google Drive。"""
    rep_dir = SA / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    out = rep_dir / f"data_health_{TODAY}.md"

    wd = ["一", "二", "三", "四", "五", "六", "日"][NOW.weekday()]
    status_map = {"OK": "🟢 正常", "FAIL": "🔴 異常"}

    lines = [
        "# 數據健康總覽報告",
        "",
        f"**日期**：{TODAY}（{wd}）",
        f"**檢查時間**：{NOW:%H:%M} HKT",
        f"**最近交易日（結算基準）**：{ltday}",
        "",
    ]
    for part, title in (("A", "數據源"), ("B", "網站運作")):
        lines += [f"## Part {part} — {title}", "", "| 項目 | 狀態 | 詳情 |", "|---|---|---|"]
        for _, _, name, status, detail in [r for r in ROWS if r[0] == part]:
            lines.append(f"| {name} | {status_map[status]} | {detail} |")
        lines.append("")

    fails = [r for r in ROWS if r[3] == "FAIL"]
    lines += ["## 問題／Bug 摘要", ""]
    if not fails:
        lines.append("✅ 全部檢查通過，冇發現問題。")
    else:
        lines.append(f"共 **{len(fails)}** 項異常：")
        for _, cat, name, _, detail in fails:
            lines.append(f"- 🔴 **{name}**（{cat}）— {detail}")
    lines += ["", f"**判決**：`{verdict}`"]

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"報告已寫：{out}", flush=True)


def main():
    ltday = latest_trading_day()
    log(f"最近交易日（結算基準）: {ltday}")

    part_a(ltday)
    part_b()

    # 輸出報告
    print()
    print("=" * 60)
    fails = [r for r in ROWS if r[3] == "FAIL"]
    for part in ("A", "B"):
        print(f"【{('數據源' if part == 'A' else '網站運作')}】")
        for _, _, name, status, detail in [r for r in ROWS if r[0] == part]:
            mark = "🟢" if status == "OK" else "🔴"
            print(f"  {mark} {name} — {detail}")
    print("=" * 60)

    n_fail = len(fails)
    if n_fail == 0:
        verdict = "OVERVIEW_OK"
    elif any("OpenD" in r[1] or "K線" in r[1] or "主站" in r[2] for r in fails):
        verdict = "OVERVIEW_ALERT"
    else:
        verdict = "OVERVIEW_WARN"

    print(f"{verdict}: 共 {len(ROWS)} 項檢查，{n_fail} 項異常")

    _write_report(ltday, verdict, n_fail)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"OVERVIEW_ALERT: 總覽檢查本身出錯 — {type(e).__name__}: {e}")
        sys.exit(1)
