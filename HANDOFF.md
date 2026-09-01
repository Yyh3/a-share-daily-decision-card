# A 股每日复盘决策卡 — 项目 Handoff

> 更新时间：2026-09-01 01:05（UTC+8）  
> 项目状态：**P1 缺口补齐完成；四块数据层（涨停梯队 / 指数多窗口+风格坐标 / 两融 / 全球市场）已全部接入并实盘验证 2026-08-31 快照**

## 0.4 决策卡缺口补齐（2026-09-01 凌晨）

对照参考卡（2026-08-28 完全体版），原 30% 覆盖扩到 ~70%。用户决策（2026-08-31）：四块数据层全做，分析层（事件卡全字段、主线判定、情景剧本、验证清单）暂缓。完成项：

### 0.4.1 涨停梯队与情绪结构（数据已有，硬依赖）

- `collect_data.py` 解析 push2ex 涨停池时新增 `lbc`（连板数）、`zttj`（n天m板）、`fund`（封单额）、`zbc`（炸板次数）、`hybk`（行业归属）五个字段；
- 新增 `fetch_pool("zb", date)` 拉取东财炸板池（1 次/日），构成封板率分母；
- 每日涨停股代码列表写入 `data/cache/daily_stats.json`（`zt_codes`），与前一日列表求交集即得晋级率；冷启动多取一次历史涨停池；
- `build_data.py` 调纯函数 `build_limit_ladder` 排序生成天梯（按 board 降序、同板按封单/成交额）、计算 `seal_rate`/`promotion_rate`/`max_board`/`two_board_plus` 四项情绪指标；
- 页面新增「04 涨停梯队与情绪结构」章节，6 个 metric 卡 + 连板层级 chip + 天梯表格 + 口径说明；
- 核心裁决增加「情绪结构」条，提示最高连板、封板率、晋级率。

### 0.4.2 指数多窗口 + 风格坐标（硬依赖）

- `TENCENT_INDEXES` 扩到 7 项（上证 / 深证成指 / 创业板 / 科创50 / 沪深300 / 中证1000 / 中证红利）；`KLINE_LIMIT` 从 40 提到 70，60 日窗口需 61 根 K 线；
- 新增纯函数 `window_returns` 与 `build_index_panel`，输出 `{name, close, pct, ret5, ret20, ret60}`；
- 风格坐标属确定性规则：放在 `build_data.py` 的 `style_view`，按 20 日 `中证1000 - 沪深300` 与 `中证红利 - 沪深300` 差值，按 ±1.0pct 阈值归类（小盘占优 / 大盘占优 / 红利价值占优 / 成长占优 / 差异不显著），仅输出规则化描述，不做叙事；
- **明确放弃**：PE/PB 历史分位无免费稳定数据源（参考卡用的是申万 / 中证指数的内部口径），卡片不列示，已在风险栏注明。

### 0.4.3 两融余额（软依赖，T+1）

- 优先东方财富数据中心 `datacenter-web.eastmoney.com` 的 `RPTA_RZRQ_LSHJ` 接口（与被限流的 `push2` / `push2his` 不同域），1 次请求返回近月历史；解析得到 `RZRQYE`（融资融券余额）、`RZYE`（融资余额）、`RQYE`（融券余额）、`RZJME`（融资净买入）、`RZYEZB`（占流通市值比）；
- 失败时整块降级为空，风险栏标注「下次运行时自动补齐」；
- 页面在「07 市场广度」下方嵌入两融块：余额 / 环比 / 占流通市值 三张卡 + T+1 披露说明；
- **明确放弃**：参考卡的「国家队 / 汇金持仓」属公募中报 + ETF 份额公示的人工汇编，两融以外没有免费稳定源，本期不做；龙虎榜席位级解码同理（属 datacenter + 分析层）。

### 0.4.4 全球市场联动（软依赖）

- 三个数据源、3 次请求：
  1. `push2delay.eastmoney.com/api/qt/ulist.np/get`（批量 secid）→ 10 个美 / 欧 / 亚太 / 汇率指数；
  2. `hq.sinajs.cn`（批量 list）→ 费城半导体 + 伦敦金 / 银 / 纽约原油 / LME 铜 / 离岸人民币；
  3. `qt.gtimg.cn/q=` 批量 → 恒生科技；
