#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export a markdown evidence brief for offline LLM analysis (P3).

Reads data/market-card.json (nothing else) and writes data/brief-<date>.md:
a compact digest of the day's key numbers plus the exact JSON schemas the
LLM is expected to produce (events / mainline rewrite). Every figure in the
brief comes from the card, so an LLM anchored on it can pass the fact
checkers (validate_events / apply_mainline_rewrite) without inventing data.

No network. Run AFTER build_data.py:
    python -X utf8 scripts/build_brief.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "data" / "market-card.json"
OUT_DIR = ROOT / "data"

EVENT_SCHEMA_HINT = """```json
{
  "events": [
    {
      "title": "标题（不超过 20 字）",
      "level": "关注 | 观察 | 风险",
      "direction": "正向 | 负向 | 中性",
      "summary": "发生了什么（2-3 句，引用上方素材中的数字）",
      "transmission": "传导链：A → B → C（箭头短语）",
      "evidence": "必须包含上方素材（第 1-8 节）里的具体数值，逐个可核对",
      "risk": "反证/失效条件（1 句）"
    }
  ]
}
```
硬性要求：每张卡 7 个字段全部非空；evidence 中的每个数字必须能在上方素材中找到
（校验器逐数核对，对不上会被拒）。建议 3-6 张卡，宁缺毋滥。"""

MAINLINE_SCHEMA_HINT = """```json
{"conclusion": "一句话主线判定，30-60 字"}
```
硬性要求：结论必须命中下方素材中至少 2 个关键事实（板块名、涨停梯队、资金数字等，
校验器按关键 token 匹配）。只改写措辞，不得引入素材中没有的方向或数字。"""


def fmt_pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.2f}%"


def fmt_yi(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.1f}亿"


