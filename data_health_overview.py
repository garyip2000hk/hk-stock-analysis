#!/usr/bin/env python3
"""
數據健康總覽 — 每日 08:30 HKT 由「數據健康總覽」automation 跑。

Part A（數據源）＋ Part B（網站運作）一齊檢查，出錯嘅項目會先嘗試
**自動修復**（白名單內嘅安全動作：重啟服務／補抓數據），修復後再驗證。

輸出：
  1. 逐項狀態報告（stdout）
  2. 最後一行 machine-readable 判決：
       OVERVIEW_OK / OVERVIEW_WARN / OVERVIEW_ALERT
     （有項目被自動修復 → 最少 WARN，等 automation 通知用家）

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

ROWS = []  # dicts: {part, cat, name, ok, detail, repair, verify}


def add(part, category, name, ok, detail="", repair=None, verify=None):
    ROWS.append({"part": part, "cat": category, "name": name,
                 "ok": bool(ok), "detail": detail, "repair": repair, "verify": verify})


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


def _run(cmd, timeout, shell=False):
    """跑一條修復指令，回 (成功?, 短註解)。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=str(SA), shell=shell)
        tail = (p.stdout or "")[-200:].strip() or (p.stderr or "")[-200:].strip()
        return p.returncode == 0, tail.replace("\n", " ")[:180]
    except subprocess.TimeoutExpired:
        return False, f"修復指令超時（{timeout}s）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ── 自動修復動作（白名單：全部係安全、可重入嘅補抓／重啟） ──

def repair_kline():
    if not opend_alive():
        return False, "OpenD 死咗，無法補抓 K線"
    return _run([PY, "futu_data_importer.py", "--kline-only"], 900)


def repair_snapshot():
    if not opend_alive():
        return False, "OpenD 死咗，無法補抓快照"
    return _run([PY, "futu_data_importer.py", "--snapshot-only"], 900)


def repair_iv():
    return _run([PY, "options_scraper.py", "--backfill", "4"], 600)


def repair_quotes():
    return _run([PY, "sync_quotes_cache.py"], 600)


def repair_announcements():
    return _run([PY, "sync_announcements_cache.py"], 600)


def repair_short():
    code = (
        "from pathlib import Path\n"
        "from external_data_sync import sync_short_positions\n"
        "r = sync_short_positions(Path('imported'))\n"
        "print('SYNC=' + str(r))\n"
    )
    return _run([PY, "-c", code], 300)


def repair_cr():
    code = (
        "from pathlib import Path\n"
        "from external_data_sync import sync_cr_signals\n"
        "r = sync_cr_signals(Path('imported'))\n"
        "print('SYNC=' + str(r))\n"
    )
    return _run([PY, "-c", code], 300)


def repair_radar():
    cmd = ("source /root/.zo_secrets 2>/dev/null; "
           f"source {SA}/.cbbc_radar_secrets 2>/dev/null; "
           f"exec {PY} cbbc_radar_builder.py")
    return _run(["bash", "-c", cmd], 600)


def repair_strategy_lab():
    return _run([PY, str(SA / "strategy_lab.py")], 900)


def verify_strategy_lab_source():
    def _do():
        try:
            sl = json.loads((SA / "options_data/strategy_lab.json").read_text(encoding="utf-8"))
            src = (sl.get("data_freshness") or {}).get("cbbc_source", "")
            return src == "OpenD", f"來源 {src}"
        except Exception as e:
            return False, f"讀唔到: {e}"
    return _do


def restart_service(svc):
    def _do():
        return _run(["supervisorctl", "-c", SUP_CONF, "restart", svc], 120)
    return _do


# ── 重新驗證 helper ──

def verify_file(path, max_age_days):
    def _do():
        age = file_age_days(Path(path))
        if age is None:
            return False, "檔案唔存在"
        return age <= max_age_days, f"{age:.1f} 日前更新"
    return _do


def verify_dated(dirpath, prefix, ltday):
    def _do():
        date_str, _ = latest_dated(dirpath, prefix)
        if date_str is None:
            return False, "冇任何檔案"
        return date_str >= ltday.replace("-", ""), f"最新 {date_str}（應 {ltday}）"
    return _do


