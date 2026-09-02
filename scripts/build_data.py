#!/usr/bin/env python3
"""Aggregate local raw snapshots into the static decision-card payload.

No network calls are made. Rules are deterministic and intentionally simple.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analysis  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
CACHE_DIR = ROOT / "data" / "cache"
OUTPUT = ROOT / "data" / "market-card.json"
VERIFY_LOG = ROOT / "data" / "verify_log.json"
SEATS_KNOWN = ROOT / "data" / "seats-known.json"
CALENDAR_CFG = ROOT / "data" / "macro-calendar.json"
CALENDAR_DAYS_AHEAD = 14
LIQUIDITY_WINDOW = 10    # sessions behind the turnover ratio
FLOW_TABLE_ROWS = 20  # displayed rows in the flow table (sorted by 5d flow)
POOL_ROWS = 10        # displayed rows in the accumulation pool
LADDER_DISPLAY_ROWS = 18   # displayed rows in the limit-up ladder
STYLE_THRESHOLD = 1.0      # pct points before a style edge is called
LHB_STOCK_DISPLAY = 24     # dragon-tiger stock rows shown
LHB_SEAT_DISPLAY = 20      # dragon-tiger seat rows shown
LHB_SPECIAL_DISPLAY = 10   # rows per special-seat bucket shown
EVENTS_LIMIT = 6           # event cards rendered on the card
MAINLINE_MIN_FACTS = 2     # rule-derived tokens a rewrite must quote

CONFIG_DIR = ROOT / "data"


def trim_dragon_tiger(view: dict[str, Any] | None) -> dict[str, Any] | None:
    """Cap the dragon-tiger tables for display; keep every summary number.

    The full raw tables stay in data/raw/eod-*.json; the card only shows the
    extreme rows, which is what the |NET| sort already surfaced.
    """
    if not view:
        return None
    trimmed = dict(view)
    trimmed["stocks"] = (view.get("stocks") or [])[:LHB_STOCK_DISPLAY]
    trimmed["top_seats"] = (view.get("top_seats") or [])[:LHB_SEAT_DISPLAY]
    trimmed["special"] = {
        bucket: rows[:LHB_SPECIAL_DISPLAY]
        for bucket, rows in (view.get("special") or {}).items()
    }
    return trimmed


def load_json(path: Path, default: Any = None) -> Any:
    """Read a local cache/static file. Missing files degrade to the default."""
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return default


def snapshot_numbers(flows: list[dict[str, Any]], ladder: dict[str, Any],
                     divergence: list[dict[str, Any]] | None,
                     dragon: dict[str, Any] | None,
                     days: list[dict[str, Any]]) -> list[float]:
    """Every number an event card is allowed to quote.

    Built from the snapshot itself, so an anchor that is not here is a number
    the day's data cannot vouch for. Derived scales (万元 → 亿元) are included
    alongside the raw ones because cards quote whichever unit reads better.
    """
    values: list[float] = []
    for row in flows or []:
        for key in ("change_pct", "today", "day5", "day10", "limit_up"):
            value = row.get(key)
            if value is not None:
                try:
                    values.append(float(value))
                except (TypeError, ValueError):
                    continue
    for value in (ladder.get("metrics") or {}).values():
        if value is not None:
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                continue
    for item in divergence or []:
        for key in ("global_pct", "sector_pct", "gap"):
            value = item.get(key)
            if value is not None:
                try:
                    values.append(float(value))
                except (TypeError, ValueError):
                    continue
    summary = (dragon or {}).get("summary") or {}
    for key in ("inst_net_wan", "north_net_wan"):
        value = summary.get(key)
        if value is None:
            continue
        try:
            wan = float(value)
        except (TypeError, ValueError):
            continue
        values.extend([wan, wan / 10000.0])
    for row in days or []:
        for key in ("turnover", "close", "change_pct"):
            value = row.get(key)
            if value is not None:
                try:
                    values.append(float(value))
                except (TypeError, ValueError):
                    continue
    return values


def load_optional_cards(path: Path, key: str = "events") -> list[dict[str, Any]]:
    """Read an externally authored block (LLM or human) if one exists.

    Accepts either a bare list or {"key": [...]}. Missing or malformed files
    return an empty list: the rule layer is the baseline, not the fallback.
    """
    doc = load_json(path)
    if isinstance(doc, dict):
        doc = doc.get(key)
    if not isinstance(doc, list):
        return []
    return [item for item in doc if isinstance(item, dict)]


def prev_session_daily(history: dict[str, Any], today: str) -> tuple[str | None, dict[str, float]]:
    """Collapse sector_flow_history into {sector: net_yuan} for the session before today.

    Board names can repeat across industry levels, so equal names are summed.
    """
    if not history:
        return None, {}
    dates = sorted({date for item in history.values()
                    for date in ((item or {}).get("daily") or {})})
    earlier = [d for d in dates if d < today]
    if not earlier:
        return None, {}
    prev = earlier[-1]
    merged: dict[str, float] = {}
    for item in history.values():
        daily = (item or {}).get("daily") or {}
        value = daily.get(prev)
        if value is None:
            continue
        name = (item or {}).get("name")
        if not name:
            continue
        merged[name] = merged.get(name, 0.0) + float(value)
    return prev, merged


def liquidity_ratio(stats: dict[str, Any], today: str,
                    window: int = LIQUIDITY_WINDOW) -> tuple[float | None, int]:
    """Today's turnover vs the mean of the previous sessions (unit-free ratio)."""
    series = [(date, float(value["sz_turnover"]))
              for date, value in sorted((stats or {}).items())
              if isinstance(value, dict) and value.get("sz_turnover")]
    if not series:
        return None, 0
    today_value = dict(series).get(today)
    previous = [value for date, value in series if date < today][-window:]
    if today_value is None or not previous:
        return None, 0
    return round(today_value / (sum(previous) / len(previous)), 3), len(previous)


