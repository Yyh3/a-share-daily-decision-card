# HANDOFF — 批三交付与下一步

> 更新时间：2026-09-02 18:20（UTC+8）
> 前置文档：`HANDOFF-完全体差距分析.md`（15 项差距清单）→ `HANDOFF-分析层补齐方案.md`（9 项实测）→ 本文档
> 当前状态：**P1/P2/P3 全部完成**，测试 **282 项全绿**，09-02 真实数据全链路已跑通；P4 为积累类无需开发
> 本地路径：`C:\Users\xxw98\cola\coding-cola\project-20260830-233000-dcf14a8f`（仓库 `Yyh3/a-share-daily-decision-card`，私有）

---

## 0. 2026-09-02 傍晚更新：P1-P3 交付记录

### P1 — promotion_floor 历史分位校准 ✅

- 新增纯函数 `analysis.promotion_history_floor(rates)`：≥20 个历史晋级率样本时取**中位数**作为 floor（模式 `history`），否则维持固定 40%（模式 `default`）；None 样本剔除，偶数样本取中间两值均值。
- `build_verify_checks` 新增 `promo_history` 参数：生成侧 statement 切换为「晋级率能否站上近 20 日中枢 X%」，`params` 带 `floor_mode` 标记；评分侧 `evaluate_check_detail` 无需改动（已参数化，自动用新 floor）。
- `build_data.py` 从 `data/raw/eod-*.json` 历史（`limit_ladder.metrics.promotion_rate`）收集序列传入，仅取 market_date 之前的样本。
- **勘误**：原文档说"data/cache 已逐日累积 promotion_rate 序列"不准确——daily_stats.json 只存 zt/dt/zt_codes，晋级率历史实际在 raw 快照里，本次实现直接读 raw。当前仅 2 个历史样本（08-31: 22.0, 09-01: 20.5），floor 显示 default 模式，约 20 个交易日后自动切 history。
- 单测 11 项（偶数中位数、None 剔除、两种模式的 statement/评分接线）。

### P2 — ETF 份额公示源 ✅（探测成功并落地）

- **数据源实测结论**：东财 datacenter-web 报表 **`RPT_FUND_ETFLIST`** 可用（该域名不在 push2 限流内），字段 `DEC_TOTALSHARE`（总份额，份）+ `INDEX_CODE`（跟踪指数）+ `SECURITY_NAME_ABBR`，全市场 1650 只 ETF 约 4 次分页请求（500/页）。
- 新增纯函数 `analysis.build_etf_view(today, prev)`：按 8 个宽基指数（沪深300/中证500/1000/上证50/创业板指/科创50/中证红利/A500）聚合 ETF 总份额日差分（净增=申购、净减=赎回），附单基金变动 top8（|Δ|<500 万份视为披露噪声剔除）；首次运行返回 bootstrap 视图。
- `collect_data.py` 新增 `fetch_etf_shares()` + 缓存 `data/cache/etf_shares.json`（按交易日存全量份额快照，随 dates_all 窗口修剪）+ 主流程 7f 块（软依赖）；`build_data.py` 透传 `etf_shares`；前端 `app.js renderEtf` 渲染在市场广度章节两融下方（`index.html` 新增 `#etf-shares` 容器，`?v=` 升至 **20260902c**）。
- **首次运行已入库**：09-02 采集 1650 只基金份额，bootstrap 状态，自 09-03 起可见差分。
- 单测 12 项（多基金同指数聚合、噪声剔除、bootstrap、summary 文案）。

### P3 — 证据包 brief-*.md 生成 ✅

- 新文件 `scripts/build_brief.py`：读 `market-card.json`（零新增采集）输出 `data/brief-<date>.md`，9 节结构：市场概览（含风格/估值/两融/ETF）→ 规则版主线判定 → 行业资金 top20 → 涨停梯队+情绪指标 → 龙虎榜方向聚合 → 轮动兑现/噪音区 → 背离/全球 → 事件日历 → **两个待产出的 JSON schema 说明**（events 7 字段 + evidence 数字必须可核对；mainline conclusion 必须命中 ≥2 关键事实）。
- 用途：把 brief 喂给任意 LLM（含当前会话），让其产出 `data/events-<date>.json` / `data/mainline-<date>.json`，落盘后重跑 `build_data.py` 即过校验器进卡。
- 09-02 实测生成 3645 字符 / 122 行。

### 自动化任务修复 ⚠️→✅

- **发现**：原文档声称"自动化任务（19:00 每日）已按此链路配置"——实测 QwenWork 定时任务与 Windows 计划任务中**均不存在**（可能上个会话配置后丢失）。
- **已补建**：QwenWork cron「A股复盘决策卡-每日采集构建」（工作日 19:00，链路含 build_brief，非交易日自动跳过，软依赖失败不算失败，硬依赖失败才上报）。
- 渲染验证方式（沙箱内无头浏览器不可用，见第 4 节）：单测 + build 输出契约抽查——etf block bootstrap、promo floor_mode=default、standalone 61,038 bytes。

---

## 1. 交接快照（读这一段就能接手）