def verify_http(url, timeout=15):
    def _do():
        try:
            req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with request.urlopen(req, timeout=timeout) as r:
                return 200 <= r.status < 400, f"HTTP {r.status}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
    return _do


# ── 通用檢查（帶修復掛鈎） ──

def check_file(part, category, name, path, max_age_days, repair=None):
    p = Path(path)
    age = file_age_days(p)
    if age is None:
        add(part, category, name, False, "檔案唔存在", repair, verify_file(path, max_age_days))
        return
    ok = age <= max_age_days
    add(part, category, name, ok, f"{age:.1f} 日前更新",
        None if ok else repair, verify_file(path, max_age_days))


def check_dated(part, category, name, dirpath, prefix, ltday, repair=None):
    date_str, age = latest_dated(dirpath, prefix)
    v = verify_dated(dirpath, prefix, ltday)
    if date_str is None:
        add(part, category, name, False, "冇任何檔案", repair, v)
        return
    ok = date_str >= ltday.replace("-", "")
    add(part, category, name, ok, f"最新 {date_str}（應 {ltday}）", None if ok else repair, v)


def check_http(part, category, name, url, timeout=15, repair=None):
    v = verify_http(url, timeout)
    ok, detail = v()
    add(part, category, name, ok, detail, None if ok else repair, v)


def check_kline(ltday):
    kp = DB / "Futu/Kline/kline_day.parquet"

    def verify():
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
                return d >= ltday, f"最新 {d}（應 {ltday}）· {rows} 行 / {stocks} 隻"
            return False, "讀唔到"
        except Exception as e:
            return False, f"{type(e).__name__}"

    ok, detail = verify()
    add("A", "Futu K線", "kline_day.parquet", ok, detail, None if ok else repair_kline, verify)


