#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect real EOD market data for the decision card (incremental, multi-source).

Request budget (steady state, once per trading day):

    Tencent fqkline (7 index klines)      7 requests
    CSIndex official (SH turnover)        1 request   (range query)
    SZSE official (SZ turnover)           1 request   (today only; history cached)
    Eastmoney push2ex (ZT/DT/ZB pools)    3 requests  (today only; history cached)
    Sina hs_a list (breadth/closes)     ~56 requests  (paged at 100, 0.12s apart)
    Eastmoney sector flow rank           ~5 requests  (paged at 100; push2 -> push2delay)
    Eastmoney datacenter (margin)         1 request   (T+1 disclosure, whole market)
    Eastmoney push2delay (global batch)   1 request   (10 markets in one call)
    Sina hq.sinajs.cn (sox/metals/fx)     1 request   (batched)
    Tencent qt.gtimg.cn (HSTECH)          1 request
    US Treasury.gov (yield curve csv)     1 request   (official, no key)
    Eastmoney datacenter (龙虎榜)          2 requests  (84 stocks + 420 seats,
                                                       pageSize=500 each)
    CSIndex index-perf (PE percentiles)    3 requests  (10y history cached,
                                                       then 0-2 rows/day each)
    Eastmoney datacenter (解禁时间表)      1 request  (next 7 days)

    Eastmoney total: ~10 requests/day (was ~97 in the naive design, which
    triggered IP-level throttling within minutes).

Blocks added later (limit ladder / index panel / margin / global markets) stay
on push2delay + datacenter-web + Tencent + Sina. Only the sector-flow ranking
ever touches push2, and it falls back to push2delay automatically.

The sector-flow ranking tries the realtime cluster (push2) first and falls
back to the delayed cluster (push2delay) - after market close the values are
identical, and the delayed cluster sits outside the IP-throttling applied to
push2/push2his.

Caches under data/cache/:
    daily_stats.json          per-date ZT/DT counts + SZ turnover
    sector_flow_history.json  per-sector daily main-force net inflow (yuan;
                              raw material for longer custom windows later)
    breadth_history.json      per-date close prices (5d new-high/low)

Every Eastmoney call degrades gracefully: the snapshot is still emitted with
an explicit note.