### 1.1 运行链路（四条命令，顺序固定）

```
python -X utf8 scripts/collect_data.py     # 联网采集，落 data/raw/eod-<date>.json + data/cache/*
python -X utf8 scripts/build_data.py       # 纯本地聚合 → data/market-card.json（副作用：写 data/verify_log.json）
python -X utf8 scripts/build_standalone.py # 数据内联 → market-card-view.html（双击可看，需与 app.js/styles.css 同目录）
python -X utf8 scripts/build_brief.py      # LLM 素材包 → data/brief-<date>.md（喂给 LLM 填 events/mainline）
python -X utf8 scripts/test_logic.py       # 282 项单测；改任何纯函数后必跑
```

- 自动化任务（19:00 每日，工作日）已按此四步链路配置于 QwenWork cron「A股复盘决策卡-每日采集构建」，龙虎榜 18:00 后披露，软依赖缺了不算失败；非交易日自动跳过。
- `market-card-view.html` 只内联**数据**，app.js/styles.css 仍相对引用——这是设计，不是遗漏。

### 1.2 架构铁律（改代码前必读）

1. **决定论立身之本**：页面上的每个结论都来自本地 `eod-*.json` + 纯函数规则。渲染层（app.js）不做计算，只搬运。
2. **LLM 是编译器不是运行时**：LLM 只允许在离线环节产出 JSON，且必须过事实校验才能落进卡片：
   - 事件卡：`data/events-<date>.json` → `validate_events`（7 字段齐全 + 每个 anchors 数字在当日快照有出处，**anchors 为空也拒绝**）→ 通过的排前展示，被拒的注明原因
   - 主线改写：`data/mainline-<date>.json`（`{"conclusion": "..."}` 或裸字符串）→ `apply_mainline_rewrite` 必须命中 ≥2 个 `key_tokens`，否则保留规则结论并标注"外部改写未采用"
   - 来源标记：`origin: llm | rule`；页面渲染时外部卡带"外部"徽章
3. **纯函数层**：`scripts/analysis.py` 全部 dict 进 dict 出、无 I/O；`build_data.py` 只做 I/O 和组装。新逻辑进 analysis.py + 配套单测，**跑通测试等同于该模块上线**。
4. **改 app.js / styles.css 必须同步升 `index.html` 里的 `?v=`**（当前 `20260902b`），否则浏览器缓存出 NaN。
5. 临时探测脚本 `_probe_*.py` 跑完即删，commit 里不许有。

### 1.3 三批交付物一览

| 批次 | commit | 内容 |
|---|---|---|
| 一 | `ad5d249` | 轮动兑现 / 主线 5 判据 / 情绪阶段 / 趋势倒推 / 验证回溯 / 龙虎榜方向级聚合 / 噪音区 |
| 二 | `3f33f84` | 盘中节奏时间轴 / 跨市场背离 / 宏观事件日历 / 方向池九宫格 + 数据层 P1（PE 分位/解禁/美债/龙虎榜溢出保护） |
| 批前修复 | （未单独提交，并入批三） | 板块去重 `dedupe_sectors`（Ⅱ/Ⅲ 级重复合并）/ 验证清单只评次日 + reason 标签 + `ctx_fingerprint` 幂等重评 / 自动化 15:30→19:00 |
| 三 | `b84776c` | 事件卡（规则扫描 + 事实校验 + 外部入口）/ 三情景剧本 / 主线 structured + dimensions + 改写校验 |

### 1.4 关键纯函数索引（scripts/analysis.py）

- 资金：`dedupe_sectors`（去重）、`build_rotation_view`（轮动）、`noise_zone`（噪音区）、`direction_pool`（九宫格）
- 主线：`build_mainline_view`（5 判据 + conclusion + structured + dimensions）、`apply_mainline_rewrite`
- 情绪：`emotion_stage`（周期定位）、`trend_forecast`（趋势倒推）
- 事件：`scan_event_candidates`（5 类信号）、`validate_events`
- 情景：`build_scenarios`（阈值 = 当日涨停 × 固定系数 round5；概率先验 25/50/25，60 交易日后切基频）
- 验证：`build_verify_checks` / `evaluate_check_detail` / `score_checks` / `ctx_fingerprint`（清单 5 类断言：`sector_day5_positive` / `rotation_payoff` / `turnover_floor` / `board_continue` / `promotion_floor`）

---

## 2. 已知问题与修复记录（2026-09-02 审计）

审计发现 6 个问题，5 个已修：

1. ✅ flows 表 Ⅱ/Ⅲ 级重复（20 行 ≈ 11 个真实方向）→ `dedupe_sectors`（后缀剥离 + 数值指纹 + 前缀包含，容差 5%）
2. ✅ 验证清单在 D+2..D+5 被错配评分 → 只评紧邻次日；漏跑窗口直接清理而非硬评
3. ✅ 回溯分数来自 18:00 部分采集 → `ctx_fingerprint`（sha1[:12]）变化才重评
4. ✅ 自动化 15:30 永远看不到 18:00 龙虎榜 → 改 19:00 并重写 prompt
5. ✅ 主线 `structured.level` 与结论句不一致（持续=1 时误标"无"）→ level 与结论句同源
6. ✅ **`promotion_floor=40` 硬编码**——已于 09-02 傍晚修复（P1，见第 0 节）：≥20 个历史样本后 floor 自动切换为近 20 日中位数，`floor_mode` 标记来源；样本不足时维持 40 并保留旧文案。