def check_services():
    """Part B：全部 supervisor 服務；唔 RUNNING 嘅會自動重啟再驗證。"""
    def status():
        proc = subprocess.run(["supervisorctl", "-c", SUP_CONF, "status"],
                              capture_output=True, text=True, timeout=60)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        bad = [l.split()[0] for l in lines if "RUNNING" not in l]
        return lines, bad

    try:
        lines, bad = status()
    except Exception as e:
        add("B", "服務進程", "supervisorctl", False, f"{type(e).__name__}")
        return

    def verify():
        try:
            ls, bd = status()
            return not bd, (f"{len(ls)} 個服務，全部 RUNNING" if not bd
                            else f"異常: {'; '.join(bd)}")
        except Exception as e:
            return False, f"{type(e).__name__}"

    def repair():
        fixed, notes = [], []
        for svc in bad:
            ok, note = _run(["supervisorctl", "-c", SUP_CONF, "restart", svc], 120)
            notes.append(f"{svc}:{'重啟成功' if ok else '重啟失敗'}")
            if ok:
                fixed.append(svc)
        time.sleep(8)
        return len(fixed) == len(bad), "; ".join(notes)

    add("B", "服務進程", "supervisorctl", not bad,
        f"{len(lines)} 個服務，{'全部 RUNNING' if not bad else '異常: ' + '; '.join(bad)}",
        None if not bad else repair, verify)


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

    # Futu K線／快照（過期 → 自動補抓）
    check_kline(ltday)
    check_dated("A", "Futu 快照", "snapshot", str(DB / "Futu/Snapshot"), "snapshot_",
                ltday, repair=repair_snapshot)

    # CCASS（Desktop 同步，Zo 呢邊冇補抓源 → 只警報）
    check_file("A", "CCASS", "holdings.parquet", DB / "CCASS/holdings.parquet", 3)
    check_file("A", "CCASS", "dailylog.parquet", DB / "CCASS/dailylog.parquet", 3)
    check_file("A", "CCASS", "shortnames.parquet", DB / "CCASS/shortnames.parquet", 3)

    # 牛熊 / 窩輪 / 現貨 scrape 檔 — 2026-08-20 起只做後備（主源已轉 OpenD）。
    # 過期唔算異常：strategy_lab 用 OpenD，scrape 只剩外國指數補位。資訊性記錄。
    for _nm, _pfx in (("牛熊證(後備)", "cbbc_"), ("窩輪(後備)", "warrants_"), ("現貨(後備)", "spot_")):
        _ds, _age = latest_dated(str(DB / "CBBC" if _pfx == "cbbc_" else DB / ("Warrants" if _pfx == "warrants_" else "Spot")), _pfx)
        add("A", "後備數據", _nm, True, f"最新 {_ds}（後備，過期唔警報）" if _ds else "冇檔案（後備）")

    # 期權數據（IV 過期 → 自動 backfill；下游由 07:30 pipeline 更新）
    check_file("A", "期權 IV", "iv_history.parquet", SA / "options_data/iv_history.parquet",
               3, repair=repair_iv)
    check_file("A", "期權 ATM", "atm_iv_history.parquet", SA / "options_data/atm_iv_history.parquet", 7)
    check_file("A", "期權鏈", "chain_history.parquet", SA / "options_data/chain_history.parquet", 3)
    check_file("A", "策略", "strategies.json", SA / "options_data/strategies.json", 3)
    check_file("A", "業績日曆", "earnings_calendar.json", SA / "options_data/earnings_calendar.json", 7)

    # imported 快取（過期 → 自動 sync）
    check_file("A", "imported", "quotes.json", SA / "imported/quotes.json", 3, repair=repair_quotes)
    check_file("A", "imported", "announcements.json", SA / "imported/announcements.json", 3,
               repair=repair_announcements)

    # 牛熊雷達（過期 → 重跑 builder 並重新 push）
    check_file("A", "牛熊雷達", "dataset_latest.json", SA / "cbbc_radar/dataset_latest.json", 2,
               repair=repair_radar)

    # 每日報告／波幅系統（由排程 pipeline 生成 → 只警報）
    check_file("A", "每日報告", "daily_report.json", SA / "daily_report.json", 2)
    check_file("A", "波幅系統", "vol_system.json", SA / "vol_system.json", 5)
    check_file("A", "策略實驗室", "strategy_lab.json", SA / "options_data/strategy_lab.json", 3,
               repair=repair_strategy_lab)
    # strategy_lab 主源必須係 OpenD（scrape-fallback = 主流程壞咗）
    try:
        _sl = json.loads((SA / "options_data/strategy_lab.json").read_text(encoding="utf-8"))
        _src = (_sl.get("data_freshness") or {}).get("cbbc_source", "unknown")
        add("A", "策略實驗室", "牛熊主源", _src == "OpenD",
            f"來源 {_src}" + ("" if _src == "OpenD" else "（應為 OpenD，跌咗返 scrape 後備）"),
            repair_strategy_lab, verify_strategy_lab_source())
    except Exception as e:
        add("A", "策略實驗室", "牛熊主源", False, f"讀唔到: {e}",
            repair_strategy_lab, verify_strategy_lab_source())

    # 公眾來源補抓（SFC/CR 每週公佈 → 過期自動補抓）
    check_file("A", "沽空", "short_positions.json (SFC 週報)", SA / "imported/short_positions.json",
               14, repair=repair_short)
    check_file("A", "公司註冊", "cr_signals.json (CR 週報)", SA / "imported/cr_signals.json",
               10, repair=repair_cr)


# ── Part B：網站運作 ──

def part_b():
    log("── Part B：網站運作 ──")

    check_services()

    # HTTP 端點（supervisor 管嘅服務 → 自動重啟；平台／外部 → 只警報）
    check_http("B", "gsmart-box", "主站", "https://gsmart-box.manus.space/")
    check_http("B", "warrant-api", "futu-warrant-api",
               "https://futu-warrant-api-garysir.zocomputer.io/health",
               repair=restart_service("futu-warrant-api"))
    check_http("B", "hsi-strangle", "本機 API", "http://127.0.0.1:8891/signal",
               repair=restart_service("hsi-strangle-api"))
    check_http("B", "zo.space", "主頁", "https://garysir.zo.space/")
    check_http("B", "zo.space", "stock-analysis", "https://garysir.zo.space/stock-analysis")
    check_http("B", "zo.space", "auto-trading", "https://garysir.zo.space/auto-trading")
    check_http("B", "zo.space", "fintech-course", "https://garysir.zo.space/fintech-course")
    check_http("B", "zo.space", "fx-exchange", "https://garysir.zo.space/fx-exchange")


# ── 自動修復 ──

