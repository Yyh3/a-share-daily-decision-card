# HANDOFF — 批三交付与下一步

> 创建时间：2026-09-02 17:30（UTC+8）
> 前置文档：`HANDOFF-完全体差距分析.md`（15 项差距清单）→ `HANDOFF-分析层补齐方案.md`（9 项实测）→ 本文档
> 当前状态：**三批分析层全部落地**，最新 commit `b84776c`，测试 261 项全绿，已推送 GitHub main
> 本地路径：`C:\Users\xxw98\cola\coding-cola\project-20260830-233000-dcf14a8f`（仓库 `Yyh3/a-share-daily-decision-card`，私有）

---

## 1. 交接快照（读这一段就能接手）

### 1.1 运行链路（三条命令，顺序固定）

```
python -X utf8 scripts/collect_data.py     # 联网采集，落 data/raw/eod-<date>.json + data/cache/*
python -X utf8 scripts/build_data.py       # 纯本地聚合 → data/market-card.json（副作用：写 data/verify_log.json）
python -X utf8 scripts/build_standalone.py # 数据内联 → market-card-view.html（双击可看，需与 app.js/styles.css 同目录）
python -X utf8 scripts/test_logic.py       # 261 项单测；改任何纯函数后必跑
```

- 自动化任务（19:00 每日）已按此链路配置，龙虎榜 18:00 后披露，软依赖缺了不算失败。
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
6. ⚠️ **`promotion_floor=40` 硬编码**（`analysis.py:380`）——实测 09-01 晋级率仅 20.5%，连续两日按 ✗ 误判。**未修**，见下节。

---

## 3. 待办（按优先级）

### P1 — promotion_floor 历史分位校准
- 现状：验证清单里"晋级率能否 ≥40%"的 40 是拍脑袋值，而近期市场晋级率中枢明显下移（09-01 仅 20.5%），该断言天天 ✗，失去信息量。
- 方案：`data/cache/` 已逐日累积 promotion_rate 序列（daily_stats / 涨停缓存）。等积累 ≥20 个交易日后，把 floor 改为「近 20 日分位中位数」，断言语义从绝对阈值变为相对阈值，`statement` 同步改为"晋级率能否站上近 20 日中枢 X%"。
- 注意：改的是 `build_verify_checks`（生成侧）与 `evaluate_check_detail`（评分侧）两处，别只改一半。

### P2 — ETF 份额公示源探测（参考卡 03 章"国家队/ETF 动向"）
- 现状：`HANDOFF-分析层补齐方案.md` 判为"半可做"——push2delay 的 ETF clist 只有价格，份额字段需另找报表名（东财 datacenter 或基金公司官网公示）。
- 探测建议：东财 datacenter 试 `RPT_FUND_ETF...` 系报表名；或深交所/上交所基金周报页面。找到后沿用 datacenter 域名（不在 push2 限流内）。

### P3 — 证据包 brief-*.md 生成
- 目的：给 LLM 离线填 `events-<date>.json` / `mainline-<date>.json` 时喂的素材包。把当日快照的关键数字（flows top20、梯队、龙虎榜摘要、背离、日历）导出为一份 markdown。
- 落点：`scripts/build_brief.py`（新文件），输出 `data/brief-<date>.md`。内容全部来自 market-card.json，零新增采集。
- LLM 侧约定：产出 JSON → 走 `validate_events` / `apply_mainline_rewrite` 校验，校验器会兜底，所以 brief 里写清楚 schema 即可。

### P4 — 积累类（无需开发，时间自动解决）
- 方向池九宫格的 pct 历史目前仅 1 个样本，全部显示"积累中"，约 20 个交易日后自动丰满。
- 三情景概率 60 个交易日后从先验切历史基频（`SCENARIO_MIN_SAMPLES=60`）。

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

## 5. 验收基线（交接时全部满足）

- [x] `python -X utf8 scripts/test_logic.py` → **ALL TESTS PASSED（261 项）**
- [x] `python -X utf8 scripts/build_data.py` → `built data\market-card.json from eod-2026-09-01.json`
- [x] `python -X utf8 scripts/build_standalone.py` → 60,858 bytes，market_date=2026-09-01
- [x] `node --check app.js` 通过；`?v=20260902b`
- [x] `git status` 干净，main 已推至 `b84776c`
- [x] 09-01 真实数据抽查：主线"存在情绪主线（数字媒体），无产业级主线"（资金=数字媒体 vs 涨停=影视院线，两维度不同向）；事件卡 6 张全部有数值出处；三情景阈值可核对
