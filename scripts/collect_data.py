#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect real EOD market data for the decision card (incremental, multi-source).

Request budget (steady state, once per trading day):

    Tencent fqkline (index pct)          3 requests
    CSIndex official (SH turnover)       1 request   (range query)
    SZSE official (SZ turnover)          1 request   (today only; history cached)
    Eastmoney push2ex (ZT/DT pools)      2 requests  (today only; history cached)
    Sina hs_a list (breadth/closes)    ~56 requests  (paged at 100, 0.12s apart)
    Eastmoney push2 (sector flow rank)   1 request   (today only; history cached)

    Eastmoney total: 3 requests/day  (was ~97 in the naive design, which
    triggered IP-level throttling within minutes).

Caches under data/cache/ make this possible:
    daily_stats.json          per-date ZT/DT counts + SZ turnover
    sector_flow_history.json  per-sector daily main-force net inflow (yuan)
    breadth_history.json      per-date close prices (5d new-high/low)

On a fresh install the sector-flow history is backfilled once (~86 requests,
paced); afterwards it is never re-fetched. Every Eastmoney call degrades
gracefully: the snapshot is still emitted with an explicit note.

Standard library only. Run:  python -X utf8 scripts/collect_data.py
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.request
from datetime import datetime
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

TENCENT_INDEXES = [("sh000001", "shanghai"), ("sz399006", "chinext"), ("sh000688", "star50")]
KEEP_DAYS = 40          # trading days retained in caches
BACKFILL_COVER5 = 4     # a sector needs >=4 of last 5 days to be shown
BACKFILL_COVER20 = 16   # and >=16 of last 20 days

REQUEST_COUNTS: dict[str, int] = {}

RISK_NOTES = [
    "主力资金净流入为东方财富单因子口径（超大单+大单），非交易所统一披露字段，不同供应商数值不可直接混用。",
    "行业分类为东方财富行业板块体系，与其他平台的申万/中信行业分类不一致。",
    "涨跌停家数以东财涨停池为准；上市首日等无涨跌幅限制个股不计入。",
    "5日新高/新低基于本地收盘价缓存计算，缓存不足时该指标显示为「—」，连续运行数日后生效。",
    "两市成交额 = 中证指数官网披露的上证指数成交额 + 深交所官网披露的深市股票成交额，与行情软件口径可能略有差异。",
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
    for symbol, key in TENCENT_INDEXES:
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={symbol},day,,,{KEEP_DAYS},qfq")
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
    pool = "ZT" if kind == "zt" else "DT"
    sort = "fbt%3Aasc" if kind == "zt" else "fund%3Aasc"
    url = ("https://push2ex.eastmoney.com/getTopic" + pool + "Pool?cb=&"
           "ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&"
           f"Pageindex=0&pagesize=10000&sort={sort}&date={yyyymmdd}")
    return (get_json(url).get("data") or {}).get("pool") or []


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
    """Eastmoney sector ranking: ALL sectors' today main-force flow in ONE request.

    f62 today main net inflow (yuan) - verified identical to the per-sector
    fflow/daykline endpoint. f3 sector pct change.
    """
    url = ("https://push2.eastmoney.com/api/qt/clist/get?"
           "pn=1&pz=200&po=1&np=1&fltt=2&invt=2&fid=f62&fs=m:90+t:2&"
           "fields=f12,f14,f3,f62")
    diff = (get_json(url, retries=2).get("data") or {}).get("diff", [])
    out = []
    for item in diff:
        if not isinstance(item, dict) or not item.get("f12"):
            continue
        flow = item.get("f62")
        out.append({
            "code": item["f12"], "name": item["f14"],
            "pct": float(item["f3"]) if isinstance(item.get("f3"), (int, float)) else 0.0,
            "today_yuan": float(flow) if isinstance(flow, (int, float)) else None,
        })
    return out


def backfill_sector_history(code: str) -> dict[str, float]:
    """One-time per sector: full fflow daykline -> {date: main_net_inflow_yuan}."""
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?"
           f"lmt=0&klt=101&secid=90.{code}&fields1=f1,f2,f3,f7&"
           "fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65")
    data = get_json(url, retries=2).get("data") or {}
    return {line.split(",")[0]: float(line.split(",")[1])
            for line in data.get("klines", [])}


# ---------------------------------------------------------------- pure logic
def flow_coverage(daily: dict[str, float], trading_dates: list[str]) -> tuple[int, int]:
    """(covered_5d, covered_20d) for a sector's cached daily map."""
    window5, window20 = trading_dates[-5:], trading_dates[-20:]
    return (sum(1 for d in window5 if d in daily),
            sum(1 for d in window20 if d in daily))


