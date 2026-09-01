#!/usr/bin/env python3
"""Aggregate local raw snapshots into the static decision-card payload.

No network calls are made. Rules are deterministic and intentionally simple.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
OUTPUT = ROOT / "data" / "market-card.json"
FLOW_TABLE_ROWS = 20  # displayed rows in the flow table (sorted by 5d flow)
POOL_ROWS = 10        # displayed rows in the accumulation pool
LADDER_DISPLAY_ROWS = 18   # displayed rows in the limit-up ladder
STYLE_THRESHOLD = 1.0      # pct points before a style edge is called


def style_view(panel: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Rule-based style coordinate from the 20-day index returns.

    规模：中证1000 - 沪深300（正 = 小盘占优）
    价值：中证红利 - 沪深300（正 = 红利/价值占优）

    Deliberately expressionless: it reports the spread and labels it by a fixed
    threshold. No narrative, no forecast.
    """
    by_key = {row["key"]: row for row in panel or []}
    small = (by_key.get("zz1000") or {}).get("ret20")
    large = (by_key.get("hs300") or {}).get("ret20")
    dividend = (by_key.get("zzdiv") or {}).get("ret20")
    if None in (small, large, dividend):
        return None
    size_edge = round(small - large, 2)
    value_edge = round(dividend - large, 2)

    def label(edge: float, pos: str, neg: str) -> str:
        if edge >= STYLE_THRESHOLD:
            return pos
        if edge <= -STYLE_THRESHOLD:
            return neg
        return "差异不显著"

    size_label = label(size_edge, "小盘占优", "大盘占优")
    value_label = label(value_edge, "红利/价值占优", "成长占优")
    return {
        "size_edge": size_edge,
        "value_edge": value_edge,
        "size_label": size_label,
        "value_label": value_label,
        "note": (f"规模：中证1000 20日 {small:+.2f}% vs 沪深300 {large:+.2f}%"
                 f"（差 {size_edge:+.2f}pct，{size_label}）；"
                 f"价值：中证红利 20日 {dividend:+.2f}% vs 沪深300 {large:+.2f}%"
                 f"（差 {value_edge:+.2f}pct，{value_label}）。"),
        "method": f"20 日区间收益之差，按 ±{STYLE_THRESHOLD}pct 阈值归类；规则生成，非叙事判断。",
    }


def load_documents() -> list[dict[str, Any]]:
    files = sorted(RAW_DIR.glob("*.json"))
    if not files:
        raise FileNotFoundError("data/raw 中没有 JSON；请保留 sample.json 或放入真实快照")
    documents = []
    for path in files:
        with path.open(encoding="utf-8") as handle:
            doc = json.load(handle)
        doc["_file"] = path.name
        documents.append(doc)
    return documents


def classify_flow(row: dict[str, Any]) -> str:
    day5, day10 = float(row["day5"]), float(row["day10"])
    if day5 > 0 and day10 > 0:
        return "持续流入"
    if day5 > 0 >= day10:
        return "拐点回流"
    if day5 < 0 <= day10:
        return "拐点撤退"
    return "持续流出"


def emotion(breadth: dict[str, Any], days: list[dict[str, Any]]) -> dict[str, Any]:
    total = breadth["up"] + breadth["down"] + breadth["flat"]
    up_ratio = round(breadth["up"] / total * 100, 1) if total else 0
    limit_balance = breadth["limit_up"] - breadth["limit_down"]
    turnover_change = round(days[-1]["turnover"] - days[-2]["turnover"], 2) if len(days) > 1 else 0
    score = (2 if up_ratio >= 60 else 1 if up_ratio >= 50 else 0)
    score += 2 if limit_balance >= 50 else 1 if limit_balance >= 20 else 0
    score += 1 if turnover_change > 0 else 0
    labels = {0:"退潮", 1:"偏弱", 2:"修复", 3:"活跃", 4:"高潮", 5:"高潮"}
    return {"label": labels[score], "score": score, "up_ratio": up_ratio,
            "turnover_change": turnover_change,
            "method": "上涨占比、涨跌停差与成交额环比的固定阈值评分"}


