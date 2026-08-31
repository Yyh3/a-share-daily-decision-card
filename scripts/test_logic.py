#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline unit tests for the pure logic in collect_data.py / build_data.py.

No network access. Run:  python -X utf8 scripts/test_logic.py
Exits non-zero on the first failure.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_data  # noqa: E402
import collect_data as cd  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ---------------------------------------------------------------- fixtures
DATES = [f"2026-08-{d:02d}" for d in range(3, 29) if d not in (8, 9, 15, 16, 22, 23)]  # 20 weekdays
MARKET = DATES[-1]


# ---------------------------------------------------------------- build_flow_rows
# day5/day10 now come straight from the ranking fields (f164/f174); the cache
# only backstops sectors whose ranking fields are missing.
def sec(code, name, pct, today, d5, d10):
    return {"code": code, "name": name, "pct": pct, "today": today, "day5": d5, "day10": d10}

history = {
    "BK01": {"name": "板块一", "daily": {d: 1.0e8 for d in DATES[:-1]}},   # 19d cached
    "BK02": {"name": "板块二", "daily": {d: -2.0e8 for d in DATES[:18]}},  # stale, misses today
    "BK03": {"name": "板块三", "daily": {d: 1.0e8 for d in DATES[:-1]}},   # 19d cached
}
sectors = [
    sec("BK01", "板块一", 1.5, 3.0e8, 7.0e8, 12.0e8),   # full ranking data
    sec("BK02", "板块二", -0.5, None, None, None),        # no data at all -> excluded
    sec("BK03", "板块三", 2.0, 5.0e8, None, None),        # ranking fields missing -> cache fallback
]
rows = cd.build_flow_rows(sectors, history, DATES, {"板块一": 2})
check("build_flow_rows: sectors without any data excluded", len(rows) == 2, f"got {len(rows)}")
by_name = {r["sector"]: r for r in rows}
r1 = by_name.get("板块一", {})
check("today from ranking", r1.get("today") == 3.0, f"got {r1.get('today')}")
check("day5 from ranking f164", r1.get("day5") == 7.0, f"got {r1.get('day5')}")
check("day10 from ranking f174", r1.get("day10") == 12.0, f"got {r1.get('day10')}")
check("limit_up attributed from ZT pool", r1.get("limit_up") == 2)
check("change_pct from ranking pct", r1.get("change_pct") == 1.5)
r3 = by_name.get("板块三", {})
check("fallback: day5 summed from cache (today 5 + 4x1)", r3.get("day5") == 9.0, f"got {r3.get('day5')}")
check("fallback: day10 summed from cache (today 5 + 9x1)", r3.get("day10") == 14.0, f"got {r3.get('day10')}")

# ---------------------------------------------------------------- day_feature
check("放量上攻", cd.day_feature(1.0, 1.0, 1.0, 0.1, 30) == "放量上攻")
check("缩量回调", cd.day_feature(-1.0, -1.0, -1.0, -0.1, 10) == "缩量回调")
check("指数弱题材活跃", cd.day_feature(-0.2, -0.3, -0.2, -0.01, 70) == "指数弱、题材活跃")
check("窄幅震荡", cd.day_feature(0.1, -0.1, 0.0, 0.0, 10) == "窄幅震荡")

# ---------------------------------------------------------------- classify_flow
check("持续流入", build_data.classify_flow({"day5": 1, "day10": 1}) == "持续流入")
check("拐点回流", build_data.classify_flow({"day5": 1, "day10": -1}) == "拐点回流")
check("拐点撤退", build_data.classify_flow({"day5": -1, "day10": 1}) == "拐点撤退")
check("持续流出", build_data.classify_flow({"day5": -1, "day10": -1}) == "持续流出")

# ---------------------------------------------------------------- emotion
breadth = {"up": 3200, "down": 1800, "flat": 200, "limit_up": 90, "limit_down": 5}
days = [{"date": "d1", "turnover": 2.0}, {"date": "d2", "turnover": 2.2}]
mood = build_data.emotion(breadth, days)
check("emotion up_ratio", abs(mood["up_ratio"] - 61.5) < 0.01, f"got {mood['up_ratio']}")
check("emotion score 5 -> 高潮", mood["label"] == "高潮", f"got {mood['label']}")

# ---------------------------------------------------------------- build_data end-to-end on sample
raw_dir = Path(__file__).resolve().parents[1] / "data" / "raw"
with (raw_dir / "sample.json").open(encoding="utf-8") as handle:
    sample = json.load(handle)
sample["_file"] = "sample.json"
payload = build_data.build([sample])
check("build(sample): 3 verdicts", len(payload["verdicts"]) == 3)
check("build(sample): pools non-empty", len(payload["accumulation_pool"]) > 0)
check("build(sample): flows classified",
      all("classification" in f for f in payload["flows"]))

# degradation path: real snapshot with empty flows beats demo sample
real = {"meta": {"market_date": "2026-08-28", "demo": False, "sources": [], "updated_at": "x"},
        "market_days": sample["market_days"], "breadth": sample["breadth"],
        "flows": [], "events": [], "scenarios": [], "risk_notes": []}
real["_file"] = "eod.json"
chosen = build_data.build([sample, real])
check("real snapshot beats newer demo", chosen["meta"]["input_file"] == "eod.json")
check("empty flows -> degraded verdicts", len(chosen["verdicts"]) == 2
      and chosen["verdicts"][1]["tag"] == "数据缺口")

print()
if FAILURES:
    print(f"{len(FAILURES)} test(s) FAILED: {FAILURES}")
    sys.exit(1)
print("ALL TESTS PASSED")