def build_flow_rows(sectors_today: list[dict[str, Any]],
                    history: dict[str, dict[str, Any]],
                    trading_dates: list[str],
                    zt_sector: dict[str, int]) -> list[dict[str, Any]]:
    """Assemble schema flows[] from today's ranking + cached history. Pure."""
    market_date = trading_dates[-1]
    rows = []
    for sec in sectors_today:
        entry = history.get(sec["code"]) or {}
        daily = entry.get("daily", {})
        if sec["today_yuan"] is not None:
            daily = dict(daily)
            daily[market_date] = sec["today_yuan"]  # today wins over cache
        cov5, cov20 = flow_coverage(daily, trading_dates)
        if market_date not in daily:
            continue
        if cov5 < BACKFILL_COVER5 or cov20 < BACKFILL_COVER20:
            continue
        window5, window20 = trading_dates[-5:], trading_dates[-20:]
        rows.append({
            "sector": sec["name"],
            "today": round(daily[market_date] / 1e8, 1),
            "day5": round(sum(daily.get(d, 0.0) for d in window5) / 1e8, 1),
            "day20": round(sum(daily.get(d, 0.0) for d in window20) / 1e8, 1),
            "change_pct": round(sec["pct"], 2),
            "limit_up": zt_sector.get(sec["name"], 0),
        })
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
    print(f"[1/6] market date: {market_date}")
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
    print(f"[2/6] turnover {market_date}: {turnover[market_date]} trillion CNY")
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

    # 3. ZT/DT pools - 2 requests today + cache for the display window
    for d in window6:
        entry = daily_stats.setdefault(d, {})
        if "zt" not in entry:
            entry["zt"] = len(fetch_pool("zt", d.replace("-", "")))
        if "dt" not in entry:
            entry["dt"] = len(fetch_pool("dt", d.replace("-", "")))
        if "zt" in entry and "dt" in entry:
            print(f"[3/6] pools {d}: ZT={entry['zt']} DT={entry['dt']}")
    zt_today = fetch_pool("zt", market_date.replace("-", ""))  # full list for sector attribution
    zt_sector: dict[str, int] = {}
    for item in zt_today:
        name = item.get("hybk") or "未知"
        zt_sector[name] = zt_sector.get(name, 0) + 1
    daily_stats[market_date]["zt"] = len(zt_today)
    sources.append({
        "name": "东方财富 涨停/跌停池接口（push2ex）",
        "as_of": f"{market_date} 收盘",
        "note": "涨跌停家数按东财涨停池判定，行业归属按池内 hybk 字段。",
    })

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
    print(f"[4/6] breadth: up={up} down={down} flat={flat} total={len(stocks)}")
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
        print("[4/6] new-high/low: cache insufficient -> null this run")

    # 5. Sector flows - 1 request today + cache; one-time backfill if needed
    flows: list[dict[str, Any]] = []
    flow_status = "ok"
    sector_history: dict[str, dict[str, Any]] = load_cache(SECTOR_FLOW_CACHE)
    try:
        sectors_today = fetch_sector_today()
    except Exception as exc:  # noqa: BLE001
        print(f"[5/6] sector ranking unavailable ({type(exc).__name__}) -> flows degraded")
        sectors_today = []
        flow_status = "throttled"

    if sectors_today:
        # merge today's flow into cache
        for sec in sectors_today:
            entry = sector_history.setdefault(sec["code"], {"name": sec["name"], "daily": {}})
            entry["name"] = sec["name"]
            if sec["today_yuan"] is not None:
                entry["daily"][market_date] = sec["today_yuan"]
        # one-time backfill for sectors lacking 20d coverage (paced, best effort)
        need = [s for s in sectors_today
                if flow_coverage(sector_history.get(s["code"], {}).get("daily", {}), dates_all)[1]
                < BACKFILL_COVER20]
        if need:
            print(f"[5/6] backfilling {len(need)} sector(s) history (one-time)...")
            for i, sec in enumerate(need):
                try:
                    daily = backfill_sector_history(sec["code"])
                    if daily:
                        sector_history.setdefault(sec["code"], {"name": sec["name"], "daily": {}})
                        sector_history[sec["code"]]["daily"].update(daily)
                except Exception:  # noqa: BLE001 - throttled mid-backfill
                    print(f"  WARN backfill stopped at {i + 1}/{len(need)} (throttled)")
                    break
                time.sleep(0.35)
        prune_history({c: e["daily"] for c, e in sector_history.items()}, keep_dates)
        save_cache(SECTOR_FLOW_CACHE, sector_history)
        flows = build_flow_rows(sectors_today, sector_history, dates_all, zt_sector)
        if not flows:
            flow_status = "throttled"

    if flow_status == "ok":
        print(f"[5/6] sector flows: {len(flows)} sectors")
        sources.append({
            "name": "东方财富 行业板块主力资金流接口",
            "as_of": f"{market_date} 收盘",
            "note": "当日净流入来自板块资金排行（单次请求全量）；5日/20日为本地缓存逐日累加。主力=超大单+大单（东财单因子口径）。",
        })
    else:
        print("[5/6] sector flows: UNAVAILABLE -> empty with note")

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

    notes = list(RISK_NOTES)
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
        "events": [],
        "scenarios": [],
        "risk_notes": notes,
    }

    out = RAW_DIR / f"eod-{market_date}.json"
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"[6/6] wrote {out.name}: sectors={len(flows)} days={len(market_days)} flows={flow_status}")
    print("requests by host:", json.dumps(REQUEST_COUNTS, ensure_ascii=False))
    print("next: python scripts/build_data.py")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"COLLECT FAILED: {exc!r}", file=sys.stderr)
        print("requests by host:", json.dumps(REQUEST_COUNTS, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