def run_repairs():
    """對所有 FAIL 而有 repair 掛鈎嘅項目跑修復，再驗證。"""
    fixed, failed = [], []
    for row in ROWS:
        if row["ok"] or not row["repair"]:
            continue
        log(f"自動修復：{row['name']}")
        try:
            success, note = row["repair"]()
        except Exception as e:
            success, note = False, f"{type(e).__name__}: {e}"
        orig_detail = row["detail"]
        if success and row.get("verify"):
            vok, vdetail = row["verify"]()
            if vok:
                row["ok"] = True
                row["detail"] = f"曾異常（{orig_detail}）→ 已自動修復"
                log(f"  ✅ {row['name']} 修復成功")
                fixed.append(row)
                continue
            row["detail"] = f"{orig_detail}｜修復跑完仍未達標（{vdetail}）"
        elif success:
            row["ok"] = True
            row["detail"] = f"曾異常（{orig_detail}）→ 已自動修復"
            fixed.append(row)
            continue
        else:
            row["detail"] = f"{orig_detail}｜自動修復失敗：{note[:140]}"
        log(f"  ❌ {row['name']} 修復失敗")
        failed.append(row)
    return fixed, failed


def _write_report(ltday, verdict, fixed, unrepaired):
    """寫 markdown 報告去 stock-analysis/reports/，供 automation 上傳 Google Drive。"""
    rep_dir = SA / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    out = rep_dir / f"data_health_{TODAY}.md"

    wd = ["一", "二", "三", "四", "五", "六", "日"][NOW.weekday()]
    status_map = {True: "🟢 正常", False: "🔴 異常"}

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
        for r in [r for r in ROWS if r["part"] == part]:
            lines.append(f"| {r['name']} | {status_map[r['ok']]} | {r['detail']} |")
        lines.append("")

    lines += ["## 自動修復記錄", ""]
    if fixed:
        lines.append(f"🔧 共 **{len(fixed)}** 項已自動修復：")
        for r in fixed:
            lines.append(f"- ✅ **{r['name']}**（{r['cat']}）— {r['detail']}")
    else:
        lines.append("今日冇需要自動修復嘅項目。")
    lines.append("")

    fails = [r for r in ROWS if not r["ok"]]
    lines += ["## 問題／Bug 摘要", ""]
    if not fails:
        if fixed:
            lines.append(f"✅ 所有檢查最終通過（其中 {len(fixed)} 項係自動修復返嚟）。")
        else:
            lines.append("✅ 全部檢查通過，冇發現問題。")
    else:
        lines.append(f"共 **{len(fails)}** 項異常（自動修復後仍未解決）：")
        for r in fails:
            lines.append(f"- 🔴 **{r['name']}**（{r['cat']}）— {r['detail']}")
    lines += ["", f"**判決**：`{verdict}`"]

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"報告已寫：{out}", flush=True)


def main():
    ltday = latest_trading_day()
    log(f"最近交易日（結算基準）: {ltday}")

    part_a(ltday)
    part_b()

    # 自動修復 FAIL 項目（白名單內）
    fixed, unrepaired = run_repairs()

    # 輸出報告
    print()
    print("=" * 60)
    for part in ("A", "B"):
        print(f"【{('數據源' if part == 'A' else '網站運作')}】")
        for r in [r for r in ROWS if r["part"] == part]:
            mark = "🟢" if r["ok"] else "🔴"
            print(f"  {mark} {r['name']} — {r['detail']}")
    print("=" * 60)

    fails = [r for r in ROWS if not r["ok"]]
    n_fail = len(fails)
    if n_fail == 0 and not fixed:
        verdict = "OVERVIEW_OK"
    elif n_fail == 0 and fixed:
        verdict = "OVERVIEW_WARN"  # 全部修返好，但要通知用家
    elif any(r["cat"] in ("Futu OpenD", "Futu K線") or (r["cat"] == "gsmart-box" and r["name"] == "主站")
             for r in fails):
        verdict = "OVERVIEW_ALERT"
    else:
        verdict = "OVERVIEW_WARN"

    n_fixed = len(fixed)
    print(f"{verdict}: 共 {len(ROWS)} 項檢查，{n_fail} 項異常未解決，{n_fixed} 項已自動修復")

    _write_report(ltday, verdict, fixed, unrepaired)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"OVERVIEW_ALERT: 總覽檢查本身出錯 — {type(e).__name__}: {e}")
        sys.exit(1)