Standard library only. Run:  python -X utf8 scripts/collect_data.py
"""
from __future__ import annotations

import csv
import io
import json
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
CACHE_DIR = ROOT / "data" / "cache"
DAILY_STATS_CACHE = CACHE_DIR / "daily_stats.json"
SECTOR_FLOW_CACHE = CACHE_DIR / "sector_flow_history.json"
BREADTH_CACHE = CACHE_DIR / "breadth_history.json"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# (tencent symbol, internal key, display name). The first three are also the
# ones the five-day table renders; all seven feed the index panel.
TENCENT_INDEXES = [
    ("sh000001", "shanghai", "上证指数"),
    ("sz399001", "szcheng", "深证成指"),
    ("sz399006", "chinext", "创业板指"),
    ("sh000688", "star50", "科创50"),
    ("sh000300", "hs300", "沪深300"),
    ("sh000852", "zz1000", "中证1000"),
    ("sh000922", "zzdiv", "中证红利"),
]
INDEX_PANEL_ORDER = ["shanghai", "szcheng", "chinext", "star50", "hs300", "zz1000", "zzdiv"]
RETURN_WINDOWS = (5, 20, 60)
KLINE_LIMIT = 70         # 60-day window needs 61 closes; keep a small buffer
KEEP_DAYS = 40           # trading days retained in caches
LADDER_ROWS = 24         # max rows emitted for the limit-up ladder

# Global markets. Eastmoney delayed cluster secids (one batched request).
EM_GLOBAL_SECIDS = [
    ("100.DJIA", "道琼斯", "美股"),
    ("100.NDX", "纳斯达克", "美股"),
    ("100.SPX", "标普500", "美股"),
    ("100.N225", "日经225", "亚太"),
    ("100.KS11", "韩国KOSPI", "亚太"),
    ("100.TWII", "台湾加权", "亚太"),
    ("100.HSI", "恒生指数", "亚太"),
    ("100.FTSE", "英国富时100", "欧洲"),
    ("100.GDAXI", "德国DAX", "欧洲"),
    ("100.UDI", "美元指数", "汇率"),
]
# Sina batched codes: (code, display name, category). Parsing is driven by the
# code prefix (gb_ / hf_ / fx_), see _sina_row.
SINA_GLOBAL_CODES = [
    ("gb_$sox", "费城半导体", "美股"),
    ("hf_XAU", "伦敦金现货", "商品"),
    ("hf_XAG", "伦敦银现货", "商品"),
    ("hf_CL", "纽约原油", "商品"),
    ("hf_CAD", "LME铜", "商品"),
    ("fx_susdcnh", "离岸人民币", "汇率"),
]

# ------------------------------------------------------- US Treasury yields
# Official daily constant-maturity yield curve, published by the US Treasury.
# Free, no key, no throttling, and it carries the whole curve (1M .. 30Y) plus
# multi-year history - which is more than any domestic vendor exposes for
# US rates (Eastmoney/Sina/Tencent simply have no US-bond symbols).
US_TREASURY_URL = ("https://home.treasury.gov/resource-center/data-chart-center/"
                   "interest-rates/daily-treasury-rates.csv/{year}/all"
                   "?type=daily_treasury_yield_curve&field_tdr_date_value={year}"
                   "&page&_format=csv")
UST_TENORS = ["1 Mo", "3 Mo", "6 Mo", "1 Yr", "2 Yr", "3 Yr", "5 Yr", "7 Yr",
              "10 Yr", "20 Yr", "30 Yr"]
UST_HISTORY_DAYS = 20      # sparkline length
UST_PERCENTILE_YEARS = 2   # history depth behind the 10Y percentile
# Fallback: CBOE 10Y yield index via Yahoo (fresher than the official CSV by
# about a day, but unofficial and occasionally 401/429 - used only on failure).
YAHOO_TNX_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX?range=5d&interval=1d"

# ------------------------------------------------------- Dragon-Tiger (龙虎榜)
# Both reports return the full day in ONE request at pageSize=500, so "as much
# data as possible" costs two calls, not ten.
LHB_STOCK_REPORT = "RPT_DAILYBILLBOARD_DETAILSNEW"
LHB_SEAT_REPORT = "RPT_BILLBOARD_DAILYDETAILSBUY"
LHB_PAGE_SIZE = 500
LHB_STOCK_ROWS = 30        # stocks emitted, |net| both directions
LHB_SEAT_TOP = 20          # brokerage seats by |NET|
# Seat names that are structurally interesting regardless of size. Every
# matching record is kept, on top of the |NET| ranking.
LHB_SPECIAL_SEATS = ("机构专用", "沪股通专用", "深股通专用")
LHB_SPECIAL_ROWS = 60      # cap per special-seat bucket, safety valve only
# 「连续三个交易日内，...」-style reasons span multiple sessions and their
# amounts must not be summed with single-day ones (see lhb_window). The numeral
# arrives as either 三 or 3.
_LHB_MULTIDAY = re.compile(r"连续([0-9两三四五六七十]+)个交易日")

# ------------------------------------------------------- Valuation (PE percentile)
# CSIndex's own index-perf endpoint exposes `peg` (the index PE) for ANY date
# range, which is enough to compute a 10-year percentile locally. Same endpoint
# the collector already hits for the SSE turnover, so no new vendor.
CSINDEX_PE_INDEXES = [("000985", "中证全指"), ("000300", "沪深300"),
                      ("000922", "中证红利")]
PE_WINDOW_YEARS = 10
PE_TRADING_DAYS_PER_YEAR = 244   # ~A-share convention; only bounds the window

# ------------------------------------------------------- Share unlocks (解禁)
# Eastmoney datacenter RPT_LIFT_STAGE: scheduled share-unlock (解禁) events,
# filterable by FREE_DATE range. LIFT_MARKET_CAP is the disclosure value in
# 万元 (priced at the source's own update timestamp, not today's close).
LIFT_REPORT = "RPT_LIFT_STAGE"
LIFT_HORIZON_DAYS = 7   # events scheduled within N calendar days after the card date
LIFT_TOP_ROWS = 5       # largest unlocks listed individually
LIFT_FLAG_RATIO = 0.05  # unlock shares >= 5% of total shares -> flagged

REQUEST_COUNTS: dict[str, int] = {}

RISK_NOTES = [
    "主力资金净流入为东方财富单因子口径（超大单+大单），非交易所统一披露字段，不同供应商数值不可直接混用。",
    "行业分类为东方财富行业板块体系，与其他平台的申万/中信行业分类不一致。",
    "涨跌停家数以东财涨停池为准；上市首日等无涨跌幅限制个股不计入。",
    "5日新高/新低基于本地收盘价缓存计算，缓存不足时该指标显示为「—」，连续运行数日后生效。",
    "两市成交额 = 中证指数官网披露的上证指数成交额 + 深交所官网披露的深市股票成交额，与行情软件口径可能略有差异。",
    "指数区间收益由腾讯行情日线收盘价直接计算（不复权，指数点位本身已含成分调整），"
    "60 日窗口需连续日线，缓存重建后首日可能缺失。",
    "估值分位（PE 历史百分位）基于中证官网 index-perf 的 peg 字段序列自算，窗口 10 年；"
    "该口径与申万/Choice 等第三方估值口径存在差异，分位仅作相对位置参考。",
    "解禁市值取自东财数据中心 RPT_LIFT_STAGE 披露值（按数据源更新时点价格计算，非最新收盘价），"
    "解禁数量与占比以交易所公告为准。",
    "本页为数据整理与规则演示，不构成投资建议。",
]


# ---------------------------------------------------------------- http layer
def http_get(url: str, *, headers: dict[str, str] | None = None, decode: str = "utf-8",
             retries: int = 3, timeout: int = 20) -> str:
    host = urllib.request.urlparse(url).netloc
    REQUEST_COUNTS[host] = REQUEST_COUNTS.get(host, 0) + 1
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                return resp.read().decode(decode, "replace")
        except Exception as exc:  # noqa: BLE001 - network layer
            last_exc = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url[:80]} ... {last_exc!r}")


def get_json(url: str, **kw: Any) -> Any:
    return json.loads(http_get(url, **kw))


def load_cache(path: Path) -> Any:
    if path.exists():
        try:
            with path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_cache(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False)
    tmp.replace(path)


# ---------------------------------------------------------------- sources
def fetch_index_klines() -> dict[str, list[dict[str, Any]]]:
    """Tencent daily klines: [{date, close, pct}] per index, ascending."""
    out: dict[str, list[dict[str, Any]]] = {}
    for symbol, key, _name in TENCENT_INDEXES:
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={symbol},day,,,{KLINE_LIMIT},qfq")
        data = get_json(url).get("data") or {}
        node = data.get(symbol) or {}
        rows = node.get("day") or node.get("qfqday") or []
        if not rows:
            raise RuntimeError(f"empty tencent kline for {symbol}")
        series = [{"date": r[0], "close": float(r[2])} for r in rows]
        for i in range(len(series)):
            prev = series[i - 1]["close"] if i > 0 else None
            series[i]["pct"] = round((series[i]["close"] / prev - 1) * 100, 2) if prev else 0.0
        out[key] = series
    return out


def fetch_sh_turnover(start: str, end: str) -> dict[str, float]:
    """CSIndex official: SSE Composite trading value (yi yuan) per date, one request."""
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf?"
           f"indexCode=000001&startDate={start.replace('-', '')}&endDate={end.replace('-', '')}")
    rows = get_json(url, headers={"Accept": "application/json"}).get("data") or []
    return {f"{r['tradeDate'][:4]}-{r['tradeDate'][4:6]}-{r['tradeDate'][6:]}":
            float(r["tradingValue"]) for r in rows}


def fetch_sz_turnover(date: str) -> float:
    """SZSE official: Shenzhen stock turnover (yi yuan) for one date."""
    url = ("https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&"
           f"CATALOGID=1803_sczm&TABKEY=tab1&txtQueryDate={date}")
    rows = get_json(url)
    for block in rows:
        for item in block.get("data", []):
            if item.get("lbmc") == "股票":
                return float(item["cjje"].replace(",", ""))
    raise RuntimeError(f"SZSE stock turnover not found for {date}")


def fetch_pool(kind: str, yyyymmdd: str) -> list[dict[str, Any]]:
    """Eastmoney topic pool: zt (涨停) / dt (跌停) / zb (炸板).

    push2ex sits outside the IP-throttling applied to push2/push2his.
    ZT rows carry lbc (连板数), zttj (n天m板), fund (封单额), zbc (炸板次数),
    hybk (行业) - everything the ladder needs, at zero extra request cost.
    """
    pool = {"zt": "ZT", "dt": "DT", "zb": "ZB"}[kind]
    sort = "fund%3Aasc" if kind == "dt" else "fbt%3Aasc"
    url = ("https://push2ex.eastmoney.com/getTopic" + pool + "Pool?cb=&"
           "ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&"
           f"Pageindex=0&pagesize=10000&sort={sort}&date={yyyymmdd}")
    return (get_json(url).get("data") or {}).get("pool") or []


def fetch_margin_history(rows: int = 6) -> list[dict[str, Any]]:
    """Whole-market margin balance (融资融券), newest first.

    Source: Eastmoney datacenter-web RPTA_RZRQ_LSHJ (a different host from the
    throttled push2 family). Values are in yuan; DIM_DATE is the disclosure
    date, which lags the market date by one trading day (T+1).
    """
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPTA_RZRQ_LSHJ"
           "&columns=DIM_DATE,RZRQYE,RZYE,RQYE,RZJME,RZYEZB"
           "&sortColumns=DIM_DATE&sortTypes=-1"
           f"&pageSize={rows}&pageNumber=1")
    payload = get_json(url)
    data = (payload.get("result") or {}).get("data") or []
    if not data:
        raise RuntimeError(f"margin history empty: {payload.get('message')}")
    return data


def fetch_em_global() -> dict[str, dict[str, Any]]:
    """US/APAC/EU indices + dollar index in one batched delayed-cluster call."""
    secids = ",".join(sid for sid, _, _ in EM_GLOBAL_SECIDS)
    url = ("https://push2delay.eastmoney.com/api/qt/ulist.np/get?fltt=2&"
           f"secids={secids}&fields=f2,f3,f4,f12,f14,f18")
    diff = (get_json(url).get("data") or {}).get("diff") or []
    return {str(row.get("f12")): row for row in diff if isinstance(row, dict)}


def fetch_sina_batch(codes: list[str]) -> dict[str, str]:
    """Sina hq.sinajs.cn, many symbols in one request. Returns raw CSV strings."""
    url = "https://hq.sinajs.cn/list=" + ",".join(codes)
    body = http_get(url, decode="gbk", headers={"Referer": "https://finance.sina.com.cn"})
    out: dict[str, str] = {}
    for line in body.strip().split("\n"):
        head, _, tail = line.partition('="')
        if not tail:
            continue
        out[head.replace("var hq_str_", "").strip()] = tail.rstrip('";')
    return out


def fetch_tencent_quotes(symbols: list[str]) -> dict[str, list[str]]:
    """Tencent qt.gtimg.cn single-quote endpoint (used for HSTECH)."""
    out: dict[str, list[str]] = {}
    body = http_get("https://qt.gtimg.cn/q=" + ",".join(symbols), decode="gbk")
    for line in body.strip().split("\n"):
        head, _, tail = line.partition('="')
        if not tail:
            continue
        fields = tail.rstrip('";').split("~")
        if len(fields) > 32:
            out[head.replace("v_", "").strip()] = fields
    return out


# ---------------------------------------------------------------- US Treasury
def parse_us_treasury_csv(raw: str) -> list[dict[str, Any]]:
    """Parse the Treasury daily yield-curve CSV into rows, newest first. Pure.

    Header is `Date,"1 Mo",...,"30 Yr"`; values are percent (4.75 == 4.75%).
    Rows are returned newest-first because the source file is already ordered
    that way and every consumer only needs the tail of it.
    """
    out: list[dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(raw)):
        date = (row.get("Date") or "").strip()
        if not date:
            continue
        try:
            iso = datetime.strptime(date, "%m/%d/%Y").strftime("%Y-%m-%d")
            y10 = float(row["10 Yr"])
        except (ValueError, KeyError, TypeError):
            continue
        tenors = {}
        for key in UST_TENORS:
            try:
                tenors[key] = float(row[key])
            except (KeyError, TypeError, ValueError):
                tenors[key] = None
        out.append({"date": iso, "tenors": tenors, "y10": y10,
                    "y2": tenors.get("2 Yr"), "y30": tenors.get("30 Yr")})
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def fetch_us_treasury_year(year: int) -> list[dict[str, Any]]:
    """Daily yield curve for one year, cached.

    Past years are immutable and cached forever. The current year is refetched
    every run because the Treasury appends one row per business day.
    """
    cache = CACHE_DIR / f"ust-{year}.json"
    if year < datetime.now().year:
        hit = load_cache(cache)
        if hit:
            return hit
    rows = parse_us_treasury_csv(http_get(US_TREASURY_URL.format(year=year), timeout=30))
    if not rows:
        raise RuntimeError(f"US Treasury csv empty for {year}")
    if year < datetime.now().year:
        save_cache(cache, rows)
    return rows


def build_us_treasury_view(panels: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
    """Percentile / spread / sparkline view from per-year curve rows. Pure.

    `panels` maps year -> rows (newest first). The newest row across all years
    wins; the rest feed the change, the YTD range and the 2-year percentile.
    """
    merged: list[dict[str, Any]] = []
    for rows in panels.values():
        merged.extend(rows)
    merged.sort(key=lambda r: r["date"], reverse=True)
    if not merged:
        raise RuntimeError("no US Treasury rows")

    latest, previous = merged[0], (merged[1] if len(merged) > 1 else None)
    change_bp = round((latest["y10"] - previous["y10"]) * 100, 1) if previous else None

    spread = None
    if latest["y2"] is not None:
        spread = round((latest["y10"] - latest["y2"]) * 100, 1)

    current_year = latest["date"][:4]
    ytd = [r["y10"] for r in merged if r["date"][:4] == current_year]
    ytd_high = round(max(ytd), 2) if ytd else None
    ytd_low = round(min(ytd), 2) if ytd else None

    # Percentile over the trailing N years of *closing* yields.
    cutoff = str(int(current_year) - UST_PERCENTILE_YEARS) + "-01-01"
    window = [r["y10"] for r in merged if r["date"] >= cutoff]
    if window:
        below = sum(1 for v in window if v < latest["y10"])
        percentile = round(below / len(window) * 100, 1)
    else:
        percentile = None

    history = [{"date": r["date"], "y10": r["y10"]}
               for r in merged[:UST_HISTORY_DAYS]][::-1]

    return {
        "as_of": latest["date"],
        "tenors": latest["tenors"],
        "y10": latest["y10"],
        "y2": latest["y2"],
        "y30": latest["y30"],
        "change_bp": change_bp,
        "spread_2s10s_bp": spread,
        "ytd_high": ytd_high,
        "ytd_low": ytd_low,
        "percentile": percentile,
        "percentile_years": UST_PERCENTILE_YEARS,
        "percentile_samples": len(window),
        "history": history,
        "source": "美国财政部官方日收益率曲线 CSV",
    }


def fetch_us_treasury() -> dict[str, Any]:
    """Official yields for the current year plus trailing history for context."""
    this_year = datetime.now().year
    panels: dict[int, list[dict[str, Any]]] = {}
    try:
        for year in range(this_year - UST_PERCENTILE_YEARS, this_year + 1):
            panels[year] = fetch_us_treasury_year(year)
    except Exception:  # noqa: BLE001 - fall through to the Yahoo fallback
        panels = {}
    if not panels:
        return _fetch_us10y_yahoo()
    return build_us_treasury_view(panels)


def _fetch_us10y_yahoo() -> dict[str, Any]:
    """Fallback: CBOE 10Y yield index (^TNX), already expressed in percent."""
    chart = (get_json(YAHOO_TNX_URL).get("chart") or {}).get("result") or []
    result = chart[0]
    meta = result.get("meta") or {}
    closes = [c for c in ((result.get("indicators") or {}).get("quote")
                          or [{}])[0].get("close") or [] if c is not None]
    if not closes:
        raise RuntimeError("yahoo ^TNX: no closes")
    stamps = result.get("timestamp") or []
    dates = [datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") for t in stamps]
    return {
        "as_of": (dates[-1] if dates else datetime.now().strftime("%Y-%m-%d")),
        "tenors": {"10 Yr": round(closes[-1], 2)},
        "y10": round(closes[-1], 2),
        "y2": None,
        "y30": None,
        "change_bp": (round((closes[-1] - closes[-2]) * 100, 1) if len(closes) > 1 else None),
        "spread_2s10s_bp": None,
        "ytd_high": None,
        "ytd_low": None,
        "percentile": None,
        "percentile_years": None,
        "percentile_samples": None,
        "history": [{"date": d, "y10": round(c, 2)} for d, c in list(zip(dates, closes))[-UST_HISTORY_DAYS:]],
        "source": "Yahoo Finance ^TNX（官方口径不可用时的降级源）",
    }


# ---------------------------------------------------------------- Dragon-Tiger
def _fetch_lhb_report(report: str, sort_column: str, date: str) -> list[dict[str, Any]]:
    """One datacenter report for one date, newest-|net|-first.

    Typical days fit in a single page (2026-08-31: 84 stock rows / 420 seat
    rows). If the day is busier than one page, fetch the ascending first page
    too and merge, so the net-buy and net-sell extremes are both covered even
    though the long tail in the middle is not.
    """
    def page(sort_type: int) -> tuple[list[dict[str, Any]], int]:
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
               f"reportName={report}&columns=ALL"
               f"&filter=(TRADE_DATE%3D%27{date}%27)"
               f"&sortColumns={sort_column}&sortTypes={sort_type}"
               f"&pageSize={LHB_PAGE_SIZE}&pageNumber=1&source=WEB&client=WEB")
        result = get_json(url).get("result") or {}
        return result.get("data") or [], int(result.get("count") or 0)

    data, count = page(-1)
    if not data:
        raise RuntimeError(f"LHB report {report} empty for {date}")
    if count > len(data):
        tail, _ = page(1)
        seen = {(r.get("SECURITY_CODE"), r.get("EXPLANATION"),
                 r.get(sort_column)) for r in data}
        data.extend(r for r in tail
                    if (r.get("SECURITY_CODE"), r.get("EXPLANATION"),
                        r.get(sort_column)) not in seen)
    return data


def fetch_lhb_stocks(date: str) -> list[dict[str, Any]]:
    """Stock-level Dragon-Tiger summary for one date."""
    return _fetch_lhb_report(LHB_STOCK_REPORT, "BILLBOARD_NET_AMT", date)


def fetch_lhb_seats(date: str) -> list[dict[str, Any]]:
    """Seat-level (brokerage branch) detail for one date.

    The report is named ...BUY but each row carries BUY/SELL/NET, and rows with
    a negative NET are present too, so this single call covers both directions.
    """
    return _fetch_lhb_report(LHB_SEAT_REPORT, "NET", date)


INDUSTRY_CACHE = CACHE_DIR / "industry.json"
INDUSTRY_BATCH = 50


def fetch_stock_industries(codes: list[str]) -> dict[str, str]:
    """Map stock codes to their 申万二级 industry via one batched quote call.

    f100 is the industry name and matches the taxonomy used by the sector flow
    rows, so dragon-tiger names can be rolled up into the same sectors. The
    mapping is cached forever in data/cache/industry.json because it barely
    changes; only unknown codes cost a request.
    """
    cache: dict[str, str] = {}
    if INDUSTRY_CACHE.exists():
        cache = json.loads(INDUSTRY_CACHE.read_text(encoding="utf-8") or "{}")
    missing = [c for c in dict.fromkeys(codes) if c and c not in cache]
    for start in range(0, len(missing), INDUSTRY_BATCH):
        batch = missing[start:start + INDUSTRY_BATCH]
        secids = ",".join(_secid(code) for code in batch)
        url = ("https://push2delay.eastmoney.com/api/qt/ulist.np/get?"
               f"secids={secids}&fields=f12,f14,f100&fltt=2&invt=2")
        rows = ((get_json(url).get("data") or {}).get("diff") or [])
        for row in rows:
            if row.get("f12") and row.get("f100"):
                cache[row["f12"]] = row["f100"]
    if missing:
        INDUSTRY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        INDUSTRY_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0,
                                             sort_keys=True), encoding="utf-8")
    return cache


def _secid(code: str) -> str:
    """Eastmoney secid prefix: 1 = SH, 0 = SZ/BJ."""
    return ("1." if code.startswith(("6", "9")) else "0.") + code


def fetch_all_stocks() -> list[dict[str, Any]]:
    """Sina hs_a list (SH/SZ/BJ): [{code, price, pct, prev_close}] via pagination."""
    count = int(get_json(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeStockCount?node=hs_a", decode="gbk"))
    stocks: list[dict[str, Any]] = []
    page, page_size = 1, 100
    while len(stocks) < count and page <= 80:
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"Market_Center.getHQNodeData?page={page}&num={page_size}&sort=symbol&asc=1&node=hs_a")
        rows = get_json(url, decode="gbk", retries=4)
        if not rows:
            break
        for r in rows:
            try:
                price, prev = float(r["trade"]), float(r["settlement"])
            except (TypeError, ValueError, KeyError):
                continue
            if price <= 0 or prev <= 0:
                continue  # suspended / no quote
            stocks.append({"code": r["symbol"], "price": price, "prev_close": prev,
                           "pct": float(r.get("changepercent") or 0)})
        page += 1
        time.sleep(0.12)
    if len(stocks) < 4000:
        raise RuntimeError(f"sina list too small: {len(stocks)}")
    return stocks


def fetch_sector_today() -> list[dict[str, Any]]:
    """Eastmoney sector ranking: ALL sectors' flow figures, one request per page.

    f62 today / f164 5-day / f174 10-day main-force net inflow (yuan), f3 pct.
    The board universe (~500) mixes industry levels (一/二/三级, e.g. 电子 ⊃
    消费电子 ⊃ 消费电子零部件及组装); all levels are kept - the decision-card
    layer decides what to display. Tries the realtime cluster first, then falls
    back to the delayed cluster (push2delay) - after market close the values
    are identical, and the delayed cluster sits outside the IP-throttling
    applied to push2/push2his.
    """
    fields = "f12,f14,f3,f62,f164,f174"
    diff: list[dict[str, Any]] = []
    last_exc: Exception | None = None
    for host in ("push2.eastmoney.com", "push2delay.eastmoney.com"):
        try:
            page, total = 1, 1
            while (page - 1) * 100 < total:
                query = (f"pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f62"
                         f"&fs=m:90+t:2&fields={fields}")
                url = f"https://{host}/api/qt/clist/get?{query}"
                data = get_json(url, retries=1).get("data") or {}
                total = int(data.get("total") or 0)
                diff.extend(data.get("diff") or [])
                page += 1
                time.sleep(0.2)
            if diff:
                break
        except Exception as exc:  # noqa: BLE001 - try the next cluster
            diff = []
            last_exc = exc
    if not diff:
        raise RuntimeError(f"sector ranking unavailable on all clusters: {last_exc!r}")
    out, seen = [], set()
    for item in diff:
        if not isinstance(item, dict) or not item.get("f12") or item["f12"] in seen:
            continue
        seen.add(item["f12"])
        vals = {}
        for key, field in (("today", "f62"), ("day5", "f164"), ("day10", "f174")):
            raw = item.get(field)
            vals[key] = float(raw) if isinstance(raw, (int, float)) else None
        out.append({
            "code": item["f12"], "name": item["f14"],
            "pct": float(item["f3"]) if isinstance(item.get("f3"), (int, float)) else 0.0,
            **vals,
        })
    return out


# ---------------------------------------------------------------- pure logic
def build_flow_rows(sectors_today: list[dict[str, Any]],
                    history: dict[str, dict[str, Any]],
                    trading_dates: list[str],
                    zt_sector: dict[str, int]) -> list[dict[str, Any]]:
    """Assemble schema flows[].

    day5/day10 come straight from the ranking (EM's own aggregation); the
    cached daily history (f62 per day) only serves sectors where the ranking
    fields are missing, and accumulates toward longer custom windows later.
    Pure function.
    """
    market_date = trading_dates[-1]
    rows = []
    for sec in sectors_today:
        daily = dict((history.get(sec["code"]) or {}).get("daily", {}))
        if sec["today"] is not None:
            daily[market_date] = sec["today"]  # today's ranking value wins
        if market_date not in daily:
            continue  # no data at all for this sector
        day5 = sec["day5"] if sec["day5"] is not None else sum(
            daily.get(d, 0.0) for d in trading_dates[-5:])
        day10 = sec["day10"] if sec["day10"] is not None else sum(
            daily.get(d, 0.0) for d in trading_dates[-10:])
        rows.append({
            "sector": sec["name"],
            "today": round(daily[market_date] / 1e8, 1),
            "day5": round(day5 / 1e8, 1),
            "day10": round(day10 / 1e8, 1),
            "change_pct": round(sec["pct"], 2),
            "limit_up": zt_sector.get(sec["name"], 0),
        })
    return rows


# ------------------------------------------------- new blocks (pure logic)
def window_returns(closes: list[float], windows: tuple[int, ...] = RETURN_WINDOWS
                   ) -> dict[str, float | None]:
    """区间收益 %: close[-1] / close[-1-N] - 1. None when history is short."""
    out: dict[str, float | None] = {}
    for n in windows:
        out[f"ret{n}"] = (round((closes[-1] / closes[-1 - n] - 1) * 100, 2)
                          if len(closes) > n else None)
    return out


def build_index_panel(klines: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Seven-index snapshot: close / day pct / 5d / 20d / 60d returns. Pure."""
    names = {key: name for _, key, name in TENCENT_INDEXES}
    panel = []
    for key in INDEX_PANEL_ORDER:
        series = klines.get(key) or []
        if not series:
            continue
        row = {"key": key, "name": names.get(key, key), "date": series[-1]["date"],
               "close": round(series[-1]["close"], 2), "pct": series[-1]["pct"]}
        row.update(window_returns([r["close"] for r in series]))
        panel.append(row)
    return panel


def build_limit_ladder(zt_pool: list[dict[str, Any]], zb_pool: list[dict[str, Any]],
                       prev_codes: list[str], date: str,
                       limit_down: int | None = None,
                       rows: int = LADDER_ROWS) -> dict[str, Any]:
    """连板天梯 + 情绪指标。Pure - no I/O.

    口径（与参考卡/行情软件可能不同，页面明示）：
      封板率   = 收盘涨停 / (收盘涨停 + 炸板)       —— 炸板取东财炸板池
      晋级率   = 昨日涨停股中今日仍涨停的占比        —— 依赖本地涨停代码缓存
      封单力度 = 封单金额 / 流通市值
    """
    source: list[dict[str, Any]] = []
    for item in zt_pool:
        code = str(item.get("c") or "").strip()
        if not code:
            continue
        board = int(item.get("lbc") or 1) or 1
        ltsz = float(item.get("ltsz") or 0)
        fund = float(item.get("fund") or 0)
        zttj = item.get("zttj") or {}
        days = int(zttj.get("days") or board)
        ct = int(zttj.get("ct") or board)
        source.append({
            "code": code,
            "stock": (item.get("n") or code).replace(" ", ""),
            "sector": item.get("hybk") or "未知",
            "board": board,
            "seal_ratio": round(fund / ltsz * 100, 2) if ltsz > 0 else None,
            "seal_fund": round(fund / 1e8, 2),
            "amount": round(float(item.get("amount") or 0) / 1e8, 2),
            "turnover_rate": round(float(item.get("hs") or 0), 2),
            "zbc": int(item.get("zbc") or 0),
            "note": f"{days}天{ct}板" if days > ct else f"{ct}连板",
        })
    source.sort(key=lambda r: (-r["board"], -(r["seal_ratio"] or 0), r["code"]))

    distribution: dict[str, int] = {}
    for row in source:
        key = str(row["board"])
        distribution[key] = distribution.get(key, 0) + 1

    limit_up, zha_ban = len(zt_pool), len(zb_pool)
    prev_set = set(prev_codes or [])
    today_codes = {r["code"] for r in source}
    promoted = len(today_codes & prev_set) if prev_set else None
    metrics = {
        "limit_up": limit_up,
        "limit_down": limit_down,
        "zha_ban": zha_ban,
        "seal_rate": round(limit_up / (limit_up + zha_ban) * 100, 1) if limit_up + zha_ban else None,
        "promotion_rate": (round(promoted / len(prev_set) * 100, 1)
                           if prev_set and promoted is not None else None),
        "promoted": promoted,
        "prev_limit_up": len(prev_set) or None,
        "max_board": source[0]["board"] if source else 0,
        "two_board_plus": sum(v for k, v in distribution.items() if int(k) >= 2),
    }
    return {
        "date": date,
        "ladder": source[:rows],
        "distribution": dict(sorted(distribution.items(), key=lambda kv: int(kv[0]), reverse=True)),
        "metrics": metrics,
        "notes": [
            "封板率 = 收盘涨停 /（收盘涨停 + 炸板），采用东财涨停池与炸板池口径；"
            "与按「盘中曾触及涨停」统计的口径不同，数值不可直接比较。",
            "晋级率 = 上一交易日涨停个股中今日仍收涨停的占比，依据东财涨停池个股代码比对，"
            "上一交易日名单由本地缓存提供（首次运行会多取一次历史涨停池）。",
            "连板数取东财涨停池 lbc 字段；「n 天 m 板」取 zttj 字段；封单力度 = 封单金额 / 流通市值。",
        ],
    }


def build_margin_view(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Whole-market margin balance view from the datacenter history. Pure."""
    def yi(value: Any) -> float | None:
        return round(float(value) / 1e8, 2) if value is not None else None

    latest = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    balance = yi(latest.get("RZRQYE"))
    prev_balance = yi(prev.get("RZRQYE")) if prev else None
    return {
        "balance": balance,
        "change": round(balance - prev_balance, 2) if None not in (balance, prev_balance) else None,
        "financing": yi(latest.get("RZYE")),
        "securities_loan": yi(latest.get("RQYE")),
        "financing_net_buy": yi(latest.get("RZJME")),
        "pct_of_float": round(float(latest["RZYEZB"]), 2) if latest.get("RZYEZB") is not None else None,
        "as_of": str(latest.get("DIM_DATE", ""))[:10],
        "prev_as_of": str(prev.get("DIM_DATE", ""))[:10] if prev else None,
        "note": "两融余额为沪深两市合计（东财数据中心历史汇总），交易所 T+1 披露，"
                "数据日期通常落后卡片市场日 1 个交易日，请以 as_of 字段为准。",
    }


def lhb_window(reason: str) -> str:
    """Disclosure window of a Dragon-Tiger reason. Pure.

    The exchange files one record per *reason*, and reasons span different
    windows: 「连续三个交易日内…累计达到…」covers three sessions, everything
    else covers the single session. Amounts from the two are NOT additive - the
    multi-day figure already contains the day - so they must never be summed.

    The numeral appears as both 三 and 3 in the source text, hence the regex.
    """
    match = _LHB_MULTIDAY.search(reason or "")
    if not match:
        return "当日"
    token = match.group(1)
    words = {"两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "十": 10}
    days = words.get(token)
    if days is None:
        try:
            days = int(token)
        except ValueError:
            days = 3
    return f"{days}日"


def _merge_reasons(rows: list[dict[str, Any]], keyfields: tuple[str, ...]
                   ) -> list[dict[str, Any]]:
    """Collapse rows describing the same disclosure event, joining reasons. Pure.

    A stock can be listed under several single-session reasons (e.g. 换手率20%
    and 涨幅偏离7%) that describe the same disclosure with identical amounts.
    Those are one event, so they become one row with both reasons shown. Rows
    that differ by window or amount stay separate - they are different data.
    """
    out: list[dict[str, Any]] = []
    index: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row.get(f) for f in keyfields) + (row["window"], row["net_wan"])
        hit = index.get(key)
        if hit is None:
            clone = dict(row)
            index[key] = clone
            out.append(clone)
        elif row["reason"] and row["reason"] not in hit["reason"]:
            hit["reason"] = f"{hit['reason']} / {row['reason']}"
    return out


def build_dragon_tiger(stocks: list[dict[str, Any]],
                       seats: list[dict[str, Any]]) -> dict[str, Any]:
    """Dragon-Tiger (龙虎榜) view from the two datacenter reports. Pure.

    "As much data as possible" is cheap here: both reports fit in one request
    each, so the stock table keeps every listed record and the seat table keeps
    the |NET| ranking *plus* every 机构专用 / 沪股通 / 深股通 record.

    Seat rows carry no security name, so names are joined from the stock report
    by SECURITY_CODE; unmatched codes fall back to the bare code.

    One row per (security, reason) is preserved on purpose - see lhb_window.
    """
    def wan(value: Any) -> float | None:
        return round(float(value) / 1e4, 1) if value is not None else None

    def yi(value: Any) -> float | None:
        return round(float(value) / 1e8, 2) if value is not None else None

    def pct(value: Any) -> float | None:
        return round(float(value), 2) if value is not None else None

    names = {str(s.get("SECURITY_CODE")): s.get("SECURITY_NAME_ABBR")
             for s in stocks if s.get("SECURITY_CODE")}

    stock_rows = [{
        "code": str(s.get("SECURITY_CODE") or ""),
        "name": s.get("SECURITY_NAME_ABBR") or str(s.get("SECURITY_CODE") or ""),
        "close": s.get("CLOSE_PRICE"),
        "pct": pct(s.get("CHANGE_RATE")),
        "net_wan": wan(s.get("BILLBOARD_NET_AMT")),
        "buy_wan": wan(s.get("BILLBOARD_BUY_AMT")),
        "sell_wan": wan(s.get("BILLBOARD_SELL_AMT")),
        "amount_yi": yi(s.get("ACCUM_AMOUNT")),
        "turnover": pct(s.get("TURNOVERRATE")),
        "reason": s.get("EXPLANATION") or "",
        "seat_note": s.get("EXPLAIN") or "",
    } for s in stocks]

    seat_rows = [{
        "code": str(r.get("SECURITY_CODE") or ""),
        "name": names.get(str(r.get("SECURITY_CODE") or ""),
                          str(r.get("SECURITY_CODE") or "")),
        "seat": r.get("OPERATEDEPT_NAME") or "",
        "buy_wan": wan(r.get("BUY")),
        "sell_wan": wan(r.get("SELL") or 0),
        "net_wan": wan(r.get("NET")),
        "pct": pct(r.get("CHANGE_RATE")),
        "rise_prob": pct(r.get("RISE_PROBABILITY_3DAY")),
        "buy_times": r.get("TOTAL_BUYER_SALESTIMES_3DAY"),
        "reason": r.get("EXPLANATION") or "",
    } for r in seats]

    for row in stock_rows:
        row["window"] = lhb_window(row["reason"])
    for row in seat_rows:
        row["window"] = lhb_window(row["reason"])
    stock_rows = _merge_reasons(stock_rows, ("code",))
    seat_rows = _merge_reasons(seat_rows, ("code", "seat"))

    stock_rows.sort(key=lambda r: -abs(r["net_wan"] or 0))
    top_seats = sorted(seat_rows, key=lambda r: -abs(r["net_wan"] or 0))[:LHB_SEAT_TOP]

    special: dict[str, list[dict[str, Any]]] = {}
    for bucket in LHB_SPECIAL_SEATS:
        rows = [r for r in seat_rows if bucket in r["seat"]]
        rows.sort(key=lambda r: -(r["net_wan"] or 0))
        special[bucket] = rows[:LHB_SPECIAL_ROWS]

    # Totals are computed on single-session rows only, because 3-day disclosures
    # are a superset of the day and would double-count.
    stock_base = [r for r in stock_rows if r["window"] == "当日"]
    seat_base = [r for r in seat_rows if r["window"] == "当日"]
    inst_rows = [r for r in (special.get("机构专用") or []) if r["window"] == "当日"]
    north_rows = [r for bucket in ("沪股通专用", "深股通专用")
                  for r in (special.get(bucket) or []) if r["window"] == "当日"]

    def total(rows: list[dict[str, Any]]) -> float:
        return round(sum(r["net_wan"] or 0 for r in rows), 1)

    return {
        "as_of": str((stocks[0].get("TRADE_DATE") or ""))[:10] if stocks else None,
        "record_count": len(stock_rows),
        "stock_count": len({r["code"] for r in stock_rows}),
        "seat_count": len(seat_rows),
        "broker_count": len({r["seat"] for r in seat_rows if r["seat"]}),
        "stocks": stock_rows,
        "top_seats": top_seats,
        "special": special,
        "summary": {
            "basis": "当日窗口（已去重），3日窗口记录单独标注不并入合计",
            "net_in_stocks": sum(1 for r in stock_base if (r["net_wan"] or 0) > 0),
            "net_out_stocks": sum(1 for r in stock_base if (r["net_wan"] or 0) < 0),
            "total_net_wan": total(stock_base),
            "seat_net_wan": total(seat_base),
            "inst_net_wan": total(inst_rows),
            "inst_count": len(inst_rows),
            "inst_buy_count": sum(1 for r in inst_rows if (r["net_wan"] or 0) > 0),
            "north_net_wan": total(north_rows),
            "north_count": len(north_rows),
            "top_buy_seat": top_seats[0] if top_seats and (top_seats[0]["net_wan"] or 0) > 0 else None,
            "top_sell_seat": (sorted(seat_rows, key=lambda r: (r["net_wan"] or 0))[0]
                              if seat_rows else None),
        },
        "note": "龙虎榜为交易所披露的公开席位数据。「机构专用」为机构席位合计口径，"
                "沪/深股通专用为北向资金席位；「3日上涨概率」为该席位历史上榜后 3 日的统计胜率，"
                "仅为历史频率，不代表未来。同一只证券触发多个上榜原因时会分行列示，"
                "「连续三个交易日内」类原因的统计窗口为 3 日、与当日口径不可相加，"
                "故合计口径仅取当日窗口并去重。",
    }


# ---------------------------------------------------------------- Valuation
def pe_percentile(series: dict[str, float], years: int = PE_WINDOW_YEARS
                  ) -> dict[str, Any] | None:
    """Percentile of the latest PE within the trailing N-year window. Pure.

    `series` maps ISO date -> PE. The window is cut by calendar year (365-day
    steps), not by trading-day count, so holidays only cost precision, never
    correctness. Returns None when there is not even a full year of history.
    """
    if not series:
        return None
    last_date = max(series)
    cur = series[last_date]
    cutoff = f"{int(last_date[:4]) - years}-{last_date[4:]}"
    window = [v for d, v in sorted(series.items()) if d >= cutoff]
    if len(window) < PE_TRADING_DAYS_PER_YEAR or window[-1] != cur:
        return None
    below = sum(1 for v in window if v <= cur)
    return {
        "as_of": last_date,
        "pe": round(cur, 2),
        "percentile": round(below / len(window) * 100, 1),
        "window_years": years,
        "samples": len(window),
    }


def fetch_index_pe(code: str, start: str, end: str) -> dict[str, float]:
    """CSIndex index-perf `peg` field: {ISO date: index PE}, one request."""
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf?"
           f"indexCode={code}&startDate={start.replace('-', '')}"
           f"&endDate={end.replace('-', '')}")
    rows = get_json(url, headers={"Accept": "application/json"}).get("data") or []
    return {f"{r['tradeDate'][:4]}-{r['tradeDate'][4:6]}-{r['tradeDate'][6:]}":
            float(r["peg"]) for r in rows if r.get("peg")}


def build_valuation(code: str, name: str, end_date: str) -> dict[str, Any] | None:
    """PE percentile for one index, with a local ever-growing history cache.

    Cold start pulls the whole 10-year window in one request; afterwards only
    the days after the last cached date are fetched (usually 0-2 rows).
    """
    cache = CACHE_DIR / f"pe-{code}.json"
    series: dict[str, float] = load_cache(cache)
    if not isinstance(series, dict):
        series = {}
    start = (min(series) if series else
             f"{int(end_date[:4]) - PE_WINDOW_YEARS}{end_date[4:]}")
    if series:
        last = max(series)
        y, m, d = int(last[:4]), int(last[5:7]), int(last[8:10])
        nxt = (datetime(y, m, d) + timedelta(days=1)).strftime("%Y-%m-%d")
        start = min(start, nxt)
    if start <= end_date:
        series.update(fetch_index_pe(code, start, end_date))
        series = {k: v for k, v in sorted(series.items())}
        save_cache(cache, series)
    view = pe_percentile(series)
    if view is None:
        return None
    view["name"] = name
    view["code"] = code
    return view


# ---------------------------------------------------------------- Share unlocks
def fetch_lift_rows(start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Scheduled unlock (解禁) events within a date range, one request."""
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           f"reportName={LIFT_REPORT}&columns=ALL"
           f"&filter=(FREE_DATE%3E%3D%27{start_date}%27)"
           f"(FREE_DATE%3C%3D%27{end_date}%27)"
           "&sortColumns=LIFT_MARKET_CAP&sortTypes=-1"
           f"&pageSize={LHB_PAGE_SIZE}&pageNumber=1&source=WEB&client=WEB")
    data = (get_json(url).get("result") or {}).get("data") or []
    if data is None:
        raise RuntimeError("lift stage report failed")
    return data


def build_lift_view(rows: list[dict[str, Any]], start_date: str,
                    end_date: str) -> dict[str, Any]:
    """Unlock events -> risk view: totals, per-day load, TOP5, flags. Pure.

    LIFT_MARKET_CAP arrives in 万元 and is priced at the source's own update
    time, so it is shown as-is (no re-pricing) and labelled accordingly.
    TOTAL_RATIO is unlock shares / total shares as a fraction.
    """
    events = []
    for r in rows:
        date = str(r.get("FREE_DATE") or "")[:10]
        if not start_date <= date <= end_date:
            continue
        cap_wan = r.get("LIFT_MARKET_CAP")
        ratio = r.get("TOTAL_RATIO")
        events.append({
            "date": date,
            "code": str(r.get("SECURITY_CODE") or ""),
            "name": r.get("SECURITY_NAME_ABBR") or "",
            "cap_yi": round(float(cap_wan) / 1e4, 2) if cap_wan is not None else None,
            "shares_wan": round(float(r["FREE_SHARES"]), 1) if r.get("FREE_SHARES") is not None else None,
            "type": r.get("FREE_SHARES_TYPE") or "",
            "ratio_pct": round(float(ratio) * 100, 2) if ratio is not None else None,
        })

    events.sort(key=lambda e: -(e["cap_yi"] or 0))
    by_date: dict[str, dict[str, Any]] = {}
    for e in events:
        slot = by_date.setdefault(e["date"], {"date": e["date"], "count": 0, "cap_yi": 0.0})
        slot["count"] += 1
        slot["cap_yi"] = round(slot["cap_yi"] + (e["cap_yi"] or 0), 2)
    flagged = [e for e in events if (e["ratio_pct"] or 0) >= LIFT_FLAG_RATIO * 100]
    return {
        "window": f"{start_date} ~ {end_date}",
        "event_count": len(events),
        "total_cap_yi": round(sum(e["cap_yi"] or 0 for e in events), 2),
        "by_date": sorted(by_date.values(), key=lambda s: s["date"]),
        "top": events[:LIFT_TOP_ROWS],
        "flagged": [
            {"name": e["name"], "date": e["date"], "ratio_pct": e["ratio_pct"],
             "cap_yi": e["cap_yi"]}
            for e in flagged[:LIFT_TOP_ROWS]
        ],
        "note": "解禁市值取东财数据中心披露值（按其更新时点价格计算，非最新收盘价）；"
                f"占比≥{LIFT_FLAG_RATIO * 100:.0f}% 总股本的解禁单列为风险提示；"
                "解禁不必然导致下跌，仅为供给端参考。",
    }


def global_session_state(market_date: str, now: datetime | None = None) -> str:
    """Has the US session for market_date closed yet (local clock is Beijing)?

    US cash equities close 16:00 ET, i.e. 04:00-05:00 Beijing the next day.
    A card built before that gets an intraday print, and must say so instead of
    silently presenting it as a close. 05:00 is the conservative bound (EDT).
    """
    now = now or datetime.now()
    close = datetime.strptime(market_date, "%Y-%m-%d") + timedelta(days=1, hours=5)
    return "closed" if now >= close else "intraday"


def _num(value: Any) -> float | None:
    """Coerce to float. 4 decimals keeps FX pairs meaningful; the page formats."""
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _sina_row(raw: str | None, kind: str) -> dict[str, Any]:
    """Parse one sina hq.sinajs.cn CSV payload. Pure."""
    if not raw:
        return {"close": None, "pct": None, "as_of": None}
    parts = raw.split(",")
    try:
        if kind == "gb":      # 名称,现价,涨跌幅%,时间,...
            return {"close": _num(parts[1]), "pct": _num(parts[2]),
                    "as_of": (parts[3] or None) if len(parts) > 3 else None}
        if kind == "hf":      # 现价,买,卖,?,高,低,时间,昨收,开盘,...,日期,名称
            close, prev_close = _num(parts[0]), _num(parts[7])
            return {"close": close,
                    "pct": round((close / prev_close - 1) * 100, 2)
                           if close and prev_close else None,
                    "as_of": (parts[12] or None) if len(parts) > 12 else None}
        if kind == "fx":      # 时间,现价,买,卖,昨收,...,涨跌幅%,...,日期
            return {"close": _num(parts[1]), "pct": _num(parts[10]),
                    "as_of": (parts[17] or None) if len(parts) > 17 else None}
    except IndexError:
        pass
    return {"close": None, "pct": None, "as_of": None}


def build_global_rows(em_quotes: dict[str, dict[str, Any]],
                      sina_raw: dict[str, str],
                      tx_quotes: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Assemble the global-market table from the three batched sources. Pure.

    Any single symbol can come back empty; the row is still emitted with nulls
    so the table keeps its shape and the page can show「—」.
    """
    rows: list[dict[str, Any]] = []
    for secid, name, category in EM_GLOBAL_SECIDS:
        quote = em_quotes.get(secid.split(".", 1)[1]) or {}
        rows.append({"name": name, "category": category, "close": _num(quote.get("f2")),
                     "pct": _num(quote.get("f3")), "as_of": None, "unit": "点"})
    for code, name, category in SINA_GLOBAL_CODES:
        parsed = _sina_row(sina_raw.get(code), code[:2])
        rows.append({"name": name, "category": category, "close": parsed["close"],
                     "pct": parsed["pct"], "as_of": parsed["as_of"], "unit": None})
    for symbol, name, category in (("hkHSTECH", "恒生科技", "亚太"),):
        fields = tx_quotes.get(symbol) or []
        rows.append({"name": name, "category": category,
                     "close": _num(fields[3]) if len(fields) > 3 else None,
                     "pct": _num(fields[32]) if len(fields) > 32 else None,
                     "as_of": (fields[30] or None) if len(fields) > 30 else None,
                     "unit": "点"})
    return rows


def day_feature(sh: float, cx: float, star: float, t_delta: float, limit_up: int) -> str:
    avg = (sh + cx + star) / 3
    if avg >= 0.5 and t_delta > 0.05:
        return "放量上攻"
    if avg >= 0.5:
        return "缩量反弹"
    if avg <= -0.5 and t_delta > 0.05:
        return "放量下跌"
    if avg <= -0.5:
        return "缩量回调"
    if limit_up >= 60 and avg < 0:
        return "指数弱、题材活跃"
    if abs(avg) < 0.3:
        return "窄幅震荡"
    return "结构分化"


def prune_history(history: dict[str, dict[str, float]], keep_dates: list[str]) -> None:
    keep = set(keep_dates)
    for daily in history.values():
        for d in list(daily):
            if d not in keep:
                del daily[d]


# ---------------------------------------------------------------- main
def main() -> None:
    sources: list[dict[str, str]] = []

    # 1. Index klines (calendar driver) - 3 requests
    klines = fetch_index_klines()
    dates_all = [r["date"] for r in klines["shanghai"]]
    market_date = dates_all[-1]
    prev_date = dates_all[-2] if len(dates_all) >= 2 else None
    window6 = dates_all[-6:]
    print(f"[1/8] market date: {market_date}")
    sources.append({
        "name": "腾讯行情接口 fqkline（指数日线涨跌幅）",
        "as_of": f"{market_date} 收盘",
        "note": "上证指数、创业板指、科创50 涨跌幅，由相邻收盘价计算。",
    })

    # 2. Turnover - 1 (CSIndex range) + 1/day (SZSE today) + cache
    sh_amount = fetch_sh_turnover(window6[0], window6[-1])
    daily_stats: dict[str, dict[str, Any]] = load_cache(DAILY_STATS_CACHE)
    for d in window6:
        entry = daily_stats.setdefault(d, {})
        if entry.get("sz_turnover") is None:
            entry["sz_turnover"] = fetch_sz_turnover(d)  # cold start: up to 6, then 1/day
    turnover = {d: round((sh_amount.get(d, 0.0) + daily_stats[d]["sz_turnover"]) / 1e4, 2)
                for d in window6 if d in daily_stats}
    if turnover.get(market_date, 0) <= 0:
        raise RuntimeError("turnover assembly failed")
    print(f"[2/8] turnover {market_date}: {turnover[market_date]} trillion CNY")
    sources.append({
        "name": "中证指数有限公司官网（上证指数成交额）",
        "as_of": f"{market_date} 收盘",
        "note": "index-perf 接口 tradingValue 字段，单位亿元。",
    })
    sources.append({
        "name": "深圳证券交易所官网（深市股票成交金额）",
        "as_of": f"{market_date} 收盘",
        "note": "每日行情-证券类别统计中「股票」行，单位亿元。两市成交额=沪+深。历史值由本地缓存提供。",
    })

    # 3. ZT/DT/ZB pools - 3 requests today + cache for the display window
    for d in window6:
        entry = daily_stats.setdefault(d, {})
        if "zt" not in entry:
            entry["zt"] = len(fetch_pool("zt", d.replace("-", "")))
        if "dt" not in entry:
            entry["dt"] = len(fetch_pool("dt", d.replace("-", "")))
        if "zt" in entry and "dt" in entry:
            print(f"[3/8] pools {d}: ZT={entry['zt']} DT={entry['dt']}")
    zt_today = fetch_pool("zt", market_date.replace("-", ""))  # full list for sector attribution
    zb_today = fetch_pool("zb", market_date.replace("-", ""))  # 炸板池：封板率分母
    zt_sector: dict[str, int] = {}
    for item in zt_today:
        name = item.get("hybk") or "未知"
        zt_sector[name] = zt_sector.get(name, 0) + 1
    daily_stats[market_date]["zt"] = len(zt_today)
    daily_stats[market_date]["dt"] = len(fetch_pool("dt", market_date.replace("-", ""))) \
        if daily_stats[market_date].get("dt") is None else daily_stats[market_date]["dt"]
    sources.append({
        "name": "东方财富 涨停/跌停/炸板池接口（push2ex）",
        "as_of": f"{market_date} 收盘",
        "note": "涨跌停家数按东财涨停池判定，行业归属按池内 hybk 字段；"
                "连板数取 lbc、封单额取 fund、炸板次数取 zbc、炸板家数取炸板池。",
    })

    # 涨停代码名单：当日入缓存，晋级率需要上一交易日名单（冷启动多取一次）
    daily_stats[market_date]["zt_codes"] = [
        str(item.get("c")) for item in zt_today if item.get("c")]
    prev_codes = daily_stats.get(prev_date, {}).get("zt_codes") if prev_date else None
    if not prev_codes and prev_date:
        prev_codes = [str(i.get("c")) for i in fetch_pool("zt", prev_date.replace("-", ""))
                      if i.get("c")]
        daily_stats[prev_date]["zt_codes"] = prev_codes
        print(f"[3/8] backfilled ZT codes for {prev_date}: {len(prev_codes)}")

    # prune daily stats
    for d in list(daily_stats):
        if d not in dates_all:
            del daily_stats[d]
    save_cache(DAILY_STATS_CACHE, daily_stats)

    # 4. Breadth + close cache - ~56 requests (Sina, tolerant)
    stocks = fetch_all_stocks()
    up = sum(1 for s in stocks if s["pct"] > 0)
    down = sum(1 for s in stocks if s["pct"] < 0)
    flat = sum(1 for s in stocks if s["pct"] == 0)
    print(f"[4/8] breadth: up={up} down={down} flat={flat} total={len(stocks)}")
    sources.append({
        "name": "新浪财经 A 股全列表接口（涨跌家数）",
        "as_of": f"{market_date} 收盘",
        "note": "沪深北全市场；停牌个股（无有效报价）不计入。",
    })

    breadth_history: dict[str, dict[str, float]] = load_cache(BREADTH_CACHE)
    if prev_date:
        breadth_history[prev_date] = {s["code"]: s["prev_close"] for s in stocks}
    breadth_history[market_date] = {s["code"]: s["price"] for s in stocks}
    prior_dates = [d for d in sorted(breadth_history) if d < market_date][-4:]
    new_high = new_low = None
    bootstrap_note: str | None = None
    if len(prior_dates) >= 2:
        if len(prior_dates) < 4:
            bootstrap_note = (f"本次新高/新低仅基于 {len(prior_dates)} 个历史交易日计算"
                              "（收盘价缓存积累中，连续运行数个交易日后为完整口径）。")
        new_high = new_low = 0
        for s in stocks:
            priors = [breadth_history[d].get(s["code"]) for d in prior_dates
                      if s["code"] in breadth_history[d]]
            if len(priors) < 2:
                continue
            if s["price"] > max(priors):
                new_high += 1
            if s["price"] < min(priors):
                new_low += 1
    keep_dates = dates_all[-KEEP_DAYS:]
    for d in list(breadth_history):
        if d not in keep_dates:
            del breadth_history[d]
    save_cache(BREADTH_CACHE, breadth_history)
    if new_high is None:
        print("[4/8] new-high/low: cache insufficient -> null this run")

    # 5. Sector flows - 1 request today + cache; one-time backfill if needed
    flows: list[dict[str, Any]] = []
    flow_status = "ok"
    sector_history: dict[str, dict[str, Any]] = load_cache(SECTOR_FLOW_CACHE)
    try:
        sectors_today = fetch_sector_today()
    except Exception as exc:  # noqa: BLE001
        print(f"[5/8] sector ranking unavailable ({type(exc).__name__}) -> flows degraded")
        sectors_today = []
        flow_status = "throttled"

    if sectors_today:
        # cache today's f62 per sector (raw material for longer custom windows)
        for sec in sectors_today:
            entry = sector_history.setdefault(sec["code"], {"name": sec["name"], "daily": {}})
            entry["name"] = sec["name"]
            if sec["today"] is not None:
                entry["daily"][market_date] = sec["today"]
        prune_history({c: e["daily"] for c, e in sector_history.items()}, keep_dates)
        save_cache(SECTOR_FLOW_CACHE, sector_history)
        flows = build_flow_rows(sectors_today, sector_history, dates_all, zt_sector)
        if not flows:
            flow_status = "throttled"

    if flow_status == "ok":
        print(f"[5/8] sector flows: {len(flows)} sectors")
        sources.append({
            "name": "东方财富 行业板块主力资金流接口（clist 排行，实时集群不可用时自动切换延时集群）",
            "as_of": f"{market_date} 收盘",
            "note": "当日/5日/10日主力净流入均来自板块资金排行的东财官方聚合字段（f62/f164/f174），"
                    "单次请求全量。主力=超大单+大单（东财单因子口径）。",
        })
    else:
        print("[5/8] sector flows: UNAVAILABLE -> empty with note")

    # 6. market_days from caches
    market_days = []
    for d in window6[1:]:
        i = dates_all.index(d)
        sh = klines["shanghai"][i]["pct"]
        cx = klines["chinext"][i]["pct"]
        star = klines["star50"][i]["pct"]
        stat = daily_stats.get(d, {})
        delta = turnover[d] - turnover[dates_all[i - 1]]
        market_days.append({
            "date": d[5:],
            "shanghai": sh, "chinext": cx, "star50": star,
            "turnover": turnover[d],
            "limit_up": stat.get("zt", 0),
            "limit_down": stat.get("dt", 0),
            "feature": day_feature(sh, cx, star, delta, stat.get("zt", 0)),
        })

    # 6. Index panel (7 indexes × 5/20/60d) - pure, zero extra requests
    index_panel = build_index_panel(klines)
    print(f"[6/8] index panel: {len(index_panel)} indexes, "
          f"60d available={sum(1 for r in index_panel if r['ret60'] is not None)}")

    # 6b. Limit ladder - pure, built from the pools already in hand
    limit_ladder = build_limit_ladder(
        zt_today, zb_today, prev_codes or [], market_date,
        limit_down=daily_stats[market_date].get("dt"))
    m = limit_ladder["metrics"]
    print(f"[6/8] ladder: ZT={m['limit_up']} ZB={m['zha_ban']} "
          f"seal={m['seal_rate']}% promote={m['promotion_rate']}% max={m['max_board']}板")

    # 7. Margin (T+1) + global markets - both soft dependencies
    notes = list(RISK_NOTES)
    margin: dict[str, Any] | None = None
    try:
        margin = build_margin_view(fetch_margin_history())
        print(f"[7/8] margin: {margin['balance']}亿元 ({margin['change']:+.2f}) as_of={margin['as_of']}")
        sources.append({
            "name": "东方财富数据中心 融资融券历史汇总（RPTA_RZRQ_LSHJ）",
            "as_of": f"{margin['as_of']}（T+1 披露）",
            "note": "沪深两市合计融资融券余额，单位亿元；交易所 T+1 披露，数据日期落后卡片市场日。",
        })
    except Exception as exc:  # noqa: BLE001
        print(f"[7/8] margin unavailable ({type(exc).__name__}) -> block degraded")
        notes.append("两融余额本次未采集成功（东方财富数据中心接口异常），该栏留空，下次运行时自动补齐。")

    global_markets: list[dict[str, Any]] = []
    global_as_of = None
    us_state = "unknown"
    try:
        em_quotes = fetch_em_global()
        sina_raw = fetch_sina_batch([c for c, *_ in SINA_GLOBAL_CODES])
        tx_quotes = fetch_tencent_quotes(["hkHSTECH"])
        global_markets = build_global_rows(em_quotes, sina_raw, tx_quotes)
        # Sources return dates in different formats (2026-08-31 vs 2026/08/31);
        # normalise before picking the newest, and keep only the calendar day.
        stamped = [r["as_of"][:10].replace("/", "-") for r in global_markets if r.get("as_of")]
        global_as_of = max(stamped) if stamped else None
        if global_as_of:
            for row in global_markets:
                if row.get("as_of"):
                    row["as_of"] = row["as_of"][:10].replace("/", "-")
                    row["lagged"] = row["as_of"] < market_date
        filled = sum(1 for r in global_markets if r["pct"] is not None)
        us_state = global_session_state(market_date)
        print(f"[7/8] global: {filled}/{len(global_markets)} symbols priced, "
              f"as_of={global_as_of}, US session={us_state}")
        sources.append({
            "name": "东方财富延时集群 / 新浪财经 / 腾讯行情（全球市场）",
            "as_of": global_as_of or "采集时刻",
            "note": "美股与欧股为北京时间次日凌晨收盘价，亚太与港股为当日收盘；"
                    "美债收益率取自美国财政部官方日收益率曲线，单列一节。",
        })
        if us_state == "intraday":
            notes.append(f"采集时刻（北京时间 {datetime.now():%Y-%m-%d %H:%M}）美股 {market_date} 交易时段尚未收盘"
                         "（美东 16:00 收盘，约北京时间次日 05:00），全球市场章节中的美股数值为最新盘中价，"
                         "不是收盘价；需要收盘口径请在次日 05:00 后重跑采集。")
    except Exception as exc:  # noqa: BLE001
        print(f"[7/8] global unavailable ({type(exc).__name__}) -> block degraded")
        notes.append("全球市场行情本次未采集成功，该章节为空；该块为软依赖，不影响其余章节。")
    else:
        missing = [r["name"] for r in global_markets if r["pct"] is None]
        if missing:
            notes.append(f"全球市场中 {len(missing)} 个标的本次未取到报价（{'、'.join(missing)}），以「—」显示。")

    # 7b. Dragon-Tiger - exchange-disclosed seats, soft dependency.
    #     Only published for the market date once the exchange has released it
    #     (usually around 18:00 Beijing), so an early run legitimately finds
    #     nothing and the block degrades to an explicit note.
    dragon_tiger: dict[str, Any] | None = None
    try:
        lhb_stocks = fetch_lhb_stocks(market_date)
        lhb_seats = fetch_lhb_seats(market_date)
        dragon_tiger = build_dragon_tiger(lhb_stocks, lhb_seats)
        try:
            codes = [row["code"] for row in dragon_tiger["stocks"] if row.get("code")]
            dragon_tiger["industry_map"] = {k: v for k, v in
                                            fetch_stock_industries(codes).items()
                                            if k in set(codes)}
        except Exception as exc:  # soft dependency: direction table degrades
            dragon_tiger["industry_map"] = {}
            print(f"[7b] industry lookup failed: {exc}")
        s = dragon_tiger["summary"]
        print(f"[7b] dragon-tiger: {dragon_tiger['stock_count']} stocks / "
              f"{dragon_tiger['seat_count']} seats / {dragon_tiger['broker_count']} brokerages "
              f"inst_net={s['inst_net_wan']}万 north_net={s['north_net_wan']}万")
        sources.append({
            "name": "东方财富数据中心 龙虎榜（RPT_DAILYBILLBOARD_DETAILSNEW + "
                    "RPT_BILLBOARD_DAILYDETAILSBUY）",
            "as_of": f"{market_date}（交易所披露）",
            "note": "个股榜与席位明细各一次请求全量取回（pageSize=500）；"
                    "席位明细含净买入与净卖出双向，机构专用/沪股通/深股通席位全量保留。",
        })
    except Exception as exc:  # noqa: BLE001
        print(f"[7b] dragon-tiger unavailable ({type(exc).__name__}) -> block degraded")
        notes.append("龙虎榜本次未采集成功（交易所尚未披露或接口异常），该章节留空；"
                     "交易所通常在当日 18:00 前后披露，收盘后重跑即可补齐。")

    # 7c. US Treasury yield curve - official CSV, soft dependency
    us_treasury: dict[str, Any] | None = None
    try:
        us_treasury = fetch_us_treasury()
        print(f"[7c] US Treasury: 10Y={us_treasury['y10']}% ({us_treasury['change_bp']:+.1f}bp) "
              f"2s10s={us_treasury['spread_2s10s_bp']}bp pct={us_treasury['percentile']}% "
              f"as_of={us_treasury['as_of']} via={us_treasury['source']}")
        sources.append({
            "name": f"美国财政部官方日收益率曲线（{us_treasury['source']}）",
            "as_of": us_treasury["as_of"],
            "note": "constant-maturity 收益率，单位为百分比；美东时间当日下午发布，"
                    "对应北京时间次日凌晨，故日期通常落后卡片市场日 1 个自然日。",
        })
    except Exception as exc:  # noqa: BLE001
        print(f"[7c] US Treasury unavailable ({type(exc).__name__}) -> block degraded")
        notes.append("美债收益率本次未采集成功（美国财政部官方 CSV 不可达），该栏留空，下次运行时自动补齐。")

    # 7d. PE valuation percentiles (CSIndex peg series, cached) - soft dependency
    valuation: list[dict[str, Any]] = []
    try:
        for code, name in CSINDEX_PE_INDEXES:
            view = build_valuation(code, name, market_date)
            if view:
                valuation.append(view)
        if valuation:
            pretty = "、".join(f"{v['name']} {v['pe']}({v['percentile']}%)"
                               for v in valuation)
            print(f"[7d] valuation: {pretty}")
            sources.append({
                "name": "中证指数有限公司官网 index-perf（指数市盈率 peg 序列，自算分位）",
                "as_of": f"{valuation[0]['as_of']} 收盘",
                "note": f"近 {PE_WINDOW_YEARS} 年 PE 历史百分位，样本 {valuation[0]['samples']} 个交易日；"
                        "历史序列本地缓存、每日增量追加（冷启动一次取全窗口）。",
            })
        else:
            raise RuntimeError("no percentile views produced")
    except Exception as exc:  # noqa: BLE001
        print(f"[7d] valuation unavailable ({type(exc).__name__}) -> block degraded")
        notes.append("估值分位本次未采集成功（中证官网接口异常或历史序列不足），该栏留空，下次运行时自动补齐。")

    # 7e. Share unlocks (解禁) within the next N calendar days - soft dependency
    lift_unlock: dict[str, Any] | None = None
    try:
        lift_start = market_date
        y, m, d = int(market_date[:4]), int(market_date[5:7]), int(market_date[8:10])
        lift_end = (datetime(y, m, d) + timedelta(days=LIFT_HORIZON_DAYS)).strftime("%Y-%m-%d")
        lift_unlock = build_lift_view(fetch_lift_rows(lift_start, lift_end),
                                      lift_start, lift_end)
        print(f"[7e] unlocks: {lift_unlock['event_count']} events / "
              f"{lift_unlock['total_cap_yi']}亿 in {lift_unlock['window']}")
        sources.append({
            "name": "东方财富数据中心 解禁时间表（RPT_LIFT_STAGE）",
            "as_of": f"{lift_unlock['window']}（预告口径）",
            "note": f"卡片市场日起 {LIFT_HORIZON_DAYS} 个自然日内的限售解禁事件；"
                    "市值为披露值（数据源更新时点价格），非最新收盘价。",
        })
    except Exception as exc:  # noqa: BLE001
        print(f"[7e] unlocks unavailable ({type(exc).__name__}) -> block degraded")
        notes.append("解禁排雷本次未采集成功（东财数据中心接口异常），该栏留空，下次运行时自动补齐。")

    if bootstrap_note:
        notes.append(bootstrap_note)
    if flow_status != "ok":
        notes.insert(0, "本次采集时东方财富行业资金流接口不可用（IP 限流或网络异常），资金流向与观察池章节为空，"
                        "请稍后重跑 scripts/collect_data.py 补齐。")
    else:
        gap = sorted(set(zt_sector) - {f["sector"] for f in flows})
        if gap:
            names = "、".join(gap[:6]) + ("等" if len(gap) > 6 else "")
            notes.append(f"当日涨停池中有 {len(gap)} 个行业归属（{names}）未匹配到行业板块列表，未计入分行业涨停家数。")

    payload = {
        "meta": {
            "market_date": market_date,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "demo": False,
            "sources": sources,
        },
        "market_days": market_days,
        "breadth": {"up": up, "down": down, "flat": flat,
                    "new_high": new_high, "new_low": new_low,
                    "limit_up": daily_stats[market_date].get("zt", 0),
                    "limit_down": daily_stats[market_date].get("dt", 0)},
        "flows": flows,
        "index_panel": index_panel,
        "limit_ladder": limit_ladder,
        "margin": margin,
        "global_markets": global_markets,
        "global_as_of": global_as_of,
        "global_us_session": us_state,
        "us_treasury": us_treasury,
        "dragon_tiger": dragon_tiger,
        "valuation": valuation,
        "lift_unlock": lift_unlock,
        "events": [],
        "scenarios": [],
        "risk_notes": notes,
    }

    out = RAW_DIR / f"eod-{market_date}.json"
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"[8/8] wrote {out.name}: sectors={len(flows)} days={len(market_days)} flows={flow_status}")
    print("requests by host:", json.dumps(REQUEST_COUNTS, ensure_ascii=False))
    print("next: python scripts/build_data.py")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"COLLECT FAILED: {exc!r}", file=sys.stderr)
        print("requests by host:", json.dumps(REQUEST_COUNTS, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
