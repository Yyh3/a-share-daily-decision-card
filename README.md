# A 股每日复盘决策卡（A-Share Daily Decision Card）

零依赖静态网页：每个交易日收盘后运行一次采集脚本，自动生成一张包含「核心裁决 → 近五日市场 → 资金流向 → 市场广度 → 蓄力观察池 → 来源与风险」的复盘决策卡，可打印导出 PDF。

灵感来自 [WorkBuddy 发布的「A股每日复盘决策卡」](https://www.workbuddy.link/p/JB4JF3f3Wu6Ofxdle4w9rp)（[实际报告样例](https://workbuddy-space-static.codebuddy.work/page/JB4JF3f3Wu6Ofxdle4w9rp/0/decision-card-2026-08-28.html)）。本项目没有复制其品牌、专有节点属性或发布外壳，只复用了「数据证据 → 结论 → 动作 → 风险」的信息组织方式。

## 快速开始

```powershell
python -X utf8 scripts/collect_data.py   # 采集真实 EOD 数据（联网，多源容错）
python scripts/build_data.py             # 离线规则计算
python -m http.server 8000               # 本地预览
```

浏览器打开 `http://localhost:8000/`。打印 / 导出 PDF 用右上角按钮（目标打印机选「另存为 PDF」）。不要直接双击 `index.html`——浏览器会阻止 `fetch` 本地 JSON。

只跑 `build_data.py` 也可以：没有真实快照时自动回退到演示数据 `sample.json`。`build_data.py` 完全不联网。

运行离线测试（不需要网络）：

```powershell
python -X utf8 scripts/test_logic.py
```

## 数据源与请求预算

全部使用免费公开接口，无 API key。为避免单一供应商限流，按数据项分源，并用**本地增量缓存**把每日请求数压到最低：

| 数据项 | 来源 | 稳态请求数/日 |
|---|---|---|
| 指数涨跌幅（上证/创业板指/科创50） | 腾讯 fqkline | 3 |
| 沪市成交额 | [中证指数官网](https://www.csindex.com.cn) index-perf | 1 |
| 深市成交额 | [深交所官网](https://www.szse.cn) 每日行情 | 1（历史走缓存） |
| 涨停/跌停家数、分行业涨停 | 东方财富 push2ex 涨停池 | 2（历史走缓存） |
| 涨跌家数（广度）、新高新低 | 新浪 A 股全列表 | ~58（100/页分页） |
| 行业主力资金流（当日/5日/10日） | 东方财富板块资金排行 | ~5（分页 100/页；实时集群失败自动切延时集群 push2delay） |

东方财富合计 **约 7 次/日**（naive 全量拉取约 97 次/日，实测数分钟内即触发 IP 级限流）。资金流的当日/5日/10日直接来自东财排行接口的官方聚合字段（f62/f164/f174），无需历史回填；当实时集群（push2）被限流时自动切换延时集群（push2delay），收盘后两者数值完全一致。

**缓存文件**（`data/cache/`，可随时删除重建）：

- `daily_stats.json` — 每日涨跌停数、深市成交额
- `sector_flow_history.json` — 每板块当日主力净流入（缓存，未来自算更长窗口用）
- `breadth_history.json` — 每日全市场收盘价（用于 5 日新高/新低，需连续运行约 3 个交易日生效）

## 架构

```text
公开接口（腾讯/中证/深交所/东财/新浪）
    ↓ scripts/collect_data.py（联网；按数据项分源；东财仅 3 次请求/日）
data/raw/eod-YYYY-MM-DD.json（+ 演示 sample.json）
    ↓ scripts/build_data.py（离线，确定性规则，字节稳定输出）
data/market-card.json
    ↓ app.js → index.html（纯展示，不推断结论）
```

职责边界：采集层只产出原始 JSON；构建脚本负责所有确定性指标和规则；前端只渲染。来源与截止时间强制写入 `meta.sources`。

## 数据 schema

顶层字段（完整示例见 `data/raw/sample.json`）：

- `meta`：`market_date`、`updated_at`、`demo`、`sources[]`（name/as_of/note）
- `market_days[]`：date、上证/创业板/科创50 涨跌幅、成交额（万亿元）、涨跌停数、盘面特征
- `breadth`：上涨、下跌、平盘、涨停、跌停、5日新高、5日新低
- `flows[]`：sector、today/day5/day10 主力净流入（亿元，JSON number）、change_pct、limit_up
- `events[]`：标题、级别、方向、摘要、产业传导、证据、风险（离线分析层，采集器留空）
- `scenarios[]`：情景名、概率、触发条件、动作（离线分析层，采集器留空）
- `risk_notes[]`：页面底部风险提示

## 确定性规则

- **情绪**：上涨占比、涨跌停差、成交额环比按固定阈值加分（0–5），映射 退潮/偏弱/修复/活跃/高潮
- **资金持续性**：5 日与 10 日资金正负组合 → 持续流入/拐点回流/拐点撤退/持续流出
- **蓄力观察池**：`5日净流入>0` 且 `|当日涨幅|<2%` 且 `涨停<3` 家，按固定公式评分排序

规则可解释、可复现：同一输入产出字节稳定的 JSON（字段排序、候选排序均固定）。规则不预测收益。

## 已知限制

1. 「主力资金净流入」是东方财富单因子口径（超大单+大单），非交易所统一披露字段，不同供应商数值不可混用；
2. 行业分类为东财板块体系，与申万/中信不一致；
3. 资讯事件与次日情景属于离线分析层，需人工或 LLM 按 schema 填入快照 JSON；
4. 免费接口无 SLA；东财实时集群被限流时自动切换延时集群（收盘后数值一致），资金流彻底不可得时降级为空并在页面标注，其余数据不受影响；
5. 免费接口可能改版；采集器按数据项分源，单源失效不影响其他数据项。

## 风险声明

本项目为数据整理与规则演示，不构成投资建议。规则评分不预测收益，不能替代数据核验、风险管理或独立判断。

## License

MIT