- `build_global_rows` 按 `gb_` / `hf_` / `fx_` 前缀分流解析（fx 直接取 `parts[10]`，hf 拿 `parts[0]` 现价 + `parts[7]` 昨收算 pct，gb 拿 `parts[1]`/`parts[2]`）；
- 时区对齐：纯函数 `global_session_state(market_date, now)` 根据美东 16:00 = 北京时间次日 05:00 的保守边界判断美股是否已收盘，**未收盘则在页面风险栏明示「美股为最新盘中价，非收盘价」**——首次实盘时发现 00:40 北京周一是美股周一午盘，按惯例不应当作收盘报，这是本次补齐里发现并修掉的一处口径陷阱；
- 数据日期早于卡片市场日的标的（如 LME 铜）会单独标 `[lagged YYYY-MM-DD]`，避免误读为当日行情；
- **明确放弃**：美债 10 年收益率（东财延时集群所有候选 secid 都返空，新浪 `globalbd_*` 与 `gb_$tnx` 也无数据），已在风险栏注明，不做估算。

### 0.4.5 schema 演进

`data/raw/eod-*.json` 顶层新增字段：

```json
"index_panel": [ {"key":"shanghai","name":"上证指数","date":"...","close":...,"pct":...,"ret5":...,"ret20":...,"ret60":...} ],
"limit_ladder": { "date":"...","ladder":[...],"distribution":{...},"metrics":{...},"notes":[...] },
"margin": { "balance":...,"change":...,"financing":...,"securities_loan":...,"financing_net_buy":...,
            "pct_of_float":...,"as_of":"...","prev_as_of":"...","note":"..." },
"global_markets": [ {"name":...,"category":...,"close":...,"pct":...,"as_of":...,"unit":...,"lagged":bool} ],
"global_as_of": "...",
"global_us_session": "intraday" | "closed"
```

### 0.4.6 请求预算（新增后稳态）

| 数据项 | 来源 | 稳态请求数/日 |
|---|---|---|
| 7 指数日线（K线 + 多窗口） | 腾讯 fqkline | 7 |
| 沪市成交额 | 中证指数官网 | 1 |
| 深市成交额 | 深交所官网 | 1（历史走缓存） |
| 涨停 / 跌停 / 炸板池 | push2ex | 3（当日 3 次 + 历史走缓存） |
| 涨跌家数 | 新浪 hs_a | ~58 |
| 行业主力资金流 | 东财板块排行 | ~6（实时 + 延时集群，paged at 100） |
| 两融余额 | 东财 datacenter | 1 |
| 全球 10 指数 | push2delay ulist.np（批量） | 1 |
| 全球 6 商品/汇率/费半 | 新浪 hq.sinajs.cn（批量） | 1 |
| 恒生科技 | 腾讯 qt.gtimg.cn | 1 |

合计：新增约 +3 次/日（margin / 全球批次 / 恒生科技）；炸板池 +1；总东财约 12 次/日，**仍以 push2delay 延时集群 + datacenter-web 为主，碰 push2 仅一次且带分页自动切换**。无 push2his。

### 0.4.7 测试与验收

- `scripts/test_logic.py` 扩到 84 项，新增覆盖：window_returns 区间收益与短历史 / build_index_panel 顺序与缺失 / build_limit_ladder 排序、分布、晋级率、零边界 / build_margin_view 单位换算与单行 / _sina_row 三种前缀 / build_global_rows 三源 / style_view 阈值与缺失 / global_session_state 边界 / 老快照降级；
- 软依赖降级实测（`scripts/_degrade_check.py` 一次性脚本，已删除）：人为将 margin + 全球 4 个 fetcher 替换为 `boom()`，主流程仍写快照，margin 字段 `null`、global_markets `[]`、风险栏两条降级提示，其余章节完整；DEGRADE CHECK PASS；
- 浏览器实测：本地 http://127.0.0.1:8000 渲染 4 个新章节（截图 648KB 验证完毕）。

