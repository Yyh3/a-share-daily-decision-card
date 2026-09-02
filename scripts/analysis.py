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
        capital = carried[0] if carried else None
        conclusion = (f"存在情绪主线（{capital}），无产业级主线"
                      if carried else "不存在产业级主线")
        # The two dimensions often name different sectors: capital persistence
        # looks at flow, ladder concentration looks at limit-ups. Say so instead
        # of letting the reader wonder which one is "the" mainline.
        if carried and top_industry and top_industry != "—" and capital != top_industry:
            reason = (f"资金延续看 {capital}，涨停最集中于 {top_industry}（{top_count} 只）——"
                      f"两个维度不指向同一方向，属情绪载体而非产业主线。")
        else:
            reason = ("有方向能连续两日走强，但涨停分布未收敛到单一行业，属情绪载体而非产业主线。")
    else:
        conclusion = "不存在产业级主线"
        reason = ("昨日领涨方向几乎全部兑现失败，涨停分布发散，资金处于撤出状态——"
                  "存量资金只能在低位蓄力与情绪载体之间轮动。")

    sustained = (rotation or {}).get("sustained", 0)
    partial = (rotation or {}).get("partial", 0)
    failed_n = (rotation or {}).get("failed", 0)
    carried = [r["sector"] for r in (rotation or {}).get("rows", []) if r["symbol"] == "✓"]
    key_facts = [
        f"轮动 {sustained} 持续 / {partial} 半兑现 / {failed_n} 失败",
        f"最大流入 {in_name} {max_in:+.1f}亿",
        f"涨停最集中 {top_industry} {top_count} 只（{top_share * 100:.0f}%）",
        f"上涨行业 {breadth_pct:.0f}%",
    ]
    key_tokens = [t for t in (in_name, top_industry, f"{max_in:+.1f}亿",
                              f"{top_count} 只", f"{breadth_pct:.0f}%")
                  + tuple(carried[:1]) if t and t != "—"]
    dissents = [c["meaning"] for c in criteria
                if any(word in c["meaning"] for word in ("撤出", "未收敛", "发散", "存量"))]
    # One sustained sector already reads as a 情绪级 carrier; two is needed
    # before the ladder-convergence branch can call it 产业级. The level must
    # agree with the conclusion sentence, so both read the same flags.
    any_carry = bool(rotation and rotation.get("sustained", 0) >= 1)
    return {"conclusion": conclusion, "reason": reason, "criteria": criteria,
            "dimensions": {"capital": carried[0] if carried else None,
                           "ladder": top_industry if top_industry != "—" else None,
                           "aligned": bool(carried) and carried[0] == top_industry},
            "structured": {
                "level": "产业级" if (converged and has_carry) else ("情绪级" if any_carry else "无"),
                "industrial": bool(converged and has_carry),
                "criteria_met": sum(1 for c in criteria
                                    if "未收敛" not in c["meaning"] and "撤出" not in c["meaning"]
                                    and "发散" not in c["meaning"]),
                "key_facts": key_facts, "key_tokens": key_tokens, "dissents": dissents,
            },
            "method": "5 项判据全部由本地数据与固定阈值计算；结论句由判据组合规则生成，"
                      "外部改写须命中关键事实后才被采用。"}


def apply_mainline_rewrite(view: dict[str, Any] | None, rewrite: str | None,
                           min_facts: int = 2) -> dict[str, Any] | None:
    """Swap in a reworded conclusion only when it still carries the facts.

    The judgment itself never comes from the rewrite: it must quote at least
    ``min_facts`` of the rule-derived tokens, otherwise the template sentence
    stands. Keeps the wording human without letting wording change the call.
    """
    if not view or not rewrite:
        return view
    tokens = ((view.get("structured") or {}).get("key_tokens") or [])
    hits = [t for t in tokens if str(t) in str(rewrite)]
    if len(hits) < min_facts:
        return view
    out = dict(view)
    out["conclusion"] = str(rewrite).strip()
    out["conclusion_origin"] = "llm"
    return out


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


# Why an assertion could not be scored. Carried onto the card so a "?" never
# reads as a silent failure.
REASON_LABEL = {
    "missing_data": "当日快照缺该项数据",
    "sector_absent": "板块未出现在当日快照（可能改名或退市）",
    "stale": "已错过目标验证日，判据只在次日有效",
}


