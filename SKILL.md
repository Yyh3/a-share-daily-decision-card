---
name: a-share-decision-card
description: 生成 A 股每日复盘决策卡（静态 HTML）。当用户要求"生成今日复盘卡片""跑一下决策卡""更新 A 股复盘"时使用。零依赖 Python+静态页，多源免费行情（腾讯/中证/深交所/东财/新浪），本地增量缓存+延时集群容错把东财请求数压到约 12 次/日以规避 IP 限流；卡片覆盖核心裁决、盘面全景、风格坐标、涨停梯队、两融、全球市场联动等十余章节。
version: 1.1.0
---

# A 股每日复盘决策卡

仓库：<https://github.com/Yyh3/a-share-daily-decision-card>（先 clone 到本地）

产出：单页复盘决策卡（核心裁决 → 盘面全景与风格坐标 → 近五日市场 → 资金流向 → 涨停梯队与情绪结构 → 全球市场联动 → 市场广度与两融 → 蓄力观察池 → 次日情景预案 → 来源与风险），可打印导出 PDF。

## Steps

1. 采集真实数据（联网，交易日晚间或次日运行；A 股 15:00 收盘后约 30 分钟即可，美股 8-31 行情需北京 9-1 05:00 后再跑）：

   ```powershell
   cd <repo>
   python -X utf8 scripts/collect_data.py
   ```

   输出 `data/raw/eod-YYYY-MM-DD.json`。脚本自动识别最近已完成交易日。末行打印 `requests by host`。

2. 规则计算（离线）：

   ```powershell
   python scripts/build_data.py
   ```

   输出 `data/market-card.json`。真实快照（demo=false）优先于演示 sample.json。

3. 本地预览：

   ```powershell
   python -m http.server 8000
   ```

   浏览器打开 `http://localhost:8000/`。不要双击 index.html（file:// 下 fetch 被拦截）。

4.（可选）离线单测：`python -X utf8 scripts/test_logic.py`，全过应输出 ALL TESTS PASSED（84 项）。

## 判断采集结果

- 末行会打印 `requests by host`：稳态应为 腾讯7 / 中证1 / 深交所0-1 / push2ex 3 / 新浪~58 / push2 1 + push2delay ~6（资金流分页）/ datacenter-web 1 / hq.sinajs.cn 1 / qt.gtimg.cn 1。
- `sector flows: UNAVAILABLE` 表示东财被限流——快照仍生成，资金流与观察池章节为空、页面自动标注。等数小时后重跑第 1 步即可补齐，缓存不会重复请求。
- 资金流当日/5日/10日直接来自东财排行接口官方聚合字段（f62/f164/f174），无需历史回填；实时集群 push2 被限时自动切 push2delay（收盘后数值一致）。
- 涨停梯队硬依赖：ZT 池提供 lbc / zttj / fund / zbc / hybk；新增 ZB 池构成封板率分母；晋级率靠本地 `zt_codes` 缓存的每日涨停代码列表。
- 两融（margin）软依赖 T+1：从东方财富数据中心 `RPTA_RZRQ_LSHJ` 取，失败时整块为空并在风险栏标注「下次运行时自动补齐」。
- 全球市场软依赖：三个批量接口（push2delay 10 指数 + 新浪 6 商品汇率 + 腾讯 恒生科技），任何一项失败整块降级。
- 美股在美东 16:00 收盘（约北京时间次日 05:00）；若采集时美股尚未收盘，页面会明示「美股为最新盘中价，非收盘价」，并标 `global_us_session: "intraday"`。建议在 A 股收盘后 ≥ 1 小时再跑以避免盘中价误读。
- 新高新低前几个交易日显示「—」，需缓存积累约 3 个交易日。

## Pitfalls

- **Windows 必须加 `-X utf8`**：GBK 控制台打印中文会崩（UnicodeEncodeError）。
- **东财限流是 IP 级**：push2/push2his 短时连续请求（全量拉取约 97 次）几分钟内即触发，全部编号镜像同封，实测封禁可持续 24h+。绝不要写循环重试轰炸；push2delay 延时集群与 datacenter-web 不受此限流影响（采集器已内置自动切换），但不要高频滥用。
- **多源口径不可混**：主力资金=东财单因子（超大单+大单）；行业分类=东财板块体系；两市成交额=中证官网沪值+深交所官网深值；两融=东财数据中心历史汇总（T+1）。替换数据源时必须整项替换并更新 `meta.sources`。
- **美债 10 年收益率**无免费稳定源（东财延时集群所有候选 secid 都返空，新浪 `globalbd_*` 与 `gb_$tnx` 也无数据），本卡片不列示，不做估算。
- **PE/PB 历史分位**属申万 / 中证指数的内部付费口径，本卡片不列示，已在风险栏注明。
- **数值必须是 JSON number**：涨跌幅、资金额不要写成带 `%` 或"亿元"的字符串。
- 资讯事件（events）与次日情景（scenarios）属于离线分析层，采集器留空；需要时人工或 LLM 按 `data/raw/sample.json` 的 schema 填入快照 JSON 的对应数组，再重跑第 2 步。
- 升级 `app.js` 后必须同步改 `index.html` 里 `app.js?v=...` 版本参数——否则浏览器缓存旧脚本会导致数值显示为 `NaN`。

## Verification

1. `data/raw/eod-*.json` 存在且 `meta.demo` 为 false；
2. `build_data.py` 输出 `built data\market-card.json from eod-*.json`；
3. 页面顶部横幅显示「真实行情快照」，hero 显示交易日与情绪标签；
4. 指数面板 7 行、资金表约 20 行、涨停梯队含封板率/晋级率/最高连板、广度面板 + 两融块、全球市场 17 行；
5. 交叉核对一个已知数（如两市成交额 ≈ 沪指当日成交 + 深市成交；涨停家数 = 东财 ZT 池数量）；
6. 新增 `data/cache/daily_stats.json` 含 `zt_codes` 数组，次日重跑后晋级率有值。