## 0.3 资金流彻底打通（2026-08-31 深夜）

东财实时集群（push2/push2his）IP 封禁实测持续 24h+，但探测发现**延时集群 push2delay.eastmoney.com 不受该限流影响**，且 clist 排行接口在 push2delay 上同样可用：

- 资金流改从 clist 排行接口取**东财官方聚合字段**：f62 当日 / f164 5日 / f174 10日 主力净流入——分页请求（100/页，共 ~5 页）拿全 ~496 个板块，**彻底无需历史回填**；
- fetch_sector_today 先试 push2，失败自动切 push2delay（收盘后两集群数值完全一致）；
- schema 的 day20 改为 day10（列名、build_data 分类规则、观察池公式、前端、sample.json 同步更新）；
- 板块宇宙为全层级（一级"电子"⊃二级"消费电子"⊃三级"消费电子零部件及组装"，中信式 Ⅱ/Ⅲ 后缀），与参考页面用的二级行业口径一致；全量保留，展示层控制密度：资金表取 5 日净流入前 20（FLOW_TABLE_ROWS），观察池取前 10（POOL_ROWS），裁决与观察池计算仍用全量集合；
- 顺手修了两个问题：a) 前端 script 标签加版本参数（app.js?v=…）避免更新后浏览器缓存旧脚本渲染 NaN；b) 新高新低缓存不足 4 日时在风险栏标注实际覆盖天数；
- 东财每日请求稳态约 7 次（push2ex 1-2 + push2/push2delay 资金流分页 ~5）；
- 08-31 完整卡片：裁决 3 条（电子 5 日 +266.4 亿拐点回流居首 / 有色金属当日流出 57.6 亿最明显）、资金表 20 行、观察池 10 项，全部真实数据。

## 0.2 增量缓存重构（2026-08-31 凌晨）

为消除东财 IP 限流风险，`collect_data.py` 从"每日全量拉取"重构为"每日增量 + 本地缓存"：

- 新增缓存 `data/cache/daily_stats.json`（每日涨跌停数、深市成交额）与 `sector_flow_history.json`（每板块逐日主力净流入）；
- 板块资金流改用 clist 排行接口**单次请求**拿全部板块当日值（已验证与逐板块 fflow 日K线数值完全一致），5日/20日从缓存累加；
- 首次运行一次性回填 ~90 板块历史（0.35s 间隔限速），此后永不重复；
- 东财请求数从 ~97 次/日降到 **3 次/日**（实测稳态：push2ex 1 + push2 1 + 冷启动项走缓存）；
- 每次采集结束打印 `requests by host` 请求统计；
- 修复继承自初版的 bug：下跌日的量能方向判断写反（跌+缩量被标"放量下跌"）；
- 新增 `scripts/test_logic.py` 离线单测（24 项，覆盖覆盖率判定、缓存合并、today 覆盖缓存、降级路径、快照优先级）。

## 0.1 真实数据接入（2026-08-31）

1. 新增 `scripts/collect_data.py`：联网采集真实 EOD 数据，输出与 `sample.json` 同 schema 的 `data/raw/eod-YYYY-MM-DD.json`。
2. 多源容错架构（东财 push2/push2his 会被 IP 限流，实测封禁数小时且镜像域名全部同样被封）：
   - 指数涨跌幅 → 腾讯 fqkline；
   - 两市成交额 → 中证指数官网（沪）+ 深交所官网（深），比东财更权威；
   - 涨跌停池（含 hybk 行业归属）→ 东财 push2ex（与被限流的 push2his 不同域，实测不受影响）；
   - 涨跌家数 → 新浪 A 股全列表分页（5546 只，沪深北）；
   - 5日新高/新低 → 本地收盘价缓存（`data/cache/breadth_history.json`，由新浪昨收字段引导，连续运行约 3 个交易日后生效）；
   - 行业主力资金流（当日/5日/20日）→ 东财 push2his fflow，**软依赖**：被限流时快照照常生成，flows 为空并在页面风险栏提示重跑。