def evaluate_check(check: dict[str, Any], ctx: dict[str, Any], fresh: bool = True) -> str | None:
    """Score one assertion against a later session. Returns ✓ / ✗ / △ or None."""
    return evaluate_check_detail(check, ctx, fresh)[0]


def evaluate_check_detail(check: dict[str, Any], ctx: dict[str, Any],
                          fresh: bool = True) -> tuple[str | None, str | None]:
    """Score one assertion and explain a non-answer.

    ``fresh`` marks the first session after the checklist was filed. Past that
    point an assertion about "tomorrow" can no longer be answered honestly, so
    it is reported as stale instead of being scored against the wrong day.
    """
    if not fresh:
        return None, "stale"
    kind = check.get("type")
    params = check.get("params") or {}

    if kind == "sector_day5_positive":
        row = (ctx.get("flows") or {}).get(params.get("sector"))
        if not row:
            return None, "sector_absent"
        return ("✓" if float(row.get("day5") or 0) > 0 else "✗"), None

    if kind == "rotation_payoff":
        symbol = (ctx.get("rotation") or {}).get(params.get("sector"))
        if symbol in ("✓", "△", "✗"):
            return symbol, None
        return None, "sector_absent"

    if kind == "turnover_floor":
        turnover = ctx.get("turnover")
        if turnover is None:
            return None, "missing_data"
        return ("✓" if float(turnover) >= float(params.get("floor")) else "✗"), None

    if kind == "board_continue":
        codes = ctx.get("zt_codes")
        if codes is None:
            return None, "missing_data"
        return ("✓" if params.get("code") in codes else "✗"), None

    if kind == "promotion_floor":
        rate = ctx.get("promotion_rate")
        if rate is None:
            return None, "missing_data"
        return ("✓" if float(rate) >= float(params.get("floor")) else "✗"), None

    return None, "missing_data"


