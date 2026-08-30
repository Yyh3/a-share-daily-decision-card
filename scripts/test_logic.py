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


def sector_today(code: str, name: str, pct: float, today: float | None) -> dict:
    return {"code": code, "name": name, "pct": pct, "today_yuan": today}


# ---------------------------------------------------------------- flow_coverage
daily_full = {d: 1.0e8 for d in DATES}
cov5, cov20 = cd.flow_coverage(daily_full, DATES)
check("flow_coverage full history -> 5/5 and 20/20", cov5 == 5 and cov20 == 20, f"got {cov5}/{cov20}")

daily_sparse = {d: 1.0e8 for d in DATES[:15]}  # only oldest 15 days
cov5, cov20 = cd.flow_coverage(daily_sparse, DATES)
check("flow_coverage sparse -> 0/5 and 15/20", cov5 == 0 and cov20 == 15, f"got {cov5}/{cov20}")

# ---------------------------------------------------------------- build_flow_rows
history = {
    "BK01": {"name": "板块一", "daily": {d: 1.0e8 for d in DATES[:-1]}},   # 19d cached
    "BK02": {"name": "板块二", "daily": {d: -2.0e8 for d in DATES[:18]}},  # stale, misses today
}
sectors = [
    sector_today("BK01", "板块一", 1.5, 3.0e8),      # today merged over cache
    sector_today("BK02", "板块二", -0.5, None),        # no today flow -> excluded
    sector_today("BK03", "板块三", 2.0, 5.0e8),        # no cache -> coverage fail -> excluded
]
rows = cd.build_flow_rows(sectors, history, DATES, {"板块一": 2})
check("build_flow_rows: only covered sector included", len(rows) == 1, f"got {len(rows)}")
if rows:
    r = rows[0]
    check("today value comes from ranking (over cache)", r["today"] == 3.0, f"got {r['today']}")
    check("day5 = sum of last 5 (today 3 + 4x1)", r["day5"] == 7.0, f"got {r['day5']}")
    check("day20 = sum of 20 days (19x1 + 3)", r["day20"] == 22.0, f"got {r['day20']}")
    check("limit_up attributed from ZT pool", r["limit_up"] == 2)
    check("change_pct from ranking pct", r["change_pct"] == 1.5)

# today merged into cache even when cache is empty for the date
hist2 = {"BK01": {"name": "板块一", "daily": {d: 1.0e8 for d in DATES}}}
rows2 = cd.build_flow_rows([sector_today("BK01", "板块一", 0.0, -5.0e8)], hist2, DATES, {})
check("today wins over cache", rows2 and rows2[0]["today"] == -5.0,
      f"got {rows2[0]['today'] if rows2 else None}")

# ---------------------------------------------------------------- day_feature
check("放量上攻", cd.day_feature(1.0, 1.0, 1.0, 0.1, 30) == "放量上攻")
check("缩量回调", cd.day_feature(-1.0, -1.0, -1.0, -0.1, 10) == "缩量回调")
check("指数弱题材活跃", cd.day_feature(-0.2, -0.3, -0.2, -0.01, 70) == "指数弱、题材活跃")
check("窄幅震荡", cd.day_feature(0.1, -0.1, 0.0, 0.0, 10) == "窄幅震荡")

# ---------------------------------------------------------------- classify_flow
check("持续流入", build_data.classify_flow({"day5": 1, "day20": 1}) == "持续流入")
check("拐点回流", build_data.classify_flow({"day5": 1, "day20": -1}) == "拐点回流")
check("拐点撤退", build_data.classify_flow({"day5": -1, "day20": 1}) == "拐点撤退")
check("持续流出", build_data.classify_flow({"day5": -1, "day20": -1}) == "持续流出")

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