3. `build_data.py`：快照选择优先级改为 真实(demo=false) > 演示(demo=true)；flows 为空时核心裁决降级为「市场定性 + 数据缺口」两条。
4. 前端：顶部横幅按 `meta.demo` 动态切换演示/真实标识；新高新低为 null 时显示「—」；事件与情景为空时显示说明占位，不再渲染空白。
5. 已用 2026-08-28 真实收盘数据完成一次端到端验证（详见第 9 节验证记录）。

**注意事项（采集坑）：**

- 采集时不要短时间连续探测东财接口，容易触发 IP 级限流（RemoteDisconnected），封禁期内所有 push2/push2his 镜像域名均不可用，需等待解封（通常数小时内）。
- 运行采集必须加 `-X utf8`：`python -X utf8 scripts/collect_data.py`（Windows GBK 控制台打印中文会崩）。
- 深交所接口按单日查询（`txtQueryDate`），5 个展示日 + 1 个环比日共 6 次请求，均支持历史日期。
- 停牌股在新浪列表中 `trade=0`，采集器已过滤；广度口径为"有有效报价的个股"。
- 东财跌停池口径（收盘跌停）与部分行情软件（含盘中触及）不同，涨跌停家数与参考页面可能相差数家，属口径差异非错误。

## 1. 项目目标

参考 WorkBuddy 发布页中的“A股每日复盘决策卡”，实现一套可独立运行、可替换数据源、可打印为 PDF 的每日市场复盘页面。

参考页面：

- 发布页：<https://www.workbuddy.link/p/JB4JF3f3Wu6Ofxdle4w9rp>
- 实际静态报告：<https://workbuddy-space-static.codebuddy.work/page/JB4JF3f3Wu6Ofxdle4w9rp/0/decision-card-2026-08-28.html>

本项目没有复制 WorkBuddy 品牌、专有节点属性或发布外壳，只复用了“数据证据 → 结论 → 动作 → 风险”的信息组织方式。

---

## 2. 原页面分析结论

原链接由两层组成：

1. WorkBuddy 发布外壳；
2. 跨域 iframe 中的单文件静态 HTML 报告。

原报告约 120 KB，正文、CSS 和数据都固化在 HTML 内。浏览器加载时未观察到行情、新闻、龙虎榜或宏观数据 API 请求。因此其生成模式应理解为：

```text
采集行情与资讯
    ↓
离线分析和生成结论
    ↓
生成静态 HTML 快照
    ↓
上传并通过发布外壳展示
```

原报告共有 `00–09` 十个章节，覆盖：

- 核心裁决
- 近五日市场与轮动
- 当日盘面全景
- 行业资金流向
- 热点事件与产业传导
- 主线判定
- 涨停梯队和情绪结构
- 未来事件日历
- 全球市场联动
- 次日情景、验证清单和风险排雷

其最值得保留的结构是：

```text
结论 → 数据证据 → 操作语义 → 触发条件 → 失效条件 → 次日验证
```

---

## 3. 当前交付内容

项目根目录：

```text
C:\Users\xxw98\cola\coding-cola\project-20260830-233000-dcf14a8f
```

目录结构：

```text
project-20260830-233000-dcf14a8f/
├─ index.html                 # 页面结构与容器
├─ styles.css                # 视觉、响应式及打印样式
├─ app.js                    # 获取 JSON 并渲染页面
├─ README.md                 # 运行方式、schema、规则说明
├─ HANDOFF.md                # 本文档
├─ data/
│  ├─ market-card.json       # 构建后供前端读取的数据
│  ├─ raw/
│  │  ├─ sample.json         # 演示数据 schema 示例
│  │  └─ eod-2026-08-28.json # 真实采集快照（collect_data.py 产出）
│  └─ cache/
│     └─ breadth_history.json # 新高新低所需的收盘价缓存
└─ scripts/
   ├─ collect_data.py        # 联网采集（多源容错）
   └─ build_data.py          # 离线聚合和规则计算脚本
```

### 已实现功能

