---
name: a-share-decision-card
description: 生成 A 股每日复盘决策卡（静态 HTML）。当用户要求"生成今日复盘卡片""跑一下决策卡""更新 A 股复盘"时使用。零依赖 Python+静态页，多源免费行情（腾讯/中证/深交所/东财/新浪），本地增量缓存把东财请求数压到 3 次/日以规避 IP 限流。
version: 1.0.0
---

# A 股每日复盘决策卡

仓库：<https://github.com/Yyh3/a-share-daily-decision-card>（先 clone 到本地）

产出：单页复盘决策卡（核心裁决 → 近五日市场 → 资金流向 → 市场广度 → 蓄力观察池 → 来源与风险），可打印导出 PDF。

## Steps

1. 采集真实数据（联网，交易日晚间或次日运行）：

   ```powershell
   cd <repo>
   python -X utf8 scripts/collect_data.py
   ```

   输出 `data/raw/eod-YYYY-MM-DD.json`。脚本自动识别最近已完成交易日。

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

4.（可选）离线单测：`python -X utf8 scripts/test_logic.py`，全过应输出 ALL TESTS PASSED。

## 判断采集结果

- 末行会打印 `requests by host`：稳态应为 腾讯3 / 中证1 / 深交所0-1 / push2ex 1-2 / 新浪~58 / push2 1。
- `sector flows: UNAVAILABLE` 表示东财被限流——快照仍生成，资金流与观察池章节为空、页面自动标注。等数小时后重跑第 1 步即可补齐，缓存不会重复请求。
- 首次运行会一次性回填 ~90 个板块资金流历史（~90 次请求、0.35s 间隔），属预期。
- 新高新低前几个交易日显示「—」，需缓存积累约 3 个交易日。

## Pitfalls

- **Windows 必须加 `-X utf8`**：GBK 控制台打印中文会崩（UnicodeEncodeError）。
- **东财限流是 IP 级**：push2/push2his 短时连续请求（全量拉取约 97 次）几分钟内即触发，全部编号镜像同封，持续数小时。绝不要写循环重试轰炸；采集器的缓存设计已把每日东财请求压到 3 次，不要绕过缓存重新全量拉取。
- **多源口径不可混**：主力资金=东财单因子（超大单+大单）；行业分类=东财板块体系；两市成交额=中证官网沪值+深交所官网深值。替换数据源时必须整项替换并更新 `meta.sources`。
- **数值必须是 JSON number**：涨跌幅、资金额不要写成带 `%` 或"亿元"的字符串。
- 资讯事件（events）与次日情景（scenarios）属于离线分析层，采集器留空；需要时人工或 LLM 按 `data/raw/sample.json` 的 schema 填入快照 JSON 的对应数组，再重跑第 2 步。

## Verification

1. `data/raw/eod-*.json` 存在且 `meta.demo` 为 false；
2. `build_data.py` 输出 `built data\market-card.json from eod-*.json`；
3. 页面顶部横幅显示「真实行情快照」，hero 显示交易日与情绪标签；
4. 近五日市场表 5 行、广度面板有数（新高新低为「—」属前几日预期）；
5. 交叉核对一个已知数（如两市成交额 ≈ 沪指当日成交 + 深市成交）。
