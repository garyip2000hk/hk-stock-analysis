"""content_feed.py — 將期權分析結果轉成「內容簡報」(content brief)。

第 5 項：財經內容自動化。呢個模組唔生成文案，只負責把
iv_analyzer / ccass_options_cross / strategy_engine 三套結果
整理成有事實、有角度、有免責前提嘅結構化簡報，交畀
fin-content-auto 嘅 Gemini 寫廣東話文案 + 出圖卡。

設計原則：
  · 只出「數字 + 事實」，唔出投資建議、唔講買賣、唔講目標價
  · 每條 brief 自帶 takeaway（限制／風險），文案一定要照寫
  · 冇料就回空 list，唔堆砌（寧可唔出帖，唔好出流水帳）

CLI:
    python3 content_feed.py                # 全部 brief（人讀格式）
    python3 content_feed.py --json         # JSON
    python3 content_feed.py --kind iv      # 只出某類
    python3 content_feed.py --write        # 寫入 options_data/content_briefs.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
OPT = BASE / "options_data"
OUT = OPT / "content_briefs.json"

HKT = timezone(timedelta(hours=8))

CATEGORY_LABEL = {
    "iv": "期權波幅",
    "flow": "資金流向",
    "strategy": "期權策略",
}

DISCLAIMER = "本內容只供資訊參考，唔構成任何投資建議。期權涉及槓桿，可能損失全部本金。"


def _today_hk() -> str:
    return datetime.now(HKT).strftime("%Y-%m-%d")


def _load(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _date_zh(d: str) -> str:
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return f"{dt.year} 年 {dt.month} 月 {dt.day} 日"
    except Exception:
        return d


def _name(r: dict) -> str:
    code = r.get("stock_code") or ""
    nm = r.get("name") or r.get("hkats") or ""
    return f"{nm}（{code}）" if nm else code


# ── 1. IV 貴／平榜 ─────────────────────────────────────────────

def brief_iv(top_n: int = 5) -> list[dict]:
    rows = _load(OPT / "iv_analysis.json")
    if not rows:
        return []

    date = rows[0].get("date") or _today_hk()
    briefs: list[dict] = []

    rich = [r for r in rows if (r.get("score") or 0) >= 3 and (r.get("volume") or 0) >= 500]
    rich.sort(key=lambda r: (-(r.get("score") or 0), -(r.get("iv_hv") or 0)))
    rich = rich[:top_n]

    cheap = [r for r in rows if (r.get("score") or 0) <= -2 and (r.get("volume") or 0) >= 500]
    cheap.sort(key=lambda r: ((r.get("score") or 0), (r.get("iv_hv") or 9)))
    cheap = cheap[:top_n]

    def _fact(r: dict) -> dict:
        return {
            "股票": _name(r),
            "收市": f"{r.get('close')}" if r.get("close") is not None else "—",
            "IV": f"{r['iv']:.0f}%" if r.get("iv") is not None else "—",
            "HV20": f"{r['hv20']:.1f}%" if r.get("hv20") is not None else "—",
            "IV/HV": f"{r['iv_hv']:.2f}" if r.get("iv_hv") is not None else "—",
            "IV Rank": f"{r['iv_rank']:.0f}" if r.get("iv_rank") is not None else "—",
            "一年百分位": f"{r['iv_pct']:.0f}%" if r.get("iv_pct") is not None else "—",
            "成交": f"{r.get('volume'):,}" if r.get("volume") else "—",
            "判斷": r.get("verdict") or "—",
        }

    if rich:
        lead = rich[0]
        briefs.append({
            "kind": "iv",
            "category": CATEGORY_LABEL["iv"],
            "date": date,
            "date_zh": _date_zh(date),
            "headline": f"{lead.get('name') or lead.get('stock_code')} 期權 IV {lead['iv']:.0f}%，係 HV20 嘅 {lead['iv_hv']:.2f} 倍",
            "angle": "IV 高過股票自己嘅實際波幅，代表市場為未知事件付溢價；歷史上呢種溢價多數會收窄，但收窄嘅時機冇人估得中。",
            "facts": [_fact(r) for r in rich],
            "takeaway": "IV/HV 高唔等於一定要沽期權——高 IV 通常有原因（業績、傳聞、監管）。IV 係 HKEX 官方公布嘅 ATM 隱含波幅，唔包含 skew。",
            "disclaimer": DISCLAIMER,
        })

    if cheap:
        lead = cheap[0]
        briefs.append({
            "kind": "iv",
            "category": CATEGORY_LABEL["iv"],
            "date": date,
            "date_zh": _date_zh(date),
            "headline": f"{lead.get('name') or lead.get('stock_code')} 期權 IV 只有 {lead['iv']:.0f}%，低過實際波幅",
            "angle": "IV 低過 HV20，意思係期權價格假設隻股票會靜落嚟，但佢近月實際上冇咁靜。買方成本相對便宜。",
            "facts": [_fact(r) for r in cheap],
            "takeaway": "IV 低亦可能單純因為冇人炒、流動性差，買賣差價會蠶蝕回報。低 IV ≠ 平，要睇實際買賣價位。",
            "disclaimer": DISCLAIMER,
        })

    return briefs


# ── 2. CCASS × 期權資金流共振 ─────────────────────────────────

def brief_flow(top_n: int = 5) -> list[dict]:
    rows = _load(OPT / "ccass_options_cross.json")
    if not rows:
        return []

    date = rows[0].get("date") or _today_hk()
    hot = [r for r in rows if (r.get("score") or 0) >= 4]
    hot.sort(key=lambda r: -(r.get("score") or 0))
    hot = hot[:top_n]
    if not hot:
        return []

    facts = []
    for r in hot:
        facts.append({
            "股票": _name(r),
            "收市": f"{r.get('close')}" if r.get("close") is not None else "—",
            "訊號": "、".join(r.get("signals") or []) or "—",
            "傾向": r.get("bias") or "—",
            "前十大 5 日變化": f"{r['d_c10_5d']:+.2f} 個百分點" if r.get("d_c10_5d") is not None else "—",
            "Call 成交 z": f"{r['call_vol_z']:.1f}" if r.get("call_vol_z") is not None else "—",
            "Put 成交 z": f"{r['put_vol_z']:.1f}" if r.get("put_vol_z") is not None else "—",
            "Call 未平倉 5 日": f"{r['d_call_oi_5d']:+.1f}%" if r.get("d_call_oi_5d") is not None else "—",
            "IV Rank": f"{r['iv_rank']:.0f}" if r.get("iv_rank") is not None else "—",
        })

    lead = hot[0]
    return [{
        "kind": "flow",
        "category": CATEGORY_LABEL["flow"],
        "date": date,
        "date_zh": _date_zh(date),
        "headline": f"{lead.get('name') or lead.get('stock_code')}：CCASS 歸邊 + 期權大單同時出現",
        "angle": "單看 CCASS 只知券商層面有人收貨，單看期權只知有大單；兩邊同時異動，方向性嘅參考價值高好多。最有睇頭嘅係「已經歸邊但 IV 仍然低」——即係期權市場未反應。",
        "facts": facts,
        "takeaway": "CCASS 集中度只反映券商倉位，唔等於實益擁有人；期權大單亦可能係造市商對沖而唔係方向性下注。CCASS 一般比期權報告遲一日。",
        "disclaimer": DISCLAIMER,
    }]


# ── 3. 事件驅動期權策略 ───────────────────────────────────────

def _event_of(r: dict) -> dict:
    evs = r.get("events") or []
    return evs[0] if evs else {}


def _still_ahead(r: dict) -> bool:
    """業績類事件一定要未發生；財技公告類（回溯近 10 日）照用。"""
    ev = _event_of(r)
    if ev.get("kind") != "results":
        return True
    ev_date, as_of = ev.get("date"), r.get("date")
    if not ev_date or not as_of:
        return False
    return ev_date > as_of


def brief_strategy(top_n: int = 4) -> list[dict]:
    rows = _load(OPT / "strategies.json")
    if not rows:
        return []
    if isinstance(rows, dict):
        rows = rows.get("strategies") or []

    good = [r for r in rows if (r.get("ev_hkd") or 0) > 0 and _still_ahead(r)]
    good.sort(key=lambda r: -(r.get("ev_hkd") or 0))
    good = good[:top_n]
    if not good:
        return []

    date = good[0].get("date") or _today_hk()

    facts = []
    for r in good:
        legs = r.get("legs") or []
        strikes = "／".join(
            f"{l.get('action')}{l.get('type')} {l.get('strike'):g}"
            for l in legs
            if l.get("strike") is not None
        )
        ev = _event_of(r)
        facts.append({
            "股票": _name(r),
            "事件": ev.get("label") or "—",
            "事件日": ev.get("date") or "—",
            "策略": r.get("strategy") or "—",
            "到期日": r.get("expiry") or "—",
            "腳位": strikes or "—",
            "IV／HV20": (
                f"{r['iv']:.0f}%／{r['hv20']:.1f}%"
                if r.get("iv") is not None and r.get("hv20") is not None else "—"
            ),
            "勝率": f"{r['win_rate']:.1f}%" if r.get("win_rate") is not None else "—",
            "最大利潤": f"HK${r['max_profit']:,.0f}" if r.get("max_profit") is not None else "—",
            "最大虧損": f"HK${r['max_loss']:,.0f}" if r.get("max_loss") is not None else "—",
            "期望值": f"HK${r['ev_hkd']:+,.0f}" if r.get("ev_hkd") is not None else "—",
        })

    lead = good[0]
    lead_ev = _event_of(lead)
    wr = lead.get("win_rate") or 0
    return [{
        "kind": "strategy",
        "category": CATEGORY_LABEL["strategy"],
        "date": date,
        "date_zh": _date_zh(date),
        "headline": f"{lead.get('name') or lead.get('stock_code')} {lead_ev.get('label') or '事件'}前夕：{lead.get('strategy') or '期權組合'} 勝率 {wr:.0f}%",
        "angle": "先用業績日曆同財技公告搵催化劑，再夾埋 IV 貴／平，就可以反推邊種期權組合最有數計，同埋應該揀邊個行使價。",
        "facts": facts,
        "takeaway": "勝率係用 HV20 做對數常態統計模擬，唔係預測；結算價唔等於可成交價，遠價外合約買賣差價會蠶蝕回報，亦未計佣金同印花稅。",
        "disclaimer": DISCLAIMER,
    }]


# ── 匯總 ──────────────────────────────────────────────────────

def build(kinds: list[str] | None = None) -> list[dict]:
    kinds = kinds or ["strategy", "flow", "iv"]
    out: list[dict] = []
    for k in kinds:
        if k == "iv":
            out += brief_iv()
        elif k == "flow":
            out += brief_flow()
        elif k == "strategy":
            out += brief_strategy()
    return out


def _render(b: dict) -> str:
    lines = [
        "═" * 62,
        f"【{b['category']}】{b['date_zh']}",
        f"標題：{b['headline']}",
        "",
        f"角度：{b['angle']}",
        "",
        "事實：",
    ]
    for f in b["facts"]:
        parts = [f"{k} {v}" for k, v in f.items()]
        lines.append("  · " + "｜".join(parts))
    lines += ["", f"限制：{b['takeaway']}", f"免責：{b['disclaimer']}", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--kind", choices=["iv", "flow", "strategy"], action="append")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    briefs = build(args.kind)

    if args.write:
        OPT.mkdir(parents=True, exist_ok=True)
        OUT.write_text(
            json.dumps(
                {"generated_at": datetime.now(HKT).isoformat(), "briefs": briefs},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"已寫入 {OUT}（{len(briefs)} 條 brief）")
        if not args.json:
            return

    if args.json:
        print(json.dumps(briefs, ensure_ascii=False, indent=2))
        return

    if not briefs:
        print("今日冇足夠數據出 brief。")
        return
    for b in briefs:
        print(_render(b))
    print(f"共 {len(briefs)} 條 brief")


if __name__ == "__main__":
    main()
