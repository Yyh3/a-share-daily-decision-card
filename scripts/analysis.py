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