def run_verify_cycle(market_date: str, ctx: dict[str, Any],
                     new_checks: list[dict[str, Any]],
                     prev_trading_day: str | None = None) -> dict[str, Any]:
    """Backtrack the previous session's assertions, then file today's.

    An assertion is about the *next* session, so only a checklist filed on the
    previous trading day can be scored honestly. Older ones missed their window
    and are dropped rather than scored against the wrong day. Persisted to
    data/verify_log.json; re-running collection on the same day re-scores only
    when the underlying numbers actually changed.
    """
    log = load_json(VERIFY_LOG, {}) or {}
    generated = log.setdefault("generated", {})
    results = log.setdefault("results", {})
    fingerprint = analysis.ctx_fingerprint(ctx)

    past = sorted(date for date in generated if date < market_date)
    for date in past[:-1]:
        if date not in results:
            generated.pop(date, None)  # window closed long ago, never scoreable

    target = past[-1] if past else None
    if target and prev_trading_day and target != prev_trading_day:
        # A session was skipped; this checklist can no longer be answered.
        if target not in results:
            generated.pop(target, None)
        target = None

    if target:
        prior = results.get(target)
        rescore = prior is None or (
            prior.get("evaluated_on") == market_date
            and prior.get("fingerprint") != fingerprint)
        if rescore:
            rows, tally = analysis.score_checks(generated[target], ctx, fresh=True)
            results[target] = {"evaluated_on": market_date, "fingerprint": fingerprint,
                               "rows": rows, "tally": tally}

    if market_date not in generated:
        generated[market_date] = new_checks

    with VERIFY_LOG.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(log, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    retro = None
    for date in sorted(results, reverse=True):
        if date < market_date:
            retro = dict(results[date])
            retro["date"] = date
            break
    return {"retro": retro, "next_checks": new_checks,
            "method": "断言由当日数据规则生成，仅在紧邻的下一个交易日复盘时打分（✓/✗/△）；"
                      "判不出结果的一律标注原因，不静默留空。"}


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
    # The feed mixes industry levels, so one direction can occupy several slots
    # with identical numbers. Collapse before anything consumes the ranking.
    flows = analysis.dedupe_sectors(flows)

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

    # -------------------------------------------------- analysis layer (local)
    market_date = str(source["meta"].get("market_date") or latest["date"])
    flow_history = load_json(CACHE_DIR / "sector_flow_history.json", {}) or {}
    daily_stats = load_json(CACHE_DIR / "daily_stats.json", {}) or {}
    prev_date, prev_daily = prev_session_daily(flow_history, market_date)
    rotation = analysis.build_rotation_view(prev_daily, flows, prev_date, market_date)
    ratio, window = liquidity_ratio(daily_stats, market_date)
    mainline = analysis.build_mainline_view(flows, rotation,
                                            ladder.get("ladder") or [], ratio, window)
    stage = analysis.emotion_stage(metrics)
    forecast = analysis.trend_forecast(metrics, (ladder.get("ladder") or [None])[0],
                                       (stage or {}).get("stage"))

    # dragon-tiger: roll stocks up to industries and tag the seats
    direction: list[dict[str, Any]] = []
    seat_rows: list[dict[str, Any]] = []
    known_seats = {k: v for k, v in (load_json(SEATS_KNOWN, {}) or {}).items()
                   if not k.startswith("_")}
    dragon = source.get("dragon_tiger") or {}
    industry_map = dragon.get("industry_map") or {}
    if dragon.get("stocks") and industry_map:
        direction = analysis.aggregate_direction(dragon["stocks"], industry_map)
    for row in dragon.get("top_seats") or []:
        tagged = dict(row)
        tagged["seat_tag"] = analysis.seat_tag(row.get("seat") or "", known_seats)
        seat_rows.append(tagged)
    noise = analysis.noise_zone(flows)

    # batch-2 analysis blocks: divergence / macro calendar / direction pool
    divergence = analysis.divergence_list(source.get("global_markets") or [],
                                          flows)
    calendar_cfg = load_json(CALENDAR_CFG, {}) or {}
    calendar = analysis.macro_calendar(market_date, days_ahead=CALENDAR_DAYS_AHEAD,
                                       rules=analysis.DEFAULT_MACRO_RULES,
                                       fixed=calendar_cfg.get("fixed") or [])
    pct_history: dict[str, list[float]] = {}
    for entry in flow_history.values():
        name = (entry or {}).get("name")
        daily_pct = (entry or {}).get("pct") or {}
        if name and daily_pct:
            pct_history[name] = [daily_pct[d] for d in sorted(daily_pct)]
    pool_grid = analysis.direction_pool(flows, pool, rotation, pct_history)

    # batch-3: event cards / next-session scenarios / optional reworded mainline
    allowed_values = snapshot_numbers(flows, ladder, divergence, dragon, days)
    candidates = analysis.scan_event_candidates(flows, divergence, dragon, ladder,
                                                limit=EVENTS_LIMIT)
    external_events = load_optional_cards(CONFIG_DIR / f"events-{market_date}.json")
    events, rejected = analysis.validate_events(external_events + candidates,
                                                allowed_values)
    events = events[:EVENTS_LIMIT]
    events_meta = {
        "scanned": len(candidates), "external": len(external_events),
        "accepted": len(events), "rejected": rejected,
        "method": "候选由当日快照规则扫描生成；每张卡必须填满 7 个字段，且引用的每个数字"
                  "都能在当日快照中找到出处，否则不展示并注明原因。",
    }

    sample_days = max((len(v) for v in pct_history.values()), default=0)
    scenarios_doc = analysis.build_scenarios(ladder, latest.get("turnover"), sample_days)
    scenarios = (scenarios_doc or {}).get("scenarios") or []
    scenario_meta = None
    if scenarios_doc:
        scenario_meta = {k: v for k, v in scenarios_doc.items() if k != "scenarios"}

    rewrite_doc = load_json(CONFIG_DIR / f"mainline-{market_date}.json")
    rewrite_text = rewrite_doc.get("conclusion") if isinstance(rewrite_doc, dict) else (
        rewrite_doc if isinstance(rewrite_doc, str) else None)
    if rewrite_text:
        before = mainline
        mainline = analysis.apply_mainline_rewrite(mainline, str(rewrite_text),
                                                   min_facts=MAINLINE_MIN_FACTS)
        if mainline is not None and mainline is before:
            mainline = dict(mainline)
            mainline["rewrite"] = {
                "applied": False,
                "why": f"改写未命中至少 {MAINLINE_MIN_FACTS} 项关键事实，保留规则结论句。",
            }
        elif mainline is not None:
            mainline = dict(mainline)
            mainline["rewrite"] = {"applied": True, "why": "改写命中关键事实，仅替换措辞。"}

    # verification checklist: backtrack yesterday, file today.
    # promotion-rate history (prior sessions, oldest first) calibrates the
    # promo floor from a fixed 40% to the recent median once >=20 samples.
    promo_history = []
    for doc in documents:
        if doc is source:
            continue
        rate = ((doc.get("limit_ladder") or {}).get("metrics") or {}).get("promotion_rate")
        if doc.get("meta", {}).get("market_date", "") < market_date and rate is not None:
            promo_history.append(float(rate))
    new_checks = analysis.build_verify_checks(market_date, flows, ladder,
                                              latest.get("turnover"), pool,
                                              promo_history=promo_history)
    verify_ctx = {
        "flows": {row["sector"]: row for row in flows},
        "rotation": {row["sector"]: row["symbol"] for row in (rotation or {}).get("rows", [])},
        "turnover": latest.get("turnover"),
        "zt_codes": set(((daily_stats or {}).get(market_date) or {}).get("zt_codes") or []),
        "promotion_rate": metrics.get("promotion_rate"),
    }
    prev_trading_day = days[-2]["date"] if len(days) > 1 else None
    verify = run_verify_cycle(market_date, verify_ctx, new_checks, prev_trading_day)
    verdicts = [
        {"tag":"市场定性", "title":f"情绪处于“{mood['label']}”，指数与题材表现分化",
         "evidence":f"上涨占比 {mood['up_ratio']}%，涨停/跌停 {source['breadth']['limit_up']}/{source['breadth']['limit_down']}，成交额环比 {mood['turnover_change']:+.2f} 万亿元。",
         "action":"以确认信号为先，不把单日涨停数量等同于趋势。",
         "trigger":"上涨占比回到 60% 以上且成交额环比转正。",
         "invalid":"涨停家数跌破 40 家或上涨占比跌破 40%。"},
    ]
    if flows:
        strongest = max(flows, key=lambda row: row["day5"])
        weakest = min(flows, key=lambda row: row["today"])
        verdicts.append(
            {"tag":"资金线索", "title":f"{strongest['sector']}的 5 日净流入居样本首位",
             "evidence":f"5 日 {strongest['day5']:+.1f} 亿元，10 日 {strongest['day10']:+.1f} 亿元；分类为{strongest['classification']}。",
             "action":"进入观察池，需等待量价与板块广度共同确认。",
             "trigger":f"{strongest['sector']} 放量大涨且板块涨停 ≥3 家，升级为候选主线。",
             "invalid":f"{strongest['sector']} 5 日净流入转负，移出观察池。"})
        if metrics:
            promote = (f"{metrics['promotion_rate']:.1f}%" if metrics.get("promotion_rate") is not None
                       else "暂无")
            verdicts.append(
                {"tag":"情绪结构",
                 "title":f"最高 {metrics.get('max_board', 0)} 连板，封板率 {metrics.get('seal_rate')}%",
                 "evidence":f"涨停 {metrics.get('limit_up')} 家 / 跌停 {metrics.get('limit_down')} 家，"
                            f"炸板 {metrics.get('zha_ban')} 家，2 板及以上 {metrics.get('two_board_plus')} 家，"
                            f"涨停股次日晋级率 {promote}。",
                 "action":"晋级率与封板率同时走弱时，视为情绪退潮信号，不宜接力高位板。",
                 "trigger":"最高板继续晋级且晋级率回到 50% 以上。",
                 "invalid":"最高板断板且晋级率 <40%，按退潮处理。"})
        verdicts.append(
        {"tag":"风险约束", "title":f"{weakest['sector']}当日资金流出最明显",
         "evidence":f"当日 {weakest['today']:+.1f} 亿元，5 日 {weakest['day5']:+.1f} 亿元。",
         "action":"不因单日下跌逆势猜底；5 日资金转正前保持谨慎。",
         "trigger":f"{weakest['sector']} 5 日净流入转正，且当日回流超过当日流出的一半。",
         "invalid":f"{weakest['sector']} 5 日净流出继续扩大。"})
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
    dragon_view = trim_dragon_tiger(dragon)
    if dragon_view:
        dragon_view["top_seats"] = seat_rows[:LHB_SEAT_DISPLAY]
        dragon_view["direction"] = direction
    return {"meta":meta, "status":{"emotion":mood, "market_tone":latest["feature"],
            "turnover":latest["turnover"], "stage":stage}, "verdicts":verdicts,
            "market_days":days,
            "index_panel":panel, "style":style,
            "breadth":source["breadth"], "flows":display_flows, "accumulation_pool":pool,
            "limit_ladder":display_ladder, "margin":source.get("margin"),
            "rotation":rotation, "mainline":mainline, "forecast":forecast,
            "verify":verify, "noise":noise,
            "intraday":source.get("intraday"),
            "divergence":divergence, "calendar":calendar, "pool_grid":pool_grid,
            "global_markets":source.get("global_markets", []),
            "global_as_of":source.get("global_as_of"),
            "global_us_session":source.get("global_us_session", "unknown"),
            "us_treasury":source.get("us_treasury"),
            "dragon_tiger":dragon_view,
            "valuation":source.get("valuation", []),
            "lift_unlock":source.get("lift_unlock"),
            "etf_shares":source.get("etf_shares"),
            "events":events, "events_meta":events_meta,
            "scenarios":scenarios, "scenario_meta":scenario_meta,
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