def render(card: dict[str, Any]) -> str:
    date = card["meta"]["market_date"]
    status = card.get("status") or {}
    mood = (status.get("emotion") or {})
    latest = (card.get("market_days") or [{}])[-1]
    lines: list[str] = []

    lines.append(f"# 决策卡素材包 · {date}")
    lines.append("")
    lines.append(f"> 供离线 LLM 填写 `data/events-{date}.json` 与 `data/mainline-{date}.json`。")
    lines.append(f"> 本包所有数字来自当日快照，是唯一允许引用的数字来源。")
    lines.append(f"> 生成时间：{card['meta'].get('updated_at', '')}；情绪 {mood.get('label')}（{mood.get('score')}/5）。")
    lines.append("")

    # ---- market snapshot
    lines.append("## 1. 市场概览")
    b = card.get("breadth") or {}
    lines.append(f"- 上证/创业板/科创50：{fmt_pct(latest.get('shanghai'))} / {fmt_pct(latest.get('chinext'))} / {fmt_pct(latest.get('star50'))}"
                 f"；成交 {latest.get('turnover')} 万亿；盘面特征「{latest.get('feature')}」")
    lines.append(f"- 广度：涨 {b.get('up')} / 跌 {b.get('down')} / 平 {b.get('flat')}"
                 f"；涨停 {b.get('limit_up')} / 跌停 {b.get('limit_down')}"
                 f"；5日新高 {b.get('new_high') if b.get('new_high') is not None else '—'} / 新低 {b.get('new_low') if b.get('new_low') is not None else '—'}")
    style = card.get("style") or {}
    if style.get("note"):
        lines.append(f"- 风格：{style['note']}")
    val = card.get("valuation") or []
    if val:
        vtxt = "；".join(f"{r.get('name')} PE {r.get('pe')}（{r.get('window_years')}年 {r.get('percentile')}%分位）" for r in val)
        lines.append(f"- 估值：{vtxt}")
    margin = card.get("margin") or {}
    if margin.get("balance") is not None:
        lines.append(f"- 两融：余额 {margin.get('balance')} 亿（{margin.get('as_of')}，T+1 披露），环比 {margin.get('change'):+.1f} 亿")
    etf = card.get("etf_shares") or {}
    if etf.get("bootstrap"):
        lines.append("- 宽基 ETF 份额申赎：首次采集，变动自次一交易日起可见")
    elif etf.get("by_index"):
        etxt = "；".join(f"{r['index']} {r['delta']:+.2f} 亿份（{r['direction']}）" for r in etf["by_index"][:6])
        lines.append(f"- 宽基 ETF 份额申赎：{etxt}")
        if etf.get("top_funds"):
            movers = "、".join(f"{r['name']} {r['delta']:+.2f}" for r in etf["top_funds"][:5])
            lines.append(f"- 单基金变动居前：{movers}")
    lines.append("")

    # ---- mainline (rule version, as anchor)
    ml = card.get("mainline") or {}
    if ml:
        lines.append("## 2. 规则版主线判定（改写基准）")
        lines.append(f"- 结论：{ml.get('conclusion')}")
        dims = ml.get("dimensions") or {}
        if dims:
            for k, v in dims.items():
                if isinstance(v, dict):
                    lines.append(f"- {k}：{v.get('text') or v.get('value')}")
                else:
                    lines.append(f"- {k}：{v}")
        lines.append("")

    # ---- flows
    lines.append("## 3. 行业资金（主力净流入，去重后；单位亿元）")
    lines.append("| 板块 | 当日 | 5日 | 10日 | 分类 | 涨停 |")
    lines.append("|---|---|---|---|---|---|")
    for row in (card.get("flows") or [])[:20]:
        lines.append(f"| {row['sector']} | {fmt_yi(row.get('today'))} | {fmt_yi(row.get('day5'))} "
                     f"| {fmt_yi(row.get('day10'))} | {row.get('classification', '—')} | {row.get('limit_up', 0)} |")
    lines.append("")
    pool = card.get("accumulation_pool") or []
    if pool:
        lines.append(f"- 蓄力观察池（前 10）：{'、'.join(p['sector'] for p in pool)}")
        lines.append("")

    # ---- ladder
    ladder = card.get("limit_ladder") or {}
    metrics = ladder.get("metrics") or {}
    rows = ladder.get("ladder") or []
    if rows:
        lines.append("## 4. 涨停梯队与情绪指标")
        mtxt = (f"涨停 {metrics.get('limit_up')} / 跌停 {metrics.get('limit_down')}"
                f"；炸板 {metrics.get('zha_ban')}（封板率 {metrics.get('seal_rate')}%）"
                f"；晋级率 {metrics.get('promotion_rate')}%"
                f"；最高 {metrics.get('max_board')} 板")
        lines.append(f"- {mtxt}")
        lines.append("| 板数 | 个股 | 行业 | 备注 |")
        lines.append("|---|---|---|---|")
        for r in rows[:12]:
            lines.append(f"| {r['board']} | {r['stock']} | {r['sector']} | {r.get('note', '')} |")
        dist = ladder.get("distribution") or {}
        if dist:
            lines.append(f"- 连板分布：{json.dumps(dist, ensure_ascii=False)}")
        stage = ((card.get("forecast") or {}).get("branches") or [])
        lines.append("")

    # ---- seats
    dt = card.get("dragon_tiger") or {}
    if dt.get("direction"):
        lines.append("## 5. 龙虎榜方向级聚合")
        lines.append(f"- 数据 {dt.get('as_of')}，{dt.get('record_count')} 条记录 / {dt.get('seat_count')} 席位")
        lines.append("| 方向 | 净买(万) | 个股数 | 代表个股 |")
        lines.append("|---|---|---|---|")
        for r in dt.get("direction", [])[:8]:
            top = (r.get("top") or {}).get("name", "")
            lines.append(f"| {r['sector']} | {r.get('net_wan', 0):+.0f} | {r.get('count')} | {top} |")
        lines.append("")

    # ---- rotation & noise
    rot = card.get("rotation") or {}
    rot_rows = rot.get("rows") or []
    if rot_rows:
        lines.append("## 6. 轮动兑现（昨日领涨 → 今日）")
        for r in rot_rows[:6]:
            lines.append(f"- {r['sector']}：{r.get('symbol')}（{r.get('note', '')}）")
        lines.append("")
    noise = card.get("noise") or []
    if noise:
        lines.append(f"- 噪音区（无催化散涨）：{'、'.join(n.get('sector', '') for n in noise[:8])}")
        lines.append("")

    # ---- divergence & global
    div = card.get("divergence") or []
    if div:
        lines.append("## 7. 跨市场背离 / 全球市场")
        for r in div:
            lines.append(f"- {r['theme']}：{r['sector']} {fmt_pct(r.get('sector_pct'))} vs "
                         f"{r['global_name']} {fmt_pct(r.get('global_pct'))}（{r.get('note', '')}）")
    gm = card.get("global_markets") or []
    if gm:
        gtxt = "；".join(f"{r.get('name')} {fmt_pct(r.get('pct'))}" for r in gm[:10])
        lines.append(f"- 全球行情（{card.get('global_as_of') or ''}）：{gtxt}")
    ust = card.get("us_treasury") or {}
    if ust.get("y10") is not None:
        lines.append(f"- 美债10Y {ust.get('y10')}%（{ust.get('as_of', '')}）")
    lines.append("")

    # ---- calendar
    cal = card.get("calendar") or []
    if cal:
        lines.append("## 8. 未来事件日历")
        for r in cal:
            lines.append(f"- {r.get('date')}：{r.get('name')}——{r.get('note', '')}")
        lines.append("")

    # ---- schemas
    lines.append("## 9. 需要你产出的内容")
    lines.append("")
    lines.append("### 9a. 事件卡 → 保存为 `data/events-%s.json`" % date)
    lines.append(EVENT_SCHEMA_HINT)
    lines.append("")
    lines.append("### 9b. 主线改写 → 保存为 `data/mainline-%s.json`" % date)
    lines.append(MAINLINE_SCHEMA_HINT)
    lines.append("")
    lines.append("（改写仅在命中关键事实时生效；未命中会被拒并沿用规则结论。）")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    with CARD.open(encoding="utf-8") as handle:
        card = json.load(handle)
    date = card["meta"]["market_date"]
    text = render(card)
    out = OUT_DIR / f"brief-{date}.md"
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print(f"wrote {out.relative_to(ROOT)} ({len(text)} chars, {text.count(chr(10))} lines)")


if __name__ == "__main__":
    main()