- 顶部市场日期、市场状态、情绪评分和成交额；
- 三条自动生成的核心裁决（资金流缺失时降级为两条）；
- 近五日指数、成交额、涨跌停和市场特征；
- 市场广度（新高新低依赖缓存，前几日显示「—」）；
- 行业当日、5 日、20 日资金流向；
- 资金持续性自动分类；
- 蓄力观察池自动筛选和排序；
- 热点事件及产业传导卡；
- 次日情景预案；
- 数据来源、更新时间和风险说明；
- 单章节及全部章节展开/收起；
- 浏览器打印与导出 PDF；
- 桌面双栏、移动端单栏；
- 窄屏表格横向滚动；
- 无外部 CDN、无前端框架、无运行时第三方请求；
- 真实快照与演示快照自动区分标识（`meta.demo`）。

---

## 4. 运行方式

在 PowerShell 中执行：

```powershell
cd C:\Users\xxw98\cola\coding-cola\project-20260830-233000-dcf14a8f
python -X utf8 scripts/collect_data.py   # 交易日收盘后运行：采集真实数据
python scripts/build_data.py
python -m http.server 8000 --bind 127.0.0.1
```

浏览器访问：

```text
http://127.0.0.1:8000/
```

不要直接双击 `index.html`。页面通过 `fetch` 读取 JSON，浏览器通常会阻止 `file://` 页面读取本地文件。

当前曾启动过本地服务；如果地址无法访问，重新执行上述 `http.server` 命令即可。

---

## 5. 数据流与职责边界

当前架构：

```text
公开接口（腾讯/中证/深交所/东财/新浪）
    ↓ scripts/collect_data.py（联网，多源容错，软依赖降级）
data/raw/eod-YYYY-MM-DD.json（+ 演示 sample.json）
    ↓ scripts/build_data.py
    ├─ 优先选真实快照（demo=false），同级别取最新 market_date
    ├─ 计算市场情绪
    ├─ 分类资金持续性
    ├─ 生成核心裁决（flows 为空时降级）
    └─ 筛选蓄力池
    ↓
data/market-card.json
    ↓
app.js
    ↓
index.html
```

边界约定：

- **采集层**负责生成原始 JSON；
- **构建脚本**负责确定性指标和规则；
- **前端**只负责展示，不自行推断市场结论；
- **来源和截至时间**必须写入 `meta.sources` 与 `meta.updated_at`；
- 演示数据不能伪装成实时数据。

---

## 6. 原始数据 Schema

完整示例见 `data/raw/sample.json`。

### `meta`

```json
{
  "market_date": "2026-08-28",
  "updated_at": "2026-08-30 17:00",
  "demo": true,
  "sources": [
    {
      "name": "数据源名称",
      "as_of": "数据截至时间",
      "note": "口径及限制"
    }
  ]
}
```

### `market_days[]`

每个交易日包括：

- `date`
- `shanghai`
- `chinext`
- `star50`
- `turnover`
- `limit_up`
- `limit_down`
- `feature`

### `breadth`

- `up`
- `down`
- `flat`
- `new_high`
- `new_low`
- `limit_up`
- `limit_down`

### `flows[]`

- `sector`
- `today`
- `day5`
- `day20`
- `change_pct`
- `limit_up`

资金单位当前统一为亿元；涨跌幅字段必须使用 JSON number，不要写带 `%` 的字符串。

### `events[]`

- `title`
- `level`
- `direction`
- `summary`
- `transmission`
- `evidence`
- `risk`

### `scenarios[]`

- `name`
- `probability`
- `trigger`
- `action`

### `risk_notes[]`

字符串数组，显示在来源和风险章节。

---

## 7. 当前规则

### 7.1 市场情绪

使用以下指标按固定阈值加分：

- 上涨家数占比；
- 涨停数减跌停数；
- 成交额较前一日变化。

得分映射为：

```text
退潮 / 偏弱 / 修复 / 活跃 / 高潮
```

当前是可解释的演示规则，不是训练模型，也不预测收益。

### 7.2 资金持续性

| 5 日资金 | 20 日资金 | 分类 |
|---|---|---|
| 正 | 正 | 持续流入 |
| 正 | 负 | 拐点回流 |
| 负 | 正 | 拐点撤退 |
| 负 | 负 | 持续流出 |

### 7.3 蓄力观察池

进入条件：

```text
5 日净流入 > 0
且 |当日涨幅| < 2%
且 涨停家数 < 3
```

候选再根据 5 日资金、20 日资金和当日涨幅进行固定公式排序。

---

## 8. 真实数据接入需求

### 最小可用版

至少需要：

1. 指数日线：收盘、涨跌幅、成交额、均线和区间收益；
2. 全市场广度：涨跌平、涨跌停、新高新低；
3. 行业行情：行业涨跌幅、成交额、上涨家数、涨停家数；
4. 行业资金：当日、5 日、20 日净流入；
5. 热点与连板：涨停原因、题材、连板高度、晋级率、炸板率；
6. 资讯事件：标题、来源、时间、原始 URL、关联行业；
7. 数据元信息：供应商、截止时间、复权方式、行业分类和单位。

### 接近参考页的增强版

还需要：

- 龙虎榜和营业部席位；
- 融资融券；
- ETF 份额申赎；
- 北向或其他跨境资金；
- 公司公告、业绩预告和监管函；
- 宏观经济日历；
- 美股、港股及亚太指数；
- 美债、美元、贵金属和工业金属；
- 解禁和财报披露日历。

### 可选供应商

商业或量化数据：

- Wind
- 同花顺 iFinD
- Choice
- TuShare Pro
- 聚宽
- 米筐
- 掘金量化

官方和公开来源：

- 上交所、深交所、北交所
- 巨潮资讯
- 国家统计局
- 中国人民银行
- 中证指数、国证指数

注意：“主力资金净流入”不是交易所统一字段。生产环境必须固定供应商、行业分类版本、大单定义、复权方式和统计窗口，不能直接混用不同平台口径。

---

## 9. 验证记录

原型阶段（2026-08-30）已执行并通过：

```powershell
python scripts/build_data.py
python -m json.tool data/market-card.json
node --check app.js
python -m py_compile scripts/build_data.py
```

真实数据接入阶段（2026-08-31）已执行并通过：

```powershell
python -X utf8 scripts/collect_data.py   # 产出 eod-2026-08-28.json
python scripts/build_data.py             # 输入文件切换为 eod-2026-08-28.json
python -m json.tool data/market-card.json
node --check app.js
python -m py_compile scripts/collect_data.py scripts/build_data.py
```

HTTP 与页面检查通过：

```text
GET /index.html                 → 200
GET /data/market-card.json      → 200
```

真实数据交叉验证（对照原参考页面 2026-08-28 报告中的已知数值）：

- 上证 08-28 涨跌幅 -0.11%、创业板 -1.41%、科创50 -1.85%：逐日一致；
- 两市成交额 5 日（2.01/1.83/1.81/2.13/2.10 万亿）：逐日一致（沪=中证官网 + 深=深交所官网）；
- 上涨家数 3013：一致；涨停 82 家：一致；跌停 1 家 vs 参考 3 家：口径差异（收盘跌停 vs 含盘中触及）；
- 新浪列表 5546 只 vs 东财 5905 只：东财含更多北交所/停牌口径，广度比例不受影响。

东财 IP 限流实测记录（2026-08-31 00:00–00:30）：push2his/push2 全部镜像域名（含编号镜像）对 urllib/requests/真实浏览器均返回 RemoteDisconnected / ERR_EMPTY_RESPONSE，确认 IP 级封禁而非 TLS 指纹拦截；同期 push2ex（涨停池）、腾讯、新浪、中证官网、深交所官网均正常。采集器已按此设计软依赖。

其他检查：

- 生成结果包含 2 条核心裁决（资金流降级模式）；
- 包含 5 个交易日（真实数据）；
- 关键 DOM ID 齐全；
- 未包含 WorkBuddy 品牌和 `data-page-node-id`；
- JavaScript 与 Python 语法检查通过；
- 浏览器实测页面渲染：真实横幅、状态面板、裁决、市场表、广度（新高新低显示「—」）、来源 5 项、风险提示含限流说明，均正常。

---

## 10. 已知限制与风险

