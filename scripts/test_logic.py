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
check("build(sample): 4 verdicts (定性/资金/情绪/风险)", len(payload["verdicts"]) == 4,
      f"got {[v['tag'] for v in payload['verdicts']]}")
check("build(sample): 情绪结构 verdict second to last", payload["verdicts"][2]["tag"] == "情绪结构")
check("build(sample): pools non-empty", len(payload["accumulation_pool"]) > 0)
check("build(sample): flows classified",
      all("classification" in f for f in payload["flows"]))
check("build(sample): index_panel 7 rows", len(payload["index_panel"]) == 7)
check("build(sample): style 小盘占优", payload["style"]["size_label"] == "小盘占优")
check("build(sample): ladder rows", len(payload["limit_ladder"]["ladder"]) == 8)
check("build(sample): margin as_of", payload["margin"]["as_of"] == "2026-08-27")
check("build(sample): global rows", len(payload["global_markets"]) == 17)

# degradation path: real snapshot with empty flows beats demo sample
real = {"meta": {"market_date": "2026-08-28", "demo": False, "sources": [], "updated_at": "x"},
        "market_days": sample["market_days"], "breadth": sample["breadth"],
        "flows": [], "events": [], "scenarios": [], "risk_notes": []}
real["_file"] = "eod.json"
chosen = build_data.build([sample, real])
check("real snapshot beats newer demo", chosen["meta"]["input_file"] == "eod.json")
check("empty flows -> degraded verdicts", len(chosen["verdicts"]) == 2
      and chosen["verdicts"][1]["tag"] == "数据缺口")

# degradation path: a legacy snapshot without the four new blocks must still build
check("legacy snapshot: style None", chosen["style"] is None)
check("legacy snapshot: ladder empty", chosen["limit_ladder"] == {})
check("legacy snapshot: margin None", chosen["margin"] is None)
check("legacy snapshot: global empty", chosen["global_markets"] == [])
check("legacy snapshot: index_panel empty", chosen["index_panel"] == [])

# ---------------------------------------------------------------- window_returns
closes = [100.0 + i for i in range(70)]          # 100 .. 169
w = cd.window_returns(closes)
check("ret5 = 169/164-1", w["ret5"] == 3.05, f"got {w['ret5']}")
check("ret20 = 169/149-1", w["ret20"] == 13.42, f"got {w['ret20']}")
check("ret60 = 169/109-1", w["ret60"] == 55.05, f"got {w['ret60']}")
w_short = cd.window_returns([1.0, 2.0, 3.0])
check("short history -> all None", w_short["ret5"] is None and w_short["ret60"] is None)
check("flat series -> 0.0", cd.window_returns([10.0] * 70)["ret60"] == 0.0)

# ---------------------------------------------------------------- build_index_panel
klines = {
    "shanghai": [{"date": f"d{i}", "close": 100.0 + i, "pct": 0.5} for i in range(70)],
    "hs300": [{"date": "d69", "close": 4000.0, "pct": -0.3}],
    "zz1000": [{"date": "d69", "close": 6000.0, "pct": 0.2}],
}
panel = cd.build_index_panel(klines)
by_key = {r["key"]: r for r in panel}
check("index_panel: keys and order", list(by_key) == ["shanghai", "hs300", "zz1000"],
      f"got {list(by_key)}")
check("index_panel: display name", by_key["shanghai"]["name"] == "上证指数")
check("index_panel: latest close", by_key["shanghai"]["close"] == 169.0)
check("index_panel: 1-bar series -> null returns", by_key["hs300"]["ret60"] is None)
check("index_panel: unknown key skipped", "chinext" not in by_key)

# ---------------------------------------------------------------- build_limit_ladder
ZT = [
    {"c": "000001", "n": "甲", "hybk": "银行", "lbc": 3, "ltsz": 1e10, "fund": 5e8,
     "amount": 1e9, "hs": 5.0, "zbc": 1, "zttj": {"days": 5, "ct": 3}},
    {"c": "000002", "n": "乙", "hybk": "证券", "lbc": 1, "ltsz": 1e10, "fund": 1e8,
     "amount": 2e9, "hs": 8.0, "zbc": 0, "zttj": {"days": 1, "ct": 1}},
    {"c": "000003", "n": "丙", "hybk": "保险", "lbc": 3, "ltsz": 1e10, "fund": 9e8,
     "amount": 3e9, "hs": 2.0, "zbc": 0, "zttj": {"days": 3, "ct": 3}},
]
lad = cd.build_limit_ladder(ZT, [{"c": "000009"}, {"c": "000008"}],
                           ["000002", "000003", "000007"], "2026-08-31", limit_down=11)
m = lad["metrics"]
check("ladder: limit_up from pool", m["limit_up"] == 3)
check("ladder: zha_ban from ZB pool", m["zha_ban"] == 2)
check("ladder: seal_rate 3/(3+2)", m["seal_rate"] == 60.0, f"got {m['seal_rate']}")
check("ladder: promoted 2 of 3", m["promoted"] == 2 and m["prev_limit_up"] == 3)
check("ladder: promotion_rate 66.7", m["promotion_rate"] == 66.7, f"got {m['promotion_rate']}")
check("ladder: max_board 3", m["max_board"] == 3)
check("ladder: two_board_plus 2", m["two_board_plus"] == 2)
check("ladder: sorted by board then seal", [r["stock"] for r in lad["ladder"]] == ["丙", "甲", "乙"],
      f"got {[r['stock'] for r in lad['ladder']]}")
check("ladder: distribution", lad["distribution"] == {"3": 2, "1": 1}, f"got {lad['distribution']}")
check("ladder: note 5天3板 vs 3连板", lad["ladder"][1]["note"] == "5天3板"
      and lad["ladder"][0]["note"] == "3连板")
check("ladder: seal_ratio = fund/ltsz", lad["ladder"][0]["seal_ratio"] == 9.0)
check("ladder: no prev codes -> promotion None",
      cd.build_limit_ladder(ZT, [], [], "2026-08-31")["metrics"]["promotion_rate"] is None)
check("ladder: empty pools -> seal_rate None",
      cd.build_limit_ladder([], [], [], "2026-08-31")["metrics"]["seal_rate"] is None)

# ---------------------------------------------------------------- build_margin_view
MARGIN_ROWS = [
    {"DIM_DATE": "2026-08-28 00:00:00", "RZRQYE": 2659110706352, "RZYE": 2630249928496,
     "RQYE": 28860777856, "RZJME": -6159230634, "RZYEZB": 2.616041},
    {"DIM_DATE": "2026-08-27 00:00:00", "RZRQYE": 2665116786778, "RZYE": 2636409159125,
     "RQYE": 28707627653, "RZJME": 11355223247, "RZYEZB": 2.616376},
]
mv = cd.build_margin_view(MARGIN_ROWS)
check("margin: balance in 亿元", mv["balance"] == 26591.11, f"got {mv['balance']}")
check("margin: change vs previous row", mv["change"] == -60.06, f"got {mv['change']}")
check("margin: as_of from DIM_DATE", mv["as_of"] == "2026-08-28")
check("margin: pct_of_float", mv["pct_of_float"] == 2.62, f"got {mv['pct_of_float']}")
check("margin: single row -> change None",
      cd.build_margin_view(MARGIN_ROWS[:1])["change"] is None)

# ---------------------------------------------------------------- _sina_row
check("sina gb pct", cd._sina_row("费交所半导体股指数,11489.79,0.18,2026-09-01 00:35:15", "gb")["pct"] == 0.18)
hf = cd._sina_row("4433.10,4454.230,4433.10,4433.45,4471.76,4396.39,00:37:00,4454.23,4432.24,0,0,0,2026-09-01,伦敦金", "hf")
check("sina hf pct from 昨收", abs(hf["pct"] + 0.47) < 0.01, f"got {hf['pct']}")
check("sina hf as_of", hf["as_of"] == "2026-09-01")
check("sina fx pct", cd._sina_row(
    "00:31:55,6.719100,6.719200,6.730200,149,6.731200,6.732100,6.717200,6.719100,"
    "离岸人民币（香港）,-0.160000,-0.011100,0.002214,,6.995700,6.715000,,2026-09-01", "fx")["pct"] == -0.16)
check("sina missing -> None", cd._sina_row(None, "hf")["close"] is None)

# ---------------------------------------------------------------- build_global_rows
TX = ["100", "恒生科技指数", "HSTECH", "4619.87"] + ["0"] * 26 + ["2026/08/31 16:09:08", "14.72", "0.32"]
grows = cd.build_global_rows(
    {"DJIA": {"f2": 53206.85, "f3": -0.66}, "HSI": {"f2": 25566.99, "f3": -0.07}},
    {"gb_$sox": "费交所半导体股指数,11489.79,0.18,2026-09-01 00:35:15"},
    {"hkHSTECH": TX})
gmap = {r["name"]: r for r in grows}
check("global: row count matches spec", len(grows) == 17, f"got {len(grows)}")
check("global: EM row parsed", gmap["道琼斯"]["pct"] == -0.66 and gmap["道琼斯"]["close"] == 53206.85)
check("global: absent EM symbol -> None", gmap["日经225"]["close"] is None)
check("global: sina row parsed", gmap["费城半导体"]["pct"] == 0.18)
check("global: tencent row parsed", gmap["恒生科技"]["pct"] == 0.32)

# ---------------------------------------------------------------- global session state
from datetime import datetime as _dt  # noqa: E402
check("US session: 00:40 next day -> intraday",
      cd.global_session_state("2026-08-31", _dt(2026, 9, 1, 0, 40)) == "intraday")
check("US session: 06:00 next day -> closed",
      cd.global_session_state("2026-08-31", _dt(2026, 9, 1, 6, 0)) == "closed")
check("US session: boundary 05:00 -> closed",
      cd.global_session_state("2026-08-31", _dt(2026, 9, 1, 5, 0)) == "closed")
check("US session: 04:59 -> intraday",
      cd.global_session_state("2026-08-31", _dt(2026, 9, 1, 4, 59)) == "intraday")

# ---------------------------------------------------------------- style_view
sv = build_data.style_view([{"key": "hs300", "ret20": 0.50}, {"key": "zz1000", "ret20": 8.90},
                            {"key": "zzdiv", "ret20": -1.20}])
check("style: size_edge 8.4", sv["size_edge"] == 8.4, f"got {sv['size_edge']}")
check("style: value_edge -1.7", sv["value_edge"] == -1.7, f"got {sv['value_edge']}")
check("style: labels", sv["size_label"] == "小盘占优" and sv["value_label"] == "成长占优")
check("style: note carries the numbers", "8.90" in sv["note"] and "沪深300" in sv["note"])
flat = build_data.style_view([{"key": "hs300", "ret20": 1.0}, {"key": "zz1000", "ret20": 1.2},
                              {"key": "zzdiv", "ret20": 1.1}])
check("style: below threshold -> 不显著",
      flat["size_label"] == "差异不显著" and flat["value_label"] == "差异不显著")
check("style: missing panel -> None", build_data.style_view([]) is None)
check("style: partial panel -> None", build_data.style_view([{"key": "hs300", "ret20": 1.0}]) is None)

print()
if FAILURES:
    print(f"{len(FAILURES)} test(s) FAILED: {FAILURES}")
    sys.exit(1)
print("ALL TESTS PASSED")