def score_checks(checks: list[dict[str, Any]], ctx: dict[str, Any],
                 fresh: bool = True) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Evaluate a whole checklist; returns annotated rows and the tally."""
    annotated: list[dict[str, Any]] = []
    tally = {"✓": 0, "✗": 0, "△": 0, "?": 0}
    for check in checks:
        result, reason = evaluate_check_detail(check, ctx, fresh)
        key = result if result in tally else "?"
        tally[key] += 1
        row = dict(check)
        row["result"] = result or "?"
        if reason:
            row["reason"] = reason
            row["reason_label"] = REASON_LABEL.get(reason, reason)
        annotated.append(row)
    return annotated, tally


def ctx_fingerprint(ctx: dict[str, Any]) -> str:
    """Hash of the numbers that backtracking depends on.

    Re-running collection on the same day replaces the snapshot; the stored
    fingerprint lets the caller re-score instead of keeping a verdict taken
    from an earlier, thinner pass.
    """
    import hashlib
    import json as _json

    payload = {
        "flows": {name: _round(float(row.get("day5") or 0), 4)
                  for name, row in (ctx.get("flows") or {}).items()},
        "turnover": ctx.get("turnover"),
        "promotion_rate": ctx.get("promotion_rate"),
        "zt": sorted(str(c) for c in (ctx.get("zt_codes") or [])),
    }
    blob = _json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


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


# ------------------------------------------------------------- intraday rhythm
# Tencent m30 bar: [YYYYMMDDHHMM, open, close, high, low, volume]. The label is
# the END of the 30-minute window (1000 = 09:30-10:00). Bars for one A-share
# session: 1000 1030 1100 1130 1330 1400 1430 1500.
INTRADAY_SESSION_ENDS = ("1000", "1030", "1100", "1130", "1330", "1400", "1430", "1500")
INTRADAY_STEP = 0.5        # pct: announce a threshold crossing at every step
INTRADAY_TAIL_STEP = 0.2   # pct between the last two bars that counts as a tail move
INTRADAY_AM_PM_SWING = 0.5 # pct: afternoon reversal versus the morning close
INTRADAY_FLAT = 0.2        # pct: open within this band of prev close = 平开


def _pct_from(prev: float, value: float) -> float:
    return (value / prev - 1) * 100 if prev else 0.0


def intraday_timeline(bars: list[list], prev_close: float | None) -> dict[str, Any] | None:
    """Rule-based intraday rhythm for one session from m30 bars.

    Reports only what the bars show: how the session opened, every
    ±0.5pct threshold crossing with its time, the high/low moments, the
    morning-versus-afternoon balance and the final 30 minutes. No narrative.
    """
    if not bars or not prev_close:
        return None
    events: list[dict[str, str]] = []
    closes = [float(b[2]) for b in bars]
    times = [str(b[0])[8:10] + ":" + str(b[0])[10:12] for b in bars]

    open_pct = _pct_from(prev_close, float(bars[0][1]))
    if open_pct >= INTRADAY_FLAT:
        tone = "高开"
    elif open_pct <= -INTRADAY_FLAT:
        tone = "低开"
    else:
        tone = "平开"
    events.append({"time": times[0], "text": f"{tone} {open_pct:+.2f}%（首 30 分钟开盘价）"})

    crossed = 0
    for i, close in enumerate(closes):
        pct = _pct_from(prev_close, close)
        step = int(pct / INTRADAY_STEP)
        if step != crossed:
            direction = "涨超" if step > crossed else "跌超"
            level = abs(step) * INTRADAY_STEP
            events.append({"time": times[i], "text": f"较昨收{direction} {level:.1f}%"})
            crossed = step

    hi = closes.index(max(closes))
    lo = closes.index(min(closes))
    if hi != 0:
        events.append({"time": times[hi],
                       "text": f"盘中高点 {_pct_from(prev_close, closes[hi]):+.2f}%"})
    if lo != 0:
        events.append({"time": times[lo],
                       "text": f"盘中低点 {_pct_from(prev_close, closes[lo]):+.2f}%"})

    am_end = closes[3] if len(closes) > 3 else closes[0]
    pm_end = closes[-1]
    am_pct = _pct_from(prev_close, am_end)
    pm_pct = _pct_from(prev_close, pm_end)
    if am_pct - pm_pct >= INTRADAY_AM_PM_SWING:
        events.append({"time": times[-1], "text": f"午后回落（上午收 {am_pct:+.2f}% → 收盘 {pm_pct:+.2f}%）"})
    elif pm_pct - am_pct >= INTRADAY_AM_PM_SWING:
        events.append({"time": times[-1], "text": f"午后回升（上午收 {am_pct:+.2f}% → 收盘 {pm_pct:+.2f}%）"})

    if len(closes) >= 2:
        tail = _pct_from(closes[-2], closes[-1])
        if tail >= INTRADAY_TAIL_STEP:
            events.append({"time": times[-1], "text": f"尾盘 30 分钟拉升 {tail:+.2f}%"})
        elif tail <= -INTRADAY_TAIL_STEP:
            events.append({"time": times[-1], "text": f"尾盘 30 分钟跳水 {tail:+.2f}%"})

    vols = [float(b[5]) for b in bars]
    peak = vols.index(max(vols))
    if vols[peak] > 0 and peak != len(vols) - 1:
        events.append({"time": times[peak], "text": "全日最大 30 分钟成交时段"})

    if pm_pct >= 0:
        close_word = "收红"
    else:
        close_word = "收绿"
    open_part = "" if tone == "平开" else f" {open_pct:+.2f}%"
    return {"summary": f"{tone}{open_part}，收盘 {pm_pct:+.2f}%（{close_word}）",
            "events": events,
            "method": "30 分钟线规则检测：开盘定性、±0.5pct 阈值穿越、高低点时点、"
                      "上下午摆幅 ≥0.5pct 的拐点、尾盘 ±0.2pct 动作、最大量时段。指数级（上证）。"}


# ----------------------------------------------------------------- divergence
DIVERGENCE_THRESHOLD = 1.0  # pct gap between the two legs before it counts
DEFAULT_DIVERGENCE_PAIRS = (
    {"global": "费城半导体", "sector": "半导体", "theme": "半导体链"},
    {"global": "LME铜", "sector": "工业金属", "theme": "铜链"},
    {"global": "伦敦金现货", "sector": "贵金属", "theme": "贵金属链"},
    {"global": "恒生科技", "sector": "计算机", "theme": "科技成长"},
)


def divergence_list(global_rows: list[dict[str, Any]], today_rows: list[dict[str, Any]],
                    pairs: tuple[dict[str, str], ...] = DEFAULT_DIVERGENCE_PAIRS,
                    threshold: float = DIVERGENCE_THRESHOLD) -> list[dict[str, Any]]:
    """Cross-market sign mismatches: global asset vs the A-share sector it maps to.

    Both legs are same-day close pct where available; the global leg may lag
    (US close lands the next Beijing morning) and the caller notes that.
    """
    by_name = {row["name"]: row["pct"] for row in global_rows or [] if row.get("pct") is not None}
    by_sector = {row["sector"]: row.get("change_pct") for row in today_rows or []}
    rows = []
    for pair in pairs:
        g = by_name.get(pair["global"])
        a = by_sector.get(pair["sector"])
        if g is None or a is None:
            continue
        gap = round(a - g, 2)
        if g * a < 0 and abs(gap) >= threshold:
            rows.append({"theme": pair["theme"], "global_name": pair["global"],
                         "global_pct": round(g, 2), "sector": pair["sector"],
                         "sector_pct": round(a, 2), "gap": gap,
                         "note": "同向应为共振，一涨一跌且差距达阈值记为背离"})
    rows.sort(key=lambda r: -abs(r["gap"]))
    return rows


# -------------------------------------------------------------- macro calendar
# Month-based rules only. Fixed-date events (FOMC, summits) belong in the
# "fixed" list of data/macro-calendar.json and are maintained by hand.
DEFAULT_MACRO_RULES = (
    {"rule": "day_1", "name": "财新制造业PMI", "note": "每月 1 日前后，规则按 1 日推算"},
    {"rule": "first_friday", "name": "美国非农就业报告", "note": "每月首个周五"},
    {"rule": "day_10", "name": "中国CPI/PPI", "note": "通常 9-12 日公布，规则按 10 日推算"},
    {"rule": "day_15", "name": "美国CPI", "note": "每月中旬，规则按 15 日推算"},
    {"rule": "day_20", "name": "LPR报价", "note": "每月 20 日，遇节假日顺延"},
    {"rule": "month_end", "name": "中国官方PMI", "note": "当月最后一天（统计局）"},
)


def macro_calendar(market_date: str, days_ahead: int = 14,
                   rules: tuple[dict[str, str], ...] = DEFAULT_MACRO_RULES,
                   fixed: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """Next ``days_ahead`` calendar days of rule-derived macro events.

    Pure: builds candidate dates from the month rules plus any hand-maintained
    fixed dates, keeps those inside the window, sorted by date. Fixed entries
    win over rule entries on the same date.
    """
    import calendar as _cal
    import datetime as _dt

    start = _dt.date(int(market_date[:4]), int(market_date[5:7]), int(market_date[8:10]))
    end = start + _dt.timedelta(days=days_ahead)
    out: dict[_dt.date, dict[str, str]] = {}

    def month_of(day: _dt.date) -> list[tuple[_dt.date, str, str]]:
        found = []
        last = _cal.monthrange(day.year, day.month)[1]
        for rule in rules:
            kind = rule["rule"]
            if kind == "day_1":
                date = day.replace(day=1)
            elif kind == "first_friday":
                date = day.replace(day=1)
                while date.weekday() != 4:
                    date += _dt.timedelta(days=1)
            elif kind == "day_10":
                date = day.replace(day=10)
            elif kind == "day_15":
                date = day.replace(day=15)
            elif kind == "day_20":
                date = day.replace(day=20)
            elif kind == "month_end":
                date = day.replace(day=last)
            else:
                continue
            found.append((date, rule["name"], rule["note"]))
        return found

    for offset in range(days_ahead + 1):
        day = start + _dt.timedelta(days=offset)
        for date, name, note in month_of(day):
            if start <= date <= end:
                out[date] = {"date": date.isoformat(), "name": name, "note": note,
                             "source": "rule"}
    for item in fixed or []:
        try:
            date = _dt.date.fromisoformat(str(item.get("date", ""))[:10])
        except ValueError:
            continue
        if start <= date <= end:
            out[date] = {"date": date.isoformat(), "name": item.get("name", ""),
                         "note": item.get("note", ""), "source": "fixed"}
    return [out[key] for key in sorted(out)]


# -------------------------------------------------------------- direction pool
POOL_GRID_ROWS = 12
POSITION_ACCUMULATE_DAYS = 20   # trading days of sector pct history before ret20 shows
POSITION_HIGH = 5.0             # 20-day sector return pct that counts as 高位
POSITION_LOW = -5.0
POOL_ACTION = {
    "持续流入": ("重点观察", "放量大涨且板块涨停 ≥3 家，升级候选主线", "5 日净流入转负"),
    "拐点回流": ("刚掉头，真伪待一周验证", "连续 3 日净流入且放量", "5 日净流入转负"),
    "拐点撤退": ("高位资金撤离进行时", "5 日净流入转正", "10 日净流入也转负"),
    "持续流出": ("趋势性失血，反弹只当兑现", "5 日净流入转正", "净流出继续扩大"),
}


def _sector_position(pcts: list[float]) -> dict[str, Any]:
    """20-day compounded return from accumulated daily pct series."""
    if len(pcts) < POSITION_ACCUMULATE_DAYS:
        return {"ret20": None, "label": "积累中",
                "note": f"板块日涨跌样本 {len(pcts)}/{POSITION_ACCUMULATE_DAYS}，满样本前不判位置"}
    ret = 1.0
    for pct in pcts[-POSITION_ACCUMULATE_DAYS:]:
        ret *= 1 + float(pct) / 100
    ret20 = round((ret - 1) * 100, 2)
    if ret20 >= POSITION_HIGH:
        label = "高位"
    elif ret20 <= POSITION_LOW:
        label = "低位"
    else:
        label = "中位"
    return {"ret20": ret20, "label": label, "note": f"近 {POSITION_ACCUMULATE_DAYS} 日复利收益 {ret20:+.2f}%"}


def direction_pool(today_rows: list[dict[str, Any]], pool: list[dict[str, Any]],
                   rotation: dict[str, Any] | None, pct_history: dict[str, list[float]],
                   rows_limit: int = POOL_GRID_ROWS) -> list[dict[str, Any]]:
    """Direction tracking pool: 资金四分型 × 位置(需积累) → 固定动作语义.

    Candidates = accumulation pool ∪ rotation leaders ∪ today's extremes,
    scored by |5-day flow| so the strongest capital signals surface first.
    """
    candidates: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    extremes: list[str | None] = []
    if today_rows:
        extremes = [max(today_rows, key=lambda r: r.get("day5") or 0).get("sector"),
                    min(today_rows, key=lambda r: r.get("day5") or 0).get("sector")]
    for source in ([p["sector"] for p in pool or []]
                   + [r["sector"] for r in (rotation or {}).get("rows", []) or []]
                   + extremes):
        if not source or source in candidates:
            continue
        row = next((r for r in today_rows or [] if r["sector"] == source), None)
        if not row:
            continue
        candidates[source] = row
        order.append(source)
    order.sort(key=lambda name: -abs(float(candidates[name].get("day5") or 0)))
    rows = []
    for name in order[:rows_limit]:
        row = candidates[name]
        state = row.get("classification") or "持续流入"
        action, trigger, invalid = POOL_ACTION.get(state, ("观察", "资金转正", "资金转负"))
        position = _sector_position(pct_history.get(name) or [])
        rows.append({
            "sector": name,
            "position": position["label"], "ret20": position["ret20"],
            "position_note": position["note"],
            "state": state,
            "change_pct": row.get("change_pct"),
            "day5_yi": _round(float(row.get("day5") or 0), 1),
            "limit_up": int(row.get("limit_up") or 0),
            "action": action, "trigger": trigger, "invalid": invalid,
        })
    return rows



# ------------------------------------------------------------- sector de-dup
# The upstream feed mixes level-1/2/3 industries, so one direction can occupy
# several slots with identical numbers (证券Ⅱ / 证券Ⅲ). Rules stay conservative:
# a wrong merge hides a real direction, a missed merge only costs a slot.
ROMAN_SUFFIXES = ("Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ")
DEDUP_PREFIX_TOL = 0.05  # relative day5 gap below which a prefix pair counts as one


def _base_name(name: str) -> str:
    """Strip the level suffix: 证券Ⅱ -> 证券."""
    text = str(name or "")
    for suffix in ROMAN_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def dedupe_sectors(rows: list[dict[str, Any]], tol: float = DEDUP_PREFIX_TOL) -> list[dict[str, Any]]:
    """Collapse duplicated industry levels, preserving the incoming order.

    Three rules, in order: the same base name after stripping the level
    suffix; an identical (today, day5, day10) fingerprint; a name that is a
    strict prefix of another with a day5 gap under ``tol``. Callers sort by
    strength first, so the strongest member of a group survives.
    """
    kept: list[dict[str, Any]] = []
    seen_base: dict[str, str] = {}
    for row in rows or []:
        name = str(row.get("sector") or "")
        if not name:
            continue
        base = _base_name(name)
        today = _round(float(row.get("today") or 0), 4)
        day5 = float(row.get("day5") or 0)
        day10 = _round(float(row.get("day10") or 0), 4)
        dup = False
        if base in seen_base:
            dup = True
        if not dup:
            for prev in kept:
                p_today = _round(float(prev.get("today") or 0), 4)
                p_day5 = float(prev.get("day5") or 0)
                p_day10 = _round(float(prev.get("day10") or 0), 4)
                if today == p_today and day5 == p_day5 and day10 == p_day10:
                    dup = True
                    break
                p_name = str(prev.get("sector") or "")
                if not p_name:
                    continue
                short, long_ = (p_name, name) if len(p_name) <= len(name) else (name, p_name)
                if long_.startswith(short):
                    denom = max(abs(day5), abs(p_day5)) or 1.0
                    if abs(day5 - p_day5) / denom < tol:
                        dup = True
                        break
        if dup:
            continue
        seen_base[base] = name
        kept.append(row)
    return kept



# ------------------------------------------------------------- event cards
# Schema first: a card is only renderable when all seven fields are present and
# every number it quotes can be found in the snapshot. Rules produce candidates;
# an LLM (or a human) may fill cards, but the validator has the last word.
EVENT_LEVELS = ("高", "中", "低")
EVENT_DIRECTIONS = ("利多", "利空", "中性")
EVENT_FIELDS = ("title", "level", "direction", "summary", "transmission", "evidence", "risk")
EVENT_MIN_PCT = 3.0        # sector move that reads as a move
EVENT_MIN_FLOW_YI = 10.0   # main-flow size that makes it material
EVENT_MIN_LIMITUP = 2      # limit-ups inside one industry
EVENT_OUTFLOW_YI = -50.0   # main-flow that reads as heavy selling
EVENT_INST_YI = 2.0        # dragon-tiger institutional net buy, 亿
EVENT_PROMO_SPLIT = 30.0   # promotion rate under which tall boards look fragile
EVENT_ANCHOR_TOL = 0.02    # relative/absolute slack when matching anchor numbers


def scan_event_candidates(today_rows: list[dict[str, Any]],
                          divergence: list[dict[str, Any]] | None = None,
                          dragon: dict[str, Any] | None = None,
                          ladder: dict[str, Any] | None = None,
                          limit: int = 6) -> list[dict[str, Any]]:
    """Event cards derived only from the snapshot: no news feed, no narrative.

    Five sources, each with the numbers that justify it attached as anchors so
    the validator (and the reader) can check every claim.
    """
    events: list[dict[str, Any]] = []

    for row in today_rows or []:
        sector = row.get("sector")
        pct = row.get("change_pct")
        if not sector or pct is None:
            continue
        today = float(row.get("today") or 0)
        day5 = float(row.get("day5") or 0)
        day10 = float(row.get("day10") or 0)
        up = int(row.get("limit_up") or 0)

        if pct >= EVENT_MIN_PCT and today >= EVENT_MIN_FLOW_YI and up >= EVENT_MIN_LIMITUP:
            events.append({
                "title": f"{sector} 放量异动：{pct:+.2f}% 伴 {up} 家涨停",
                "level": "高" if up >= 3 else "中",
                "direction": "利多",
                "summary": f"{sector} 当日涨 {pct:+.2f}%，主力净流入 {today:+.1f} 亿元，板块内 {up} 家涨停。",
                "transmission": "资金与涨停共振；次日延续则升级为候选主线，回落则按一日游处理。",
                "evidence": f"5 日 {day5:+.1f} 亿、10 日 {day10:+.1f} 亿，分类为 {row.get('classification') or '—'}。",
                "risk": "单日放量无法区分主升与情绪冲高，需次日兑现验证。",
                "origin": "rule",
                "anchors": [{"label": "当日涨幅", "value": _round(float(pct), 2)},
                            {"label": "主力净流入(亿)", "value": _round(today, 1)},
                            {"label": "板块涨停", "value": up}],
            })
        if today <= EVENT_OUTFLOW_YI:
            events.append({
                "title": f"{sector} 遭大额抛压：主力净流出 {abs(today):.1f} 亿元",
                "level": "中",
                "direction": "利空",
                "summary": f"{sector} 当日主力净流出 {today:.1f} 亿元，当日涨跌 {pct:+.2f}%。",
                "transmission": "大额流出先压缩板块内跟风资金的容错，反弹多按兑现处理。",
                "evidence": f"5 日 {day5:+.1f} 亿、10 日 {day10:+.1f} 亿，分类为 {row.get('classification') or '—'}。",
                "risk": "资金流出与股价涨跌可能背离，需结合涨停与量能一并看。",
                "origin": "rule",
                "anchors": [{"label": "主力净流出(亿)", "value": _round(today, 1)},
                            {"label": "当日涨幅", "value": _round(float(pct), 2)}],
            })

    for item in divergence or []:
        events.append({
            "title": f"{item['theme']} 跨市场背离：{item['global_name']} {item['global_pct']:+.2f}% "
                     f"vs A 股{item['sector']} {item['sector_pct']:+.2f}%",
            "level": "中",
            "direction": "中性",
            "summary": f"同日两腿走势相反，差值 {item['gap']:+.2f}pct。",
            "transmission": "背离通常先由 A 股补跌或补涨来收敛；外盘腿领先时可作次日开盘的观察点。",
            "evidence": f"{item['global_name']} {item['global_pct']:+.2f}%，{item['sector']} {item['sector_pct']:+.2f}%。",
            "risk": "美股腿是北京时间次日凌晨收盘，与 A 股存在时差，不能当作同刻信号。",
            "origin": "rule",
            "anchors": [{"label": "外盘涨跌", "value": _round(float(item["global_pct"]), 2)},
                        {"label": "A股板块涨跌", "value": _round(float(item["sector_pct"]), 2)}],
        })

    summary = (dragon or {}).get("summary") or {}
    # Both seat buckets read the same way: net buying is 利多, net selling 利空.
    for label, key in (("机构专用", "inst_net_wan"), ("北向席位", "north_net_wan")):
        value = summary.get(key)
        if value is None:
            continue
        yi = float(value) / 10000.0
        if abs(yi) < EVENT_INST_YI:
            continue
        buying = yi > 0
        events.append({
            "title": f"{label}当日净{'买入' if buying else '卖出'} {abs(yi):.2f} 亿元",
            "level": "中",
            "direction": "利多" if buying else "利空",
            "summary": f"龙虎榜 {label} 合计净{'买入' if buying else '卖出'} {abs(yi):.2f} 亿元。",
            "transmission": "席位方向只说明当日主力态度，隔日溢价需结合个股位置判断。",
            "evidence": f"取自当日龙虎榜披露（软依赖，交易所 18:00 后陆续发布）。",
            "risk": "龙虎榜为抽样披露，仅覆盖上榜个股，不代表全市场资金。",
            "origin": "rule",
            "anchors": [{"label": f"{label}净额(亿)", "value": _round(yi, 2)}],
        })

    metrics = (ladder or {}).get("metrics") or {}
    rate = metrics.get("promotion_rate")
    board = int(metrics.get("max_board") or 0)
    if rate is not None and rate < EVENT_PROMO_SPLIT and board >= 5:
        events.append({
            "title": f"高位股分歧：{board} 板高度 vs 晋级率 {rate}%",
            "level": "高",
            "direction": "中性",
            "summary": f"最高连板 {board} 板仍在，但晋级率只有 {rate}%，高度与质量背离。",
            "transmission": "晋级率走低通常先于高度塌陷，是退潮的前端信号而非末端。",
            "evidence": f"炸板 {metrics.get('zha_ban')} 家、封板率 {metrics.get('seal_rate')}%。",
            "risk": "单日晋级率受基期涨停家数影响，需连续观察。",
            "origin": "rule",
            "anchors": [{"label": "晋级率", "value": _round(float(rate), 1)},
                        {"label": "最高连板", "value": board}],
        })

    rank = {"高": 0, "中": 1, "低": 2}
    events.sort(key=lambda e: (rank.get(e.get("level"), 3), e.get("title", "")))
    return events[:limit]


def validate_events(events: list[dict[str, Any]], allowed_values,
                    tol: float = EVENT_ANCHOR_TOL) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Keep only cards the snapshot can vouch for.

    Every anchor value must match a number present in ``allowed_values`` within
    ``tol``. Rejected cards come back with a reason so the caller can explain
    why a slot stayed empty instead of silently dropping them.
    """
    numbers: list[float] = []
    for value in allowed_values or []:
        try:
            numbers.append(float(value))
        except (TypeError, ValueError):
            continue

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for item in events or []:
        title = str(item.get("title") or "(无标题)")
        missing = [f for f in EVENT_FIELDS if not str(item.get(f) or "").strip()]
        if missing:
            rejected.append({"title": title, "why": f"缺字段：{'、'.join(missing)}"})
            continue
        if item.get("level") not in EVENT_LEVELS:
            rejected.append({"title": title, "why": f"level 取值须为 {'/'.join(EVENT_LEVELS)}"})
            continue
        if item.get("direction") not in EVENT_DIRECTIONS:
            rejected.append({"title": title, "why": f"direction 取值须为 {'/'.join(EVENT_DIRECTIONS)}"})
            continue
        if not item.get("anchors"):
            # A card with no quoted numbers cannot be checked, so it cannot be
            # shown: an empty anchor list would otherwise sail through.
            rejected.append({"title": title, "why": "未附数值出处（anchors 为空），无法核对"})
            continue
        unverified = []
        for anchor in item.get("anchors") or []:
            label = str(anchor.get("label") or "?")
            try:
                value = float(anchor.get("value"))
            except (TypeError, ValueError):
                unverified.append(label)
                continue
            if not any(abs(value - n) <= max(tol, abs(n) * tol) for n in numbers):
                unverified.append(f"{label}={value}")
        if unverified:
            rejected.append({"title": title, "why": f"数值在当日快照中无出处：{'、'.join(unverified)}"})
            continue
        accepted.append(item)
    return accepted, rejected