1. 行业资金流依赖东财单一免费源，IP 限流时该章节为空（已自动标注），无备用口径；
2. 资讯事件（events）与次日情景（scenarios）仍为空，属于离线分析层，需人工或 LLM 辅助生成后按 schema 填入快照 JSON；
3. 5日新高/新低依赖本地缓存，前几个交易日显示「—」；
4. 没有 JSON Schema 或 Pydantic 等严格字段校验；
5. 没有交易日、停牌、复权和缺失值的系统性处理；
6. 没有逐条新闻原始 URL 与事实核验状态；
7. 没有上一期预测自动回溯；
8. 未实现服务端持久化、登录、权限或多人编辑；
9. 当前 `python -m http.server` 仅适合本地预览，不适合生产部署；
10. 免费接口无 SLA，接口改版会导致采集失败（采集器按数据项分源，单源失效不影响其他数据项）；金融数据商用展示前需确认许可。

---

## 11. 推荐后续工作顺序

### P0：接入第一份真实 EOD 数据 —— **已完成（2026-08-31）**

- ~~选择唯一行情供应商~~ → 采用多源容错（腾讯/中证/深交所/东财/新浪），见第 0 节；
- ~~写采集或转换脚本~~ → `scripts/collect_data.py`；
- ~~输出与 `sample.json` 相同的 schema~~ → 已实现；
- ~~校验一个完整交易日~~ → 2026-08-28 已交叉验证；
- ~~页面改为明确展示真实来源和截止时间~~ → 已实现（动态横幅 + sources 逐项列出）；
- 遗留：东财解封后重跑 `collect_data.py` 补齐 2026-08-28 的行业资金流并重新 build。

### P1：增加数据质量层

- 增加 JSON Schema；
- 必填字段和单位校验；
- 交易日及重复日期校验；
- 缺失、延迟和异常值标记；
- 构建失败时保留上一份成功快照。

### P2：补齐决策闭环

- 为每条裁决增加 `trigger`、`invalidation`；
- 增加次日验证清单；
- 次日生成时自动回溯上一期判断；
- 记录 `✓ / ✗ / △` 和依据。

### P3：自动发布

- Windows Task Scheduler 或 CI 定时构建；
- 静态托管至 GitHub Pages、Cloudflare Pages 或对象存储；
- 构建成功后输出 HTML/PDF/长图；
- 失败时发送告警。

### P4：增强分析模块

- 连板和轮动兑现率；
- 龙虎榜席位传导；
- 全球资产联动；
- 宏观事件日历；
- 数据血缘和逐条来源链接。

---

## 12. 接手验收清单

接手者应依次确认：

- [x] `python -X utf8 scripts/collect_data.py` 可成功运行并产出 `data/raw/eod-*.json`（资金流被限流时仍成功，flows 为空属预期）；
- [x] `python scripts/build_data.py` 可成功运行；
- [x] `data/market-card.json` 可被解析；
- [x] `python -m http.server 8000` 后页面可打开；
- [x] 页面顶部显示日期、更新时间和真实数据标识（非演示）；
- [x] 核心裁决、市场表、广度、来源均有内容；资金表/观察池在限流降级时为空属预期；
- [ ] 移动端表格可横向滚动（未在本轮重测）；
- [ ] 展开/收起和打印按钮可用（未在本轮重测）；
- [x] 替换/新增 `data/raw/*.json` 后能重新构建（真实快照优先于演示）；
- [x] 页面已明确展示真实来源和截止时间。

---

## 13. 关键文件入口

- 页面入口：`index.html`
- 页面逻辑：`app.js`
- 样式和打印：`styles.css`
- 数据采集：`scripts/collect_data.py`
- 构建规则：`scripts/build_data.py`
- 输入示例：`data/raw/sample.json`
- 真实快照：`data/raw/eod-*.json`
- 新高新低缓存：`data/cache/breadth_history.json`
- 构建产物：`data/market-card.json`
- 详细使用说明：`README.md`

如要继续开发，优先做两件事：(1) 东财解封后补齐资金流并跑通完整 6 个数据项；(2) 为 events/scenarios 建立离线生成流程（人工或 LLM 按 schema 填入快照），不建议先扩大前端组件数量。
