#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Analysis-layer rules for the decision card.

Everything here is pure: dicts and lists in, dicts and lists out, no network and
no file I/O. The caller (build_data.py) does the I/O. That split is what makes
the whole layer unit-testable offline — see scripts/test_logic.py.

Conventions
-----------
* Money in ``flows`` rows is **亿元**; money in ``sector_flow_history`` is
  **元**; money in dragon-tiger rows is **万元**. Conversion happens at the
  boundary, never in the middle of a rule.
* Every rule is expressionless: it reports a reading plus a fixed-threshold
  label. No narrative, no forecast beyond the stated conditional branches.
"""
from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------- config
ROTATION_TOP_N = 6          # yesterday's leaders tracked in the payoff table
ROTATION_MILD_DROP = -1.0   # pct: within this band the pullback counts as mild
MAINLINE_SHARE = 0.40       # ladder industry share that counts as converged
NOISE_MIN_PCT = 1.0         # sector up at least this much to be noise-checked
NOISE_MAX_PCT = 8.0         # ...but below this: a bigger move is an event, not noise
NOISE_MAX_ABS_YI = 5.0      # ...with main-flow smaller than this (亿元)
NOISE_TOP_N = 6

YUAN_PER_YI = 1e8


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


# ------------------------------------------------------------ rotation payoff
def rotation_verdict(change_pct: float | None, today_yi: float,
                     day5_yi: float) -> tuple[str, str, str]:
    """Classify how yesterday's leading sector behaved today.

    Returns (symbol, verdict, note). The four cases mirror the reference card's
    payoff table: ✓ 持续 / △ 半兑现 / ✗ 失败.
    """
    if change_pct is None:
        return ("?", "数据缺失", "当日无该板块读数")
    if change_pct >= 0 and today_yi >= 0:
        return ("✓", "持续", "价涨且资金未撤")
    if change_pct >= 0 and today_yi < 0:
        return ("△", "股价延续·资金分歧", "价格守住但主力在兑现")
    if change_pct >= ROTATION_MILD_DROP and day5_yi > 0:
        return ("△", "回吐但底仓未撤", "小幅回吐，多日资金仍为正")
    if change_pct < 0 and today_yi < 0:
        return ("✗", "一日游·兑现失败", "价跌且资金同步撤出")
    return ("✗", "停滞", "无价格延续也无资金承接")


def build_rotation_view(prev_daily: dict[str, float], today_rows: list[dict[str, Any]],
                        prev_date: str, today_date: str,
                        top_n: int = ROTATION_TOP_N) -> dict[str, Any] | None:
    """Yesterday's net-inflow leaders vs how they behaved today.

    ``prev_daily`` maps sector name -> main net flow in **元** for the previous
    session (data/cache/sector_flow_history.json). ``today_rows`` are the flow
    rows of the current session (亿元).
    """
    if not prev_daily or not today_rows:
        return None
    today_by_sector = {row["sector"]: row for row in today_rows}

    leaders = sorted(
        ((sector, float(net)) for sector, net in prev_daily.items() if net and net > 0),
        key=lambda item: -item[1],
    )[:top_n]
    if not leaders:
        return None

    rows: list[dict[str, Any]] = []
    for sector, prev_net in leaders:
        row = today_by_sector.get(sector) or {}
        change_pct = row.get("change_pct")
        today_yi = float(row.get("today") or 0.0)
        day5_yi = float(row.get("day5") or 0.0)
        symbol, verdict, note = rotation_verdict(change_pct, today_yi, day5_yi)
        rows.append({
            "sector": sector,
            "prev_yi": _round(prev_net / YUAN_PER_YI, 2),
            "today_yi": _round(today_yi, 2),
            "day5_yi": _round(day5_yi, 2),
            "change_pct": change_pct,
            "symbol": symbol,
            "verdict": verdict,
            "note": note,
        })

    sustained = sum(1 for r in rows if r["symbol"] == "✓")
    partial = sum(1 for r in rows if r["symbol"] == "△")
    failed = sum(1 for r in rows if r["symbol"] == "✗")
    total = len(rows)
    if total and failed * 2 >= total:
        tone = "典型的一日游市——追昨日最强是负期望策略"
    elif total and sustained >= partial and sustained >= failed:
        tone = "轮动有延续性，昨日强势方向可继续跟踪"
    else:
        tone = "半兑现居多，方向仍在分歧中"
    return {
        "prev_date": prev_date,
        "today_date": today_date,
        "rows": rows,
        "sustained": sustained,
        "partial": partial,
        "failed": failed,
        "total": total,
        "win_note": (f"{total} 个昨日领涨方向：{sustained} 持续 / {partial} 半兑现 / "
                     f"{failed} 失败——{tone}"),
        "method": ("兑现判定：价涨且资金不撤=持续；价涨资金撤/小幅回吐但多日为正=半兑现；"
                   "价跌且资金撤=失败。昨日领涨按主力净流入取前 %d 名。" % top_n),
    }


# ------------------------------------------------------------------- mainline
def build_mainline_view(today_rows: list[dict[str, Any]], rotation: dict[str, Any] | None,
                        ladder_rows: list[dict[str, Any]],
                        liquidity_ratio: float | None,
                        liquidity_window: int) -> dict[str, Any] | None:
    """Five deterministic criteria plus a rule-derived conclusion.

    No LLM: the conclusion sentence is assembled from the five readings.
    """
    if not today_rows:
        return None

    moved = [r for r in today_rows if r.get("change_pct") is not None]
    up_sectors = [r for r in moved if r["change_pct"] > 0]
    breadth_pct = round(len(up_sectors) / len(moved) * 100, 1) if moved else 0.0

    max_in = max((float(r.get("today") or 0) for r in today_rows), default=0.0)
    max_out = min((float(r.get("today") or 0) for r in today_rows), default=0.0)
    out_name = min(today_rows, key=lambda r: float(r.get("today") or 0))["sector"] if today_rows else "—"
    in_name = max(today_rows, key=lambda r: float(r.get("today") or 0))["sector"] if today_rows else "—"

    industry_count: dict[str, int] = {}
    for row in ladder_rows or []:
        sector = row.get("sector") or "未分类"
        industry_count[sector] = industry_count.get(sector, 0) + 1
    top_industry, top_count = (max(industry_count.items(), key=lambda kv: kv[1])
                               if industry_count else ("—", 0))
    ladder_total = sum(industry_count.values()) or 1
    top_share = round(top_count / ladder_total, 3)

    rotation_reading = "—"
    if rotation and rotation.get("total"):
        rotation_reading = (f"昨日 {rotation['total']} 大领涨：{rotation['sustained']} 持续 / "
                            f"{rotation['partial']} 半 / {rotation['failed']} 灭")

    concentration = "资金在撤出而非聚集" if abs(max_out) > max_in else "资金仍在净聚集"
    liquidity_reading = "—"
    if liquidity_ratio is not None:
        liquidity_reading = f"成交为近 {liquidity_window} 日均量的 {liquidity_ratio * 100:.0f}%"

    breadth_meaning = ("面宽但浅——轮动补涨多于进攻" if breadth_pct >= 60
                       else "涨跌分化，赚钱效应集中")
    ladder_meaning = (f"涨停最集中于 {top_industry}（{top_count} 只，占 {top_share * 100:.0f}%）"
                      + ("——已收敛为单一题材" if top_share >= MAINLINE_SHARE
                         else "——内部散，未收敛为单一题材"))

    criteria = [
        {"item": "板块宽度", "reading": f"上涨行业 {breadth_pct:.0f}%", "meaning": breadth_meaning},
        {"item": "轮动胜率", "reading": rotation_reading,
         "meaning": "主线无法从“昨日最强”中产生" if rotation and rotation.get("failed", 0) >= rotation.get("sustained", 0)
                    else "轮动具备延续基础"},
        {"item": "资金集中度", "reading": f"最大流入 {in_name} {max_in:+.1f}亿 vs 最大流出 {out_name} {max_out:+.1f}亿",
         "meaning": concentration},
        {"item": "涨停归类", "reading": f"{top_industry} {top_count} 只居首", "meaning": ladder_meaning},
        {"item": "流动性", "reading": liquidity_reading,
         "meaning": "存量博弈，仅支持单一情绪载体" if (liquidity_ratio or 1) < 1 else "量能支撑多线并行"},
    ]

    converged = top_share >= MAINLINE_SHARE
    has_carry = bool(rotation and rotation.get("sustained", 0) >= 2)
    if converged and has_carry:
        conclusion = f"主线候选：{top_industry}"
        reason = (f"涨停已向 {top_industry} 收敛（占 {top_share * 100:.0f}%），且昨日领涨方向有 "
                  f"{rotation['sustained']} 个延续，宽度与资金方向一致。")
    elif has_carry or (rotation and rotation.get("sustained", 0) >= 1):
        carried = [r["sector"] for r in (rotation or {}).get("rows", []) if r["symbol"] == "✓"]
        conclusion = (f"存在情绪主线（{'/'.join(carried[:2]) or '延续方向'}），无产业级主线"
                      if carried else "不存在产业级主线")
        reason = ("有方向能连续两日走强，但涨停分布未收敛到单一行业，属情绪载体而非产业主线。")
    else:
        conclusion = "不存在产业级主线"
        reason = ("昨日领涨方向几乎全部兑现失败，涨停分布发散，资金处于撤出状态——"
                  "存量资金只能在低位蓄力与情绪载体之间轮动。")
    return {"conclusion": conclusion, "reason": reason, "criteria": criteria,
            "method": "5 项判据全部由本地数据与固定阈值计算，结论句由判据组合规则生成。"}


# -------------------------------------------------------------- emotion stage
def emotion_stage(metrics: dict[str, Any]) -> dict[str, Any] | None:
    """Cycle stage from ladder height, promotion rate and seal quality."""
    if not metrics:
        return None
    height = int(metrics.get("max_board") or 0)
    promo = metrics.get("promotion_rate")
    seal = metrics.get("seal_rate")
    zha = int(metrics.get("zha_ban") or 0)
    two = int(metrics.get("two_board_plus") or 0)

    reasons: list[str] = []
    if height >= 7 and ((promo is not None and promo < 45) or zha >= 5):
        stage = "高潮区顶部（分歧显现）"
        reasons.append(f"{height} 板高度仍在")
        if promo is not None and promo < 45:
            reasons.append(f"晋级率 {promo:.1f}% 偏低")
        if zha >= 5:
            reasons.append(f"炸板 {zha} 家")
        reasons.append("高度还在，质量在降")
    elif height >= 5 and (promo or 0) >= 45:
        stage = "高潮区"
        reasons.append(f"{height} 板高度 + 晋级率 {promo:.1f}%")
    elif height >= 4:
        stage = "活跃区"
        reasons.append(f"最高 {height} 板，梯队成型")
    elif height >= 3:
        stage = "修复区"
        reasons.append(f"最高 {height} 板，赚钱效应弱修复")
    elif (promo is not None and promo < 30) or two <= 3:
        stage = "退潮/冰点期"
        reasons.append(f"最高仅 {height} 板，2 板及以上 {two} 家")
    else:
        stage = "修复区"
        reasons.append(f"最高 {height} 板")

    if promo is not None and promo >= 50:
        reasons.append("晋级率及格")
    if seal is not None:
        reasons.append(f"封板率 {seal}%")
    return {"stage": stage, "reasons": reasons,
            "method": "高度（连板数）+ 晋级率 + 炸板数的固定阈值组合，规则生成。"}


# ------------------------------------------------------------- trend forecast
def trend_forecast(metrics: dict[str, Any], top_row: dict[str, Any] | None,
                   stage: str | None) -> dict[str, Any] | None:
    """Three conditional branches for the next session, plus a default bet."""
    if not metrics:
        return None
    height = int(metrics.get("max_board") or 0)
    promo = metrics.get("promotion_rate")
    name = (top_row or {}).get("stock") or "最高板"
    promo_text = f"{promo:.1f}%" if promo is not None else "暂无"
    branches = [
        {"name": "退潮预警",
         "condition": f"{name}（{height}板）断板 且 晋级率 <40%（当前 {promo_text}）",
         "implication": "高位票全面回避，整体降防御，不接飞刀"},
        {"name": "高位换挡",
         "condition": f"{name} 断板但 2-3 板梯队接力晋级",
         "implication": "情绪降一档延续，只做中位分歧回踩"},
        {"name": "惯性冲顶",
         "condition": "全梯队晋级 且 晋级率 ≥50%",
         "implication": "最后的加速段，只赚分歧前的钱"},
    ]
    default = ("高潮区顶部 → 默认假设：高潮后必有退潮，优先防守"
               if stage and "顶部" in stage else
               "晋级率是当前最主要的观察变量，分歧中跟随不预判")
    return {"branches": branches, "default": default,
            "method": "三分支由梯队指标直接映射，条件与含义均为固定文本模板。"}


# ------------------------------------------------------------ verify checklist
def build_verify_checks(date: str, today_rows: list[dict[str, Any]],
                        ladder: dict[str, Any], turnover: float | None,
                        pool_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assertions about the NEXT session, generated deterministically."""
    checks: list[dict[str, Any]] = []

    for row in (pool_rows or [])[:2]:
        sector = row["sector"]
        checks.append({
            "id": f"{date}-pool-{sector}",
            "type": "sector_day5_positive",
            "params": {"sector": sector},
            "statement": f"{sector} 5 日净流入是否仍为正（蓄力池存亡判据）",
        })

    positive = [r for r in today_rows or [] if float(r.get("today") or 0) > 0]
    if positive:
        top = max(positive, key=lambda r: float(r.get("today") or 0))
        checks.append({
            "id": f"{date}-payoff-{top['sector']}",
            "type": "rotation_payoff",
            "params": {"sector": top["sector"]},
            "statement": f"今日领涨的{top['sector']}明日是否兑现（✗ 即失败）",
        })

    if turnover:
        floor = round(turnover * 0.95, 2)
        checks.append({
            "id": f"{date}-turnover",
            "type": "turnover_floor",
            "params": {"floor": floor},
            "statement": f"两市成交能否守住 {floor} 万亿（低于则缩量防御）",
        })

    metrics = (ladder or {}).get("metrics") or {}
    rows = (ladder or {}).get("ladder") or []
    if rows:
        top_row = max(rows, key=lambda r: int(r.get("board") or 0))
        checks.append({
            "id": f"{date}-board-{top_row.get('code')}",
            "type": "board_continue",
            "params": {"code": top_row.get("code"), "name": top_row.get("stock"),
                       "board": int(top_row.get("board") or 0)},
            "statement": f"{top_row.get('stock')}（{int(top_row.get('board') or 0)}板）是否继续涨停",
        })
    if metrics.get("promotion_rate") is not None:
        checks.append({
            "id": f"{date}-promo",
            "type": "promotion_floor",
            "params": {"floor": 40},
            "statement": "涨停晋级率能否守住 40%（低于则退潮预警）",
        })
    return checks


