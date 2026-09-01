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

import json
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta
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

REQUEST_COUNTS: dict[str, int] = {}

RISK_NOTES = [
    "主力资金净流入为东方财富单因子口径（超大单+大单），非交易所统一披露字段，不同供应商数值不可直接混用。",
    "行业分类为东方财富行业板块体系，与其他平台的申万/中信行业分类不一致。",
    "涨跌停家数以东财涨停池为准；上市首日等无涨跌幅限制个股不计入。",
    "5日新高/新低基于本地收盘价缓存计算，缓存不足时该指标显示为「—」，连续运行数日后生效。",
    "两市成交额 = 中证指数官网披露的上证指数成交额 + 深交所官网披露的深市股票成交额，与行情软件口径可能略有差异。",
    "指数区间收益由腾讯行情日线收盘价直接计算（不复权，指数点位本身已含成分调整），"
    "60 日窗口需连续日线，缓存重建后首日可能缺失。",
    "估值分位（PE/PB 历史百分位）无免费稳定数据源，本卡片不列示，不做任何估算。",
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
                    "美债收益率无免费稳定源，本章节不列示。",
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