def build(documents: list[dict[str, Any]]) -> dict[str, Any]:
    # Real (non-demo) snapshots win over demo samples; latest market date wins
    # among them. This makes repeated builds stable and lets users keep
    # historical snapshots in data/raw without accidental field mixing.
    source = max(documents, key=lambda d: (
        not d.get("meta", {}).get("demo", True),
        d.get("meta", {}).get("market_date", ""),
        d["_file"],
    ))
    days = sorted(source["market_days"], key=lambda row: row["date"])[-5:]
    flows = []
    for raw in source["flows"]:
        row = dict(raw)
        row["classification"] = classify_flow(row)
        flows.append(row)
    flows.sort(key=lambda row: (-row["day5"], row["sector"]))

    # Candidate pool: positive 5-day flow, subdued daily move, and fewer than
    # three limit-ups. Score provides a deterministic display order.
    pool = []
    for row in flows:
        if row["day5"] > 0 and abs(row["change_pct"]) < 2 and row["limit_up"] < 3:
            score = round(row["day5"] + max(row["day10"], 0) * 0.25 - abs(row["change_pct"]) * 5, 1)
            pool.append({"sector":row["sector"], "score":score, "reason":
                         f"5日 {row['day5']:+.1f}亿 / 10日 {row['day10']:+.1f}亿，"
                         f"当日 {row['change_pct']:+.2f}%，涨停 {row['limit_up']} 家"})
    pool.sort(key=lambda row: (-row["score"], row["sector"]))
    pool = pool[:POOL_ROWS]

    mood = emotion(source["breadth"], days)
    latest = days[-1]
    ladder = source.get("limit_ladder") or {}
    metrics = ladder.get("metrics") or {}
    panel = source.get("index_panel", [])
    style = style_view(panel)
    verdicts = [
        {"tag":"市场定性", "title":f"情绪处于“{mood['label']}”，指数与题材表现分化",
         "evidence":f"上涨占比 {mood['up_ratio']}%，涨停/跌停 {source['breadth']['limit_up']}/{source['breadth']['limit_down']}，成交额环比 {mood['turnover_change']:+.2f} 万亿元。",
         "action":"以确认信号为先，不把单日涨停数量等同于趋势。"},
    ]
    if flows:
        strongest = max(flows, key=lambda row: row["day5"])
        weakest = min(flows, key=lambda row: row["today"])
        verdicts.append(
            {"tag":"资金线索", "title":f"{strongest['sector']}的 5 日净流入居样本首位",
             "evidence":f"5 日 {strongest['day5']:+.1f} 亿元，10 日 {strongest['day10']:+.1f} 亿元；分类为{strongest['classification']}。",
             "action":"进入观察池，需等待量价与板块广度共同确认。"})
        if metrics:
            promote = (f"{metrics['promotion_rate']:.1f}%" if metrics.get("promotion_rate") is not None
                       else "暂无")
            verdicts.append(
                {"tag":"情绪结构",
                 "title":f"最高 {metrics.get('max_board', 0)} 连板，封板率 {metrics.get('seal_rate')}%",
                 "evidence":f"涨停 {metrics.get('limit_up')} 家 / 跌停 {metrics.get('limit_down')} 家，"
                            f"炸板 {metrics.get('zha_ban')} 家，2 板及以上 {metrics.get('two_board_plus')} 家，"
                            f"涨停股次日晋级率 {promote}。",
                 "action":"晋级率与封板率同时走弱时，视为情绪退潮信号，不宜接力高位板。"})
        verdicts.append(
            {"tag":"风险约束", "title":f"{weakest['sector']}当日资金流出最明显",
             "evidence":f"当日 {weakest['today']:+.1f} 亿元，5 日 {weakest['day5']:+.1f} 亿元。",
             "action":"不因单日下跌逆势猜底；5 日资金转正前保持谨慎。"})
    else:
        verdicts.append(
            {"tag":"数据缺口", "title":"行业资金流数据本次未采集成功",
             "evidence":"采集时资金流接口不可用（详见来源与风险提示），资金章节与观察池为空。",
             "action":"重跑 scripts/collect_data.py 补齐后再参考资金相关结论。"})
    meta = dict(source["meta"])
    meta["input_file"] = source["_file"]
    # The board universe mixes industry levels (~500 boards); the table keeps
    # only the top flows so the card stays readable. Verdicts and the pool are
    # computed on the FULL flow list above, so capping does not skew them.
    display_flows = flows[:FLOW_TABLE_ROWS]
    display_ladder = dict(ladder)
    if ladder:
        display_ladder["ladder"] = ladder.get("ladder", [])[:LADDER_DISPLAY_ROWS]
    return {"meta":meta, "status":{"emotion":mood, "market_tone":latest["feature"],
            "turnover":latest["turnover"]}, "verdicts":verdicts, "market_days":days,
            "index_panel":panel, "style":style,
            "breadth":source["breadth"], "flows":display_flows, "accumulation_pool":pool,
            "limit_ladder":display_ladder, "margin":source.get("margin"),
            "global_markets":source.get("global_markets", []),
            "global_as_of":source.get("global_as_of"),
            "global_us_session":source.get("global_us_session", "unknown"),
            "events":source.get("events", []), "scenarios":source.get("scenarios", []),
            "risk_notes":source.get("risk_notes", [])}


def main() -> None:
    payload = build(load_documents())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"built {OUTPUT.relative_to(ROOT)} from {payload['meta']['input_file']}")


if __name__ == "__main__":
    main()