def evaluate_check(check: dict[str, Any], ctx: dict[str, Any]) -> str | None:
    """Score one assertion against a later session. Returns ✓ / ✗ / △ or None."""
    kind = check.get("type")
    params = check.get("params") or {}

    if kind == "sector_day5_positive":
        row = (ctx.get("flows") or {}).get(params.get("sector"))
        if not row:
            return None
        return "✓" if float(row.get("day5") or 0) > 0 else "✗"

    if kind == "rotation_payoff":
        symbol = (ctx.get("rotation") or {}).get(params.get("sector"))
        return symbol if symbol in ("✓", "△", "✗") else None

    if kind == "turnover_floor":
        turnover = ctx.get("turnover")
        if turnover is None:
            return None
        return "✓" if float(turnover) >= float(params.get("floor")) else "✗"

    if kind == "board_continue":
        codes = ctx.get("zt_codes")
        if codes is None:
            return None
        return "✓" if params.get("code") in codes else "✗"

    if kind == "promotion_floor":
        rate = ctx.get("promotion_rate")
        if rate is None:
            return None
        return "✓" if float(rate) >= float(params.get("floor")) else "✗"

    return None


def score_checks(checks: list[dict[str, Any]], ctx: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Evaluate a whole checklist; returns annotated rows and the tally."""
    annotated: list[dict[str, Any]] = []
    tally = {"✓": 0, "✗": 0, "△": 0, "?": 0}
    for check in checks:
        result = evaluate_check(check, ctx)
        key = result if result in tally else "?"
        tally[key] += 1
        row = dict(check)
        row["result"] = result or "?"
        annotated.append(row)
    return annotated, tally


# --------------------------------------------------------- dragon-tiger decode
def seat_tag(seat: str, known: dict[str, str]) -> str | None:
    """Map a seat name to a tag. Exact match wins, then substring keywords."""
    if not seat:
        return None
    if seat in known:
        return known[seat]
    for key, tag in known.items():
        if key and key in seat:
            return tag
    return None


def aggregate_direction(stock_rows: list[dict[str, Any]],
                        industry_map: dict[str, str],
                        top_n: int = 6) -> list[dict[str, Any]]:
    """Roll dragon-tiger stocks up to their industry: 'what chain is buying'."""
    buckets: dict[str, dict[str, Any]] = {}
    for row in stock_rows or []:
        sector = industry_map.get(row.get("code")) or "未分类"
        bucket = buckets.setdefault(sector, {"sector": sector, "net_wan": 0.0,
                                             "count": 0, "stocks": []})
        net = float(row.get("net_wan") or 0.0)
        bucket["net_wan"] += net
        bucket["count"] += 1
        bucket["stocks"].append({
            "code": row.get("code"), "name": row.get("name"),
            "net_wan": net, "pct": row.get("pct"),
        })
    rows = list(buckets.values())
    for row in rows:
        row["net_wan"] = round(row["net_wan"], 1)
        row["net_yi"] = round(row["net_wan"] / 10000, 2)
        row["stocks"].sort(key=lambda s: -s["net_wan"])
        row["top"] = row["stocks"][0] if row["stocks"] else None
        row["in_stocks"] = sum(1 for s in row["stocks"] if s["net_wan"] > 0)
    rows.sort(key=lambda r: -r["net_wan"])
    return rows[:top_n]


def noise_zone(today_rows: list[dict[str, Any]], top_n: int = NOISE_TOP_N) -> list[dict[str, Any]]:
    """Sectors that rose without flow or limit-ups: rotation noise, not cards."""
    rows = [r for r in today_rows or []
            if NOISE_MIN_PCT <= (r.get("change_pct") or 0) < NOISE_MAX_PCT
            and abs(float(r.get("today") or 0)) <= NOISE_MAX_ABS_YI
            and int(r.get("limit_up") or 0) == 0]
    rows.sort(key=lambda r: -(r.get("change_pct") or 0))
    return [{"sector": r["sector"], "change_pct": r["change_pct"],
             "today_yi": _round(float(r.get("today") or 0), 2)} for r in rows[:top_n]]