# ------------------------------------------------------------ scenario plan
# Probabilities stay on a fixed prior until history can replace them. The basis
# travels with the number so a prior is never mistaken for an observed rate.
SCENARIO_PRIOR = {"加速上行": 25, "震荡分化": 50, "补跌退潮": 25}
SCENARIO_ACTION = {
    "加速上行": "持仓为主，回踩不破短线支撑可加；只做主线，不碰补涨。",
    "震荡分化": "降换手，只在主线内做 T；不打板、不追高位断层。",
    "补跌退潮": "先降仓，高位股与断层股优先减；等新方向被资金确认再进。",
}
SCENARIO_MIN_SAMPLES = 60   # sessions of history before priors give way to base rates


def _round5(value: float) -> int:
    """Round a limit-up count to the nearest 5 so triggers read cleanly."""
    return max(5, int(round(value / 5.0)) * 5)


def build_scenarios(ladder: dict[str, Any] | None, turnover: float | None,
                    sample_days: int = 0) -> dict[str, Any] | None:
    """Three next-session scenarios with falsifiable triggers.

    Triggers are built from today's own numbers, so tomorrow's card can check
    them without interpretation. Probabilities are a prior until enough
    sessions accumulate; the basis is always reported.
    """
    metrics = (ladder or {}).get("metrics") or {}
    zt = int(metrics.get("limit_up") or 0)
    if zt <= 0 or not turnover:
        return None
    rate = metrics.get("promotion_rate")
    turnover = float(turnover)

    up_floor = _round5(zt * 1.2)
    flat_low = _round5(zt * 0.8)
    down_ceil = _round5(zt * 0.7)
    money_floor = round(turnover * 0.95, 2)
    money_up = round(turnover * 1.05, 2)

    scenarios = [
        {"name": "加速上行", "probability": SCENARIO_PRIOR["加速上行"],
         "trigger": f"涨停 ≥ {up_floor} 家、晋级率 ≥ 40%、成交 ≥ {money_up:.2f} 万亿（三条同时成立）",
         "action": SCENARIO_ACTION["加速上行"]},
        {"name": "震荡分化", "probability": SCENARIO_PRIOR["震荡分化"],
         "trigger": f"涨停落在 {flat_low}~{up_floor} 家区间、成交不低于 {money_floor:.2f} 万亿",
         "action": SCENARIO_ACTION["震荡分化"]},
        {"name": "补跌退潮", "probability": SCENARIO_PRIOR["补跌退潮"],
         "trigger": (f"涨停 ≤ {down_ceil} 家，或晋级率 < 20%，或成交跌破 {money_floor:.2f} 万亿"
                     + (f"（今日晋级率 {rate}%）" if rate is not None else "")),
         "action": SCENARIO_ACTION["补跌退潮"]},
    ]
    observed = sample_days >= SCENARIO_MIN_SAMPLES
    return {"scenarios": scenarios,
            "probability_basis": "observed" if observed else "prior",
            "sample_days": sample_days,
            "method": ("触发阈值由当日数值按固定系数推导，次日可直接核对；"
                       + ("概率取自历史基频。" if observed
                          else f"概率为固定先验（{SCENARIO_PRIOR['加速上行']}/"
                               f"{SCENARIO_PRIOR['震荡分化']}/{SCENARIO_PRIOR['补跌退潮']}），"
                               f"积累 {SCENARIO_MIN_SAMPLES} 个交易日后切换为历史统计。"))}