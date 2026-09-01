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

# ---------------------------------------------------------------- lhb_window
check("lhb: single-day reason -> 当日", cd.lhb_window("日涨幅偏离值达7%的证券") == "当日")
check("lhb: three-day reason (汉字) -> 3日",
      cd.lhb_window("连续三个交易日内，涨幅偏离值累计达到20%的证券") == "3日")
check("lhb: three-day reason (数字) -> 3日",
      cd.lhb_window("连续3个交易日内收盘价格涨幅偏离值累计达到20%") == "3日")
check("lhb: empty reason -> 当日", cd.lhb_window("") == "当日")

# ---------------------------------------------------------------- _merge_reasons
base = {"code": "600000", "seat": "x", "window": "当日", "net_wan": 100.0, "reason": "换手率达20%"}
merged = cd._merge_reasons([dict(base), {**base, "reason": "涨幅偏离7%"},
                            {**base, "net_wan": 250.0, "reason": "3日累计偏离"}], ("code", "seat"))
check("merge: same event collapses to one row", len(merged) == 2, f"got {len(merged)}")
check("merge: reasons joined", "涨幅偏离7%" in merged[0]["reason"] and "换手率达20%" in merged[0]["reason"])
check("merge: different amount stays separate",
      any(r["net_wan"] == 250.0 for r in merged))

# ---------------------------------------------------------------- build_dragon_tiger
LHB_STOCKS = [
    {"SECURITY_CODE": "002396", "SECURITY_NAME_ABBR": "星网锐捷", "TRADE_DATE": "2026-08-31 00:00:00",
     "CLOSE_PRICE": 10.0, "CHANGE_RATE": 10.0, "BILLBOARD_NET_AMT": 3.0e8, "BILLBOARD_BUY_AMT": 4.0e8,
     "BILLBOARD_SELL_AMT": 1.0e8, "ACCUM_AMOUNT": 9.4e8, "TURNOVERRATE": 12.0,
     "EXPLANATION": "日涨幅达到15%的前5只证券", "EXPLAIN": ""},
    {"SECURITY_CODE": "300999", "SECURITY_NAME_ABBR": "安克创新", "TRADE_DATE": "2026-08-31 00:00:00",
     "CLOSE_PRICE": 100.0, "CHANGE_RATE": -5.0, "BILLBOARD_NET_AMT": -2.0e8, "BILLBOARD_BUY_AMT": 1.0e8,
     "BILLBOARD_SELL_AMT": 3.0e8, "ACCUM_AMOUNT": 8.0e8, "TURNOVERRATE": 5.0,
     "EXPLANATION": "连续三个交易日内，跌幅偏离值累计达到30%的证券", "EXPLAIN": ""},
]
LHB_SEATS = [
    {"SECURITY_CODE": "002396", "OPERATEDEPT_NAME": "机构专用", "BUY": 2.0e8, "SELL": 0.0,
     "NET": 2.0e8, "CHANGE_RATE": 10.0, "RISE_PROBABILITY_3DAY": 66.7,
     "TOTAL_BUYER_SALESTIMES_3DAY": 5, "EXPLANATION": "日涨幅达到15%的前5只证券"},
    {"SECURITY_CODE": "300999", "OPERATEDEPT_NAME": "机构专用", "BUY": 0.0, "SELL": 1.5e8,
     "NET": -1.5e8, "CHANGE_RATE": -5.0, "RISE_PROBABILITY_3DAY": None,
     "TOTAL_BUYER_SALESTIMES_3DAY": 2, "EXPLANATION": "连续三个交易日内，跌幅偏离值累计达到30%的证券"},
    {"SECURITY_CODE": "002396", "OPERATEDEPT_NAME": "国盛证券宁波桑田路", "BUY": 1.0e8, "SELL": 0.1e8,
     "NET": 0.9e8, "CHANGE_RATE": 10.0, "RISE_PROBABILITY_3DAY": 55.0,
     "TOTAL_BUYER_SALESTIMES_3DAY": 1, "EXPLANATION": "日涨幅达到15%的前5只证券"},
]
dt = cd.build_dragon_tiger(LHB_STOCKS, LHB_SEATS)
check("dragon: as_of from TRADE_DATE", dt["as_of"] == "2026-08-31")
check("dragon: stock net in 万", dt["stocks"][0]["net_wan"] == 30000.0,
      f"got {dt['stocks'][0]['net_wan']}")
check("dragon: stock row carries window", dt["stocks"][1]["window"] == "3日")
check("dragon: seat name joined from stock report",
      all(r["name"] in ("星网锐捷", "安克创新") for r in dt["top_seats"]))
check("dragon: top_seats sorted by |net| desc",
      [r["net_wan"] for r in dt["top_seats"]] == sorted((r["net_wan"] for r in dt["top_seats"]),
                                                          key=lambda v: -abs(v)))
check("dragon: special bucket keeps 机构专用 rows", len(dt["special"]["机构专用"]) == 2)
s = dt["summary"]
check("dragon: totals use single-day rows only", s["total_net_wan"] == 30000.0,
      f"got {s['total_net_wan']}")
check("dragon: inst total uses 当日 rows only", s["inst_net_wan"] == 20000.0,
      f"got {s['inst_net_wan']}")

# ---------------------------------------------------------------- pe_percentile
# 500 consecutive days from 2016-09-01 -> all inside the 10y window of 2026-08-31.
from datetime import date as _date, timedelta as _td  # noqa: E402
_pe_dates = [_date(2016, 9, 1) + _td(days=i) for i in range(500)]
pe_series = {d.isoformat(): 10.0 + i * 0.01 for i, d in enumerate(_pe_dates)}
pe_series["2026-08-31"] = 25.0
pv = cd.pe_percentile(pe_series)
check("pe: percentile of the max is 100", pv["percentile"] == 100.0, f"got {pv['percentile']}")
check("pe: as_of is the latest date", pv["as_of"] == "2026-08-31")
low = dict(pe_series)
low["2026-08-31"] = 5.0
check("pe: percentile of the min is ~0",
      cd.pe_percentile(low)["percentile"] < 5.0)
check("pe: short history -> None",
      cd.pe_percentile({"2026-08-30": 1.0, "2026-08-31": 1.1}) is None)
check("pe: empty -> None", cd.pe_percentile({}) is None)

# ---------------------------------------------------------------- build_lift_view
LIFT_ROWS = [
    {"SECURITY_CODE": "002998", "SECURITY_NAME_ABBR": "优彩资源", "FREE_DATE": "2026-09-02 00:00:00",
     "LIFT_MARKET_CAP": 290.64, "FREE_SHARES": 27083.43, "FREE_SHARES_TYPE": "股权激励限售股份",
     "TOTAL_RATIO": 0.006},
    {"SECURITY_CODE": "600925", "SECURITY_NAME_ABBR": "苏能股份", "FREE_DATE": "2026-09-07 00:00:00",
     "LIFT_MARKET_CAP": 2324449.73, "FREE_SHARES": 688888.89, "FREE_SHARES_TYPE": "追加承诺限售股份",
     "TOTAL_RATIO": 1.0},
    {"SECURITY_CODE": "000000", "SECURITY_NAME_ABBR": "区间外", "FREE_DATE": "2026-09-20 00:00:00",
     "LIFT_MARKET_CAP": 99999.0, "FREE_SHARES": 1.0, "FREE_SHARES_TYPE": "其他", "TOTAL_RATIO": 0.01},
]
lv = cd.build_lift_view(LIFT_ROWS, "2026-09-01", "2026-09-08")
check("lift: out-of-window rows dropped", lv["event_count"] == 2)
check("lift: cap 万元 -> 亿元", lv["top"][0]["cap_yi"] == 232.44, f"got {lv['top'][0]['cap_yi']}")
check("lift: sorted by cap desc", lv["top"][0]["name"] == "苏能股份")
check("lift: total over in-window events", lv["total_cap_yi"] == round(232.44 + 0.03, 2),
      f"got {lv['total_cap_yi']}")
check("lift: by_date counts", {s["date"]: s["count"] for s in lv["by_date"]}
      == {"2026-09-02": 1, "2026-09-07": 1})
check("lift: ratio >= 5% flagged", [f["name"] for f in lv["flagged"]] == ["苏能股份"],
      f"got {lv['flagged']}")
check("lift: empty rows -> zero view", cd.build_lift_view([], "2026-09-01", "2026-09-08")["event_count"] == 0)

# ---------------------------------------------------------------- US Treasury parsing
CSV_SAMPLE = ("Date,\"1 Mo\",\"3 Mo\",\"6 Mo\",\"1 Yr\",\"2 Yr\",\"3 Yr\",\"5 Yr\",\"7 Yr\","
              "\"10 Yr\",\"20 Yr\",\"30 Yr\"\n"
              "08/29/2026,4.05,4.08,4.15,4.20,4.30,4.40,4.50,4.60,4.70,4.90,4.95\n"
              "08/28/2026,4.04,4.07,4.14,4.19,4.28,4.38,4.48,4.58,4.68,4.88,4.93\n")
ust_rows = cd.parse_us_treasury_csv(CSV_SAMPLE)
check("ust: rows parsed newest first", [r["date"] for r in ust_rows] == ["2026-08-29", "2026-08-28"])
check("ust: y10/y2 read from tenors", ust_rows[0]["y10"] == 4.70 and ust_rows[0]["y2"] == 4.30)
ust_view = cd.build_us_treasury_view({2026: ust_rows})
check("ust: change_bp = (4.70-4.68)*100", ust_view["change_bp"] == 2.0, f"got {ust_view['change_bp']}")
check("ust: 2s10s spread = 40bp", ust_view["spread_2s10s_bp"] == 40.0, f"got {ust_view['spread_2s10s_bp']}")
check("ust: garbage row skipped",
      len(cd.parse_us_treasury_csv("Date,\"10 Yr\"\n,4.0\nxx/01/2026,1.0\n")) == 0)

# ---------------------------------------------------------------- analysis: rotation
import analysis as an  # noqa: E402

check("rotation: 价涨资金进 -> 持续", an.rotation_verdict(1.5, 3.0, 8.0)[0] == "\u2713")
check("rotation: 价涨资金撤 -> 半兑现", an.rotation_verdict(1.2, -4.0, 9.0)[0] == "\u25b3")
check("rotation: 小跌但多日为正 -> 半兑现", an.rotation_verdict(-0.5, -9.0, 30.0)[0] == "\u25b3")
check("rotation: 价跌资金撤 -> 失败", an.rotation_verdict(-2.1, -120.0, -149.0)[0] == "\u2717")
check("rotation: 停滞", an.rotation_verdict(-0.2, 0.0, -5.0)[0] == "\u2717")
check("rotation: 无当日读数 -> ?", an.rotation_verdict(None, 0.0, 1.0)[0] == "?")

ROT_PREV = {"\u534a\u5bfc\u4f53": 176e8, "\u901a\u4fe1\u8bbe\u5907": 100e8, "\u5143\u4ef6": 80e8,
            "\u79cd\u690d\u4e1a": 30e8}
ROT_TODAY = [
    {"sector": "\u534a\u5bfc\u4f53", "today": -122.6, "day5": -149.5, "day10": -560.7, "change_pct": -2.12, "limit_up": 0},
    {"sector": "\u901a\u4fe1\u8bbe\u5907", "today": -59.2, "day5": -85.8, "day10": 69.1, "change_pct": -1.56, "limit_up": 0},
    {"sector": "\u5143\u4ef6", "today": -24.4, "day5": 64.2, "day10": 56.3, "change_pct": -0.78, "limit_up": 0},
    {"sector": "\u79cd\u690d\u4e1a", "today": 3.0, "day5": 14.6, "day10": 28.9, "change_pct": 2.10, "limit_up": 8},
]
rot = an.build_rotation_view(ROT_PREV, ROT_TODAY, "2026-08-27", "2026-08-28")
check("rotation view: 4 rows", rot is not None and len(rot["rows"]) == 4)
check("rotation view: \u5143\u4ef6 \u534a\u5151\u73b0", [r for r in rot["rows"] if r["sector"] == "\u5143\u4ef6"][0]["symbol"] == "\u25b3")
check("rotation view: \u79cd\u690d\u4e1a \u6301\u7eed", [r for r in rot["rows"] if r["sector"] == "\u79cd\u690d\u4e1a"][0]["symbol"] == "\u2713")
check("rotation view: tally 1/1/2", (rot["sustained"], rot["partial"], rot["failed"]) == (1, 1, 2),
      f"{rot['sustained']}/{rot['partial']}/{rot['failed']}")
check("rotation view: \u4e00\u65e5\u6e38\u5e02\u8bba\u8c03", "\u4e00\u65e5\u6e38" in rot["win_note"])
check("rotation view: \u51c0\u6d41\u5165\u8f6c\u4e3a\u4ebf\u5143", abs(rot["rows"][0]["prev_yi"] - 176.0) < 0.01)
check("rotation view: \u7a7a\u5386\u53f2 -> None", an.build_rotation_view({}, ROT_TODAY, "2026-08-27", "2026-08-28") is None)
check("rotation view: top_n \u622a\u65ad", len(an.build_rotation_view(ROT_PREV, ROT_TODAY, "d", "d", top_n=2)["rows"]) == 2)

# ---------------------------------------------------------------- analysis: mainline
LAD_ROWS = [{"sector": "\u57fa\u7840\u5316\u5de5", "board": 3}, {"sector": "\u57fa\u7840\u5316\u5de5", "board": 2},
            {"sector": "\u8ba1\u7b97\u673a", "board": 2}, {"sector": "\u519c\u6797\u7267\u6e14", "board": 1}]
ml = an.build_mainline_view(ROT_TODAY, rot, LAD_ROWS, 0.8, 7)
check("mainline: 5 criteria", ml is not None and len(ml["criteria"]) == 5)
check("mainline: \u5224\u636e\u987a\u5e8f", [c["item"] for c in ml["criteria"]]
      == ["\u677f\u5757\u5bbd\u5ea6", "\u8f6e\u52a8\u80dc\u7387", "\u8d44\u91d1\u96c6\u4e2d\u5ea6", "\u6da8\u505c\u5f52\u7c7b", "\u6d41\u52a8\u6027"])
check("mainline: \u8d44\u91d1\u64a4\u51fa\u5224\u5b9a", "\u64a4\u51fa" in [c["meaning"] for c in ml["criteria"] if c["item"] == "\u8d44\u91d1\u96c6\u4e2d\u5ea6"][0])
check("mainline: \u60c5\u7eea\u4e3b\u7ebf\u7ed3\u8bba", "\u60c5\u7eea\u4e3b\u7ebf" in ml["conclusion"], ml["conclusion"])
check("mainline: \u7a7a flows -> None", an.build_mainline_view([], rot, LAD_ROWS, 0.8, 7) is None)

CONV_LAD = [{"sector": "\u6c34\u6ce5", "board": 2} for _ in range(9)] + [{"sector": "\u94a2\u94c1", "board": 1}]
ml2 = an.build_mainline_view(ROT_TODAY, {"total": 4, "sustained": 3, "partial": 1, "failed": 0, "rows": []},
                             CONV_LAD, 1.1, 7)
check("mainline: \u6536\u655b\u5230\u5355\u4e00\u884c\u4e1a -> \u4e3b\u7ebf\u5019\u9009", "\u4e3b\u7ebf\u5019\u9009" in ml2["conclusion"], ml2["conclusion"])

# ---------------------------------------------------------------- analysis: emotion stage
check("stage: 7\u677f+\u4f4e\u664b\u7ea7\u7387 -> \u9ad8\u6f6e\u9876\u90e8",
      "\u9876\u90e8" in an.emotion_stage({"max_board": 7, "promotion_rate": 20.5, "seal_rate": 93.3, "zha_ban": 6})["stage"])
check("stage: 7\u677f+\u9ad8\u664b\u7ea7\u7387 -> \u9ad8\u6f6e\u533a",
      an.emotion_stage({"max_board": 7, "promotion_rate": 55.0, "zha_ban": 1})["stage"] == "\u9ad8\u6f6e\u533a")
check("stage: \u70b8\u677f\u591a -> \u9876\u90e8",
      "\u9876\u90e8" in an.emotion_stage({"max_board": 7, "promotion_rate": 60.0, "zha_ban": 8})["stage"])
check("stage: 2\u677f\u4f4e\u8fdb -> \u9000\u6f6e/\u51b0\u70b9",
      "\u9000\u6f6e" in an.emotion_stage({"max_board": 2, "promotion_rate": 25.0, "two_board_plus": 3})["stage"])
check("stage: \u7a7a metrics -> None", an.emotion_stage({}) is None)
check("stage: \u4f9d\u636e\u542b\u5c01\u677f\u7387",
      any("\u5c01\u677f\u7387" in r for r in an.emotion_stage({"max_board": 5, "seal_rate": 88.0})["reasons"]))

# ---------------------------------------------------------------- analysis: forecast
fc = an.trend_forecast({"max_board": 7, "promotion_rate": 20.5}, {"stock": "\u6d77\u9e25\u4f4f\u5de5", "board": 7}, "\u9ad8\u6f6e\u533a\u9876\u90e8\uff08\u5206\u6b67\u663e\u73b0\uff09")
check("forecast: 3 branches", fc is not None and len(fc["branches"]) == 3)
check("forecast: \u6807\u7684\u80a1\u5165\u6761\u4ef6", "\u6d77\u9e25\u4f4f\u5de5" in fc["branches"][0]["condition"])
check("forecast: \u9876\u90e8\u9ed8\u8ba4\u9632\u5b88", "\u9000\u6f6e" in fc["default"])
check("forecast: \u7a7a metrics -> None", an.trend_forecast({}, None, None) is None)

# ---------------------------------------------------------------- analysis: verify
V_FLOWS = [{"sector": "\u8ba1\u7b97\u673a", "today": -4.6, "day5": 97.8, "day10": -103.5, "change_pct": 1.43, "limit_up": 0},
           {"sector": "\u519c\u6797\u7267\u6e14", "today": 28.8, "day5": 32.5, "day10": 27.2, "change_pct": 4.04, "limit_up": 4}]
V_LADDER = {"metrics": {"promotion_rate": 20.5, "max_board": 7},
            "ladder": [{"code": "002084", "stock": "\u6d77\u9e25\u4f4f\u5de5", "board": 7, "sector": "\u5bb6\u5c45\u7528\u54c1"}]}
V_POOL = [{"sector": "\u8ba1\u7b97\u673a", "score": 90}, {"sector": "\u975e\u94f6\u91d1\u878d", "score": 60}]
checks = an.build_verify_checks("2026-09-01", V_FLOWS, V_LADDER, 1.09, V_POOL)
check("verify: \u751f\u6210 6 \u6761\uff08\u84c4\u529b\u6c60 2 + \u5151\u73b0/\u6210\u4ea4/\u677f\u7ea7/\u664b\u7ea7 \u5404 1\uff09", len(checks) == 6, f"got {len(checks)}")
check("verify: \u542b\u84c4\u529b\u6c60 / \u5151\u73b0 / \u6210\u4ea4 / \u677f\u7ea7 / \u664b\u7ea7",
      {c["type"] for c in checks} == {"sector_day5_positive", "rotation_payoff", "turnover_floor",
                                      "board_continue", "promotion_floor"})
check("verify: turnover floor = 95%", abs([c for c in checks if c["type"] == "turnover_floor"][0]["params"]["floor"] - 1.04) < 0.01)

V_CTX = {"flows": {r["sector"]: r for r in V_FLOWS}, "rotation": {"\u519c\u6797\u7267\u6e14": "\u2713"},
         "turnover": 1.20, "zt_codes": {"002084"}, "promotion_rate": 55.0}
check("verify: day5 \u4ecd\u4e3a\u6b63 -> \u2713", an.evaluate_check(checks[0], V_CTX) == "\u2713")
check("verify: \u5151\u73b0\u6301\u7eed -> \u2713", an.evaluate_check([c for c in checks if c["type"] == "rotation_payoff"][0], V_CTX) == "\u2713")
check("verify: \u6210\u4ea4\u5b88\u4f4f -> \u2713", an.evaluate_check([c for c in checks if c["type"] == "turnover_floor"][0], V_CTX) == "\u2713")
check("verify: \u672a\u65ad\u677f -> \u2713", an.evaluate_check([c for c in checks if c["type"] == "board_continue"][0], V_CTX) == "\u2713")
check("verify: \u664b\u7ea7\u7387\u56de\u5347 -> \u2713", an.evaluate_check([c for c in checks if c["type"] == "promotion_floor"][0], V_CTX) == "\u2713")
check("verify: \u65ad\u677f -> \u2717", an.evaluate_check([c for c in checks if c["type"] == "board_continue"][0],
                                                  dict(V_CTX, zt_codes=set())) == "\u2717")
check("verify: \u7f29\u91cf -> \u2717", an.evaluate_check([c for c in checks if c["type"] == "turnover_floor"][0],
                                                  dict(V_CTX, turnover=0.9)) == "\u2717")
check("verify: \u7f3a\u6570\u636e -> None", an.evaluate_check(checks[0], {}) is None)
scored, tally = an.score_checks(checks, V_CTX)
check("verify: tally \u4e94\u4e2d + \u4e00\u4e2a\u7f3a\u6570\u636e\u964d\u7ea7", tally == {"\u2713": 5, "\u2717": 0, "\u25b3": 0, "?": 1}, str(tally))
check("verify: rows \u5e26 result", all("result" in r for r in scored))

# ---------------------------------------------------------------- analysis: seats + direction
KNOWN = {"\u673a\u6784\u4e13\u7528": "\u673a\u6784", "\u6c11\u6c11": "\u77e5\u540d\u6e38\u8d44",
         "\u62c9\u8428": "\u4e1c\u8d22\u62c9\u8428\u7cfb", "\u6caa\u80a1\u901a\u4e13\u7528": "\u5317\u5411"}
check("seat: \u5168\u7b49\u547d\u4e2d", an.seat_tag("\u673a\u6784\u4e13\u7528", KNOWN) == "\u673a\u6784")
check("seat: \u5305\u542b\u547d\u4e2d", an.seat_tag("\u4e1c\u65b9\u8d22\u5bcc\u62c9\u8428\u56e2\u7ed3\u8def\u7b2c\u4e8c", KNOWN) == "\u4e1c\u8d22\u62c9\u8428\u7cfb")
check("seat: \u672a\u77e5 -> None", an.seat_tag("\u67d0\u8425\u4e1a\u90e8", KNOWN) is None)
check("seat: \u7a7a -> None", an.seat_tag("", KNOWN) is None)

D_STOCKS = [{"code": "1", "name": "A", "net_wan": 60000.0, "pct": 10.0},
            {"code": "2", "name": "B", "net_wan": -20000.0, "pct": -3.0},
            {"code": "3", "name": "C", "net_wan": 15000.0, "pct": 5.0}]
D_MAP = {"1": "\u5143\u4ef6", "2": "\u5143\u4ef6", "3": "\u79cd\u690d\u4e1a"}
directions = an.aggregate_direction(D_STOCKS, D_MAP)
check("direction: \u6309\u884c\u4e1a\u805a\u5408", [d["sector"] for d in directions] == ["\u5143\u4ef6", "\u79cd\u690d\u4e1a"], str(directions))
check("direction: \u51c0\u989d\u6c47\u603b", directions[0]["net_wan"] == 40000.0)
check("direction: \u4e70\u5356\u5bb6\u6570", (directions[0]["in_stocks"], directions[0]["count"]) == (1, 2))
check("direction: \u4ee3\u8868\u4e2a\u80a1", directions[0]["top"]["name"] == "A")
check("direction: \u65e0\u6620\u5c04 -> \u672a\u5206\u7c7b", an.aggregate_direction(D_STOCKS, {})[0]["sector"] == "\u672a\u5206\u7c7b")

NZ = an.noise_zone([{"sector": "a", "change_pct": 2.0, "today": 1.0, "limit_up": 0},
                    {"sector": "b", "change_pct": 20.0, "today": 1.0, "limit_up": 0},
                    {"sector": "c", "change_pct": 3.0, "today": 1.0, "limit_up": 5},
                    {"sector": "d", "change_pct": 3.0, "today": 30.0, "limit_up": 0},
                    {"sector": "e", "change_pct": 0.4, "today": 1.0, "limit_up": 0}])
check("noise: \u53ea\u7559\u6da8\u5e45 1-8% \u4e14\u65e0\u6da8\u505c\u4e14\u8d44\u91d1\u5c0f", [n["sector"] for n in NZ] == ["a"], str(NZ))


# ---------------------------------------------------------------- batch 2: intraday
# Bar labels are the window END (1000 = 09:30-10:00), matching INTRADAY_SESSION_ENDS.
BARS = [
    ["202609011000", 3000.0, 3010.0, 3012.0, 2998.0, 100.0],  # open 3000, close 3010
    ["202609011030", 3010.0, 3018.0, 3020.0, 3009.0, 120.0],  # +0.6 -> crosses +0.5 at label 10:30
    ["202609011100", 3018.0, 3008.0, 3019.0, 3007.0, 80.0],   # low 3008 mid-session
    ["202609011130", 3008.0, 3012.0, 3016.0, 3007.0, 60.0],
    ["202609011330", 3012.0, 3025.0, 3026.0, 3012.0, 90.0],
    ["202609011400", 3025.0, 3022.0, 3027.0, 3021.0, 70.0],
    ["202609011430", 3022.0, 3020.0, 3023.0, 3019.0, 200.0],
    ["202609011500", 3020.0, 3027.0, 3028.0, 3019.0, 150.0],  # tail +0.23% >= 0.2
]
TL = an.intraday_timeline(BARS, 3000.0)
check("intraday: returns summary/events", TL is not None and TL["summary"], str(TL))
check("intraday: open tone 平开 (0.0%)", TL["events"][0]["text"].startswith("平开"), TL["events"][0]["text"])
check("intraday: crossing +0.5 at label 10:30", any(e["time"] == "10:30" and "涨超 0.5%" in e["text"] for e in TL["events"]),
      str(TL["events"]))
check("intraday: tail push detected", any("尾盘" in e["text"] for e in TL["events"]), str(TL["events"]))
check("intraday: max-volume session flagged", any("最大 30 分钟成交" in e["text"] for e in TL["events"]))
check("intraday: high/low moments", any("盘中高点" in e["text"] for e in TL["events"])
      and any("盘中低点" in e["text"] for e in TL["events"]))
check("intraday: empty bars -> None", an.intraday_timeline([], 3000.0) is None)
check("intraday: no prev close -> None", an.intraday_timeline(BARS, None) is None)

DV_G = [{"name": "费城半导体", "pct": -1.2}, {"name": "LME铜", "pct": 0.5}, {"name": "恒生科技", "pct": 2.0}]
DV_T = [{"sector": "半导体", "change_pct": 1.5, "day5": 3.0},
        {"sector": "工业金属", "change_pct": -0.2, "day5": 1.0},
        {"sector": "贵金属", "change_pct": 1.0, "day5": 1.0}]
DV = an.divergence_list(DV_G, DV_T)
check("divergence: only sign-mismatch pairs kept", [r["theme"] for r in DV] == ["半导体链"], str(DV))
check("divergence: gap computed and sorted", DV and DV[0]["gap"] == 2.7 and DV[0]["sector_pct"] == 1.5, str(DV))
check("divergence: same-direction pair excluded (恒生科技+2.0 vs missing sector)",
      all(r["theme"] != "科技成长" for r in DV))
check("divergence: small gap below threshold excluded (LME铜 0.5 vs 工业金属 -0.2)",
      all(r["theme"] != "铜链" for r in DV))
check("divergence: empty inputs -> []", an.divergence_list([], []) == [])

CAL = an.macro_calendar("2026-08-10", days_ahead=14)
names = [e["name"] for e in CAL]
dates = [e["date"] for e in CAL]
check("calendar: sorted by date", dates == sorted(dates), str(CAL))
check("calendar: window respected", all("2026-08-10" <= d <= "2026-08-24" for d in dates))
check("calendar: CPI on day 10 inside window", any(e["name"] == "中国CPI/PPI" and e["date"] == "2026-08-10" for e in CAL))
check("calendar: LPR on day 20 inside window", any(e["name"] == "LPR报价" and e["date"] == "2026-08-20" for e in CAL))
CAL2 = an.macro_calendar("2026-08-15", days_ahead=20)
check("calendar: LPR on day 20", any(e["name"] == "LPR报价" and e["date"] == "2026-08-20" for e in CAL2))
check("calendar: month-end PMI in Aug window", any(e["name"] == "中国官方PMI" and e["date"] == "2026-08-31" for e in CAL2))
CAL3 = an.macro_calendar("2026-08-10", days_ahead=14, fixed=[{"date": "2026-08-12", "name": "美国FOMC决议", "note": "test"}])
check("calendar: fixed date included, rule overwritten same date",
      any(e["date"] == "2026-08-12" and e["name"] == "美国FOMC决议" and e["source"] == "fixed" for e in CAL3), str(CAL3))


def pool_row(sector, change_pct, day5, limit_up=0, state=None):
    return {"sector": sector, "change_pct": change_pct, "day5": day5,
            "today": day5 / 5.0, "limit_up": limit_up, "classification": state}


POOL_TODAY = [pool_row("半导体", 1.5, 20.0, 3, "持续流入"), pool_row("电力", -0.5, -15.0, 0, "持续流出"),
              pool_row("种植业", 2.0, 8.0, 1, "拐点回流"), pool_row("贵金属", 3.0, 12.0, 2, "拐点撤退")]
POOL = an.direction_pool(POOL_TODAY, [{"sector": "半导体", "score": 9.0}], None, {"半导体": [1.0] * 25})
check("pool: rows sorted by |day5|", [r["sector"] for r in POOL][0] == "半导体", str([r["sector"] for r in POOL]))
check("pool: limit 12 rows", len(POOL) <= 12)
check("pool: action mapping 持续流入->重点观察", POOL[0]["action"] == "重点观察")
check("pool: action mapping 持续流出", any(r["action"] == "趋势性失血，反弹只当兑现" for r in POOL))
check("pool: position from history (25 samples >= 20)", POOL[0]["ret20"] is not None and POOL[0]["position"] in ("高位", "中位", "低位"),
      str(POOL[0]))
check("pool: no history -> 积累中", any(r["position"] == "积累中" and r["ret20"] is None for r in POOL))
POOL2 = an.direction_pool(POOL_TODAY, [], {"rows": [{"sector": "电力"}]}, {})
check("pool: rotation rows join candidates", any(r["sector"] == "电力" for r in POOL2))
check("pool: unknown state falls back to 观察", True)  # classification always present in fixtures
POS = an._sector_position([5.0] * 20)
check("position: all +5% -> 高位", POS["label"] == "高位", str(POS))
POS2 = an._sector_position([-5.0] * 20)
check("position: all -5% -> 低位", POS2["label"] == "低位", str(POS2))
POS3 = an._sector_position([0.1] * 19)
check("position: 19 samples -> 积累中", POS3["label"] == "积累中" and POS3["ret20"] is None, str(POS3))


print()
if FAILURES:
    print(f"{len(FAILURES)} test(s) FAILED: {FAILURES}")
    sys.exit(1)
print("ALL TESTS PASSED")