---

## 3. 待办（按优先级）—— P1/P2/P3 已于 2026-09-02 完成，见第 0 节交付记录

### ~~P1 — promotion_floor 历史分位校准~~ ✅
- 已实现 `promotion_history_floor`（中位数，≥20 样本切换）；历史样本实际来源为 raw 快照而非 daily_stats（原文档有误，已在第 0 节勘误）。当前 2 个样本处于 default 模式，约 20 个交易日后自动切 history，无需再动代码。

### ~~P2 — ETF 份额公示源探测~~ ✅
- 探测成功：东财 datacenter-web `RPT_FUND_ETFLIST` 报表（`DEC_TOTALSHARE` 总份额字段），1650 只 ETF 全量入库，`build_etf_view` 按 8 个宽基指数聚合日差分。首次运行（09-02）为 bootstrap，09-03 起出差分数据。参考卡 03 章的"国家队/ETF 动向"数据层到此闭环（龙虎榜席位级解码仍不在范围）。

### ~~P3 — 证据包 brief-*.md 生成~~ ✅
- `scripts/build_brief.py` 已落地（9 节素材 + 双 schema 说明），09-02 实测 3645 字符。**下一步使用方式**：每日 build 后把 `data/brief-<date>.md` 喂给 LLM，让其产出 `data/events-<date>.json`（7 字段全非空、数字可核对）与 `data/mainline-<date>.json`（命中 ≥2 关键事实），重跑 `build_data.py` 过校验器后进卡——这是"LLM 编译器"链路的人工触发版，可按需接成定时任务。

### P4 — 积累类（无需开发，时间自动解决）
- 方向池九宫格的 pct 历史目前仅 1 个样本，全部显示"积累中"，约 20 个交易日后自动丰满。
- 三情景概率 60 个交易日后从先验切历史基频（`SCENARIO_MIN_SAMPLES=60`）。
- 晋级率 floor（P1）：约 20 个交易日后自动从 default 切 history 模式。

### P5 — 新增候选（下个迭代再评估，非本次范围）
- brief→LLM→events 的自动化闭环：19:00 cron 目前只跑到 build_brief；若每日事件卡质量满意，可在 cron 链路后追加"调用 LLM 产 events/mainline → 重跑 build"一步（需要模型会话，成本与失败处理待设计）。
- ETF 份额口径增强：RPT_FUND_ETFLIST 的份额是披露快照（可能滞后一日），与基金公司官网申赎名单的口径差异尚未标注到页面——目前 note 已写"披露可能滞后一日"，够用。

---

## 4. 环境怪坑（新会话必看）

- **agent-browser / Chrome / Edge headless 在本沙箱静默退出**（exit 0 无日志）——渲染验证只能靠单测 + `build_data` 输出契约，别在无头浏览器上浪费时间。
- 代理会拦 `localhost` 浏览器 fetch；临时绕开用 `--no-proxy-server --proxy-bypass-list=*`，不稳定。
- 单测夹具 `pe_percentile` 需要 ≥244 个样本（10 年窗口最低门槛）。
- Git Bash 里 `/c/...` 路径直传 node CLI 会变 `C:\c\...`；用 `npm` 不带路径。
- `python -m http.server` 后台启动要 nohup + 日志重定向。
- **临时脚本写 Python 别用 `bash -c` 内联**（`\n` 会被吃掉），写成 `_probe_*.py` 再跑。
- Windows 下 Edit 工具偶发 "File has been modified since read"——重新 Read 再 Edit 即可。
- 大范围文本替换时警惕"读写同源标识符"（曾把 `for raw in source["flows"]:` 误替换成 `for raw in flows:` 导致空循环）。

---

## 5. 验收基线（2026-09-02 18:20 全部满足）

- [x] `python -X utf8 scripts/test_logic.py` → **ALL TESTS PASSED（282 项，含 P1 11 项 + P2 12 项）**
- [x] `python -X utf8 scripts/build_data.py` → `built data\market-card.json from eod-2026-09-02.json`
- [x] `python -X utf8 scripts/build_standalone.py` → 61,038 bytes，market_date=2026-09-02
- [x] `python -X utf8 scripts/build_brief.py` → `data/brief-2026-09-02.md`（3,645 字符 / 122 行）
- [x] `node --check app.js` 通过；`?v=20260902c`
- [x] QwenWork cron「A股复盘决策卡-每日采集构建」已建（工作日 19:00，四步链路）
- [x] 09-02 真实数据抽查：ETF 块 bootstrap（1650 只已缓存）；promo floor_mode=default（历史 2 样本）；采集请求统计无 push2his
- [x] git 干净后推送（本次 P1-P3 + 09-02 快照一并提交）
