"use strict";

const $ = (selector) => document.querySelector(selector);
const all = (selector) => [...document.querySelectorAll(selector)];
const dash = (value) => value === null || value === undefined || value === "" ? "—" : value;
const money = (value) => Number(value).toLocaleString("zh-CN", {
  minimumFractionDigits: Math.abs(Number(value)) < 100 ? 4 : 2,
  maximumFractionDigits: Math.abs(Number(value)) < 100 ? 4 : 2,
});
const signed = (value, suffix = "") => `${value > 0 ? "+" : ""}${Number(value).toFixed(2)}${suffix}`;
const directionClass = (value) => value > 0 ? "positive" : value < 0 ? "negative" : "";
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
const signedOrDash = (value, suffix = "") => (value === null || value === undefined) ? "—" : signed(value, suffix);
const classOrDash = (value) => (value === null || value === undefined) ? "" : directionClass(value);

function renderIndexPanel(panel, style) {
  const rows = (panel || []).map(row => `<tr><td><strong>${escapeHtml(row.name)}</strong></td><td>${money(row.close)}</td><td class="${classOrDash(row.pct)}">${signedOrDash(row.pct, "%")}</td><td class="${classOrDash(row.ret5)}">${signedOrDash(row.ret5, "%")}</td><td class="${classOrDash(row.ret20)}">${signedOrDash(row.ret20, "%")}</td><td class="${classOrDash(row.ret60)}">${signedOrDash(row.ret60, "%")}</td></tr>`).join("");
  $("#index-table").innerHTML = rows || `<tr><td colspan="6" class="empty-row">本次快照未包含指数面板数据。</td></tr>`;
  $("#style-note").textContent = style
    ? `${style.note} ${style.method}`
    : "风格坐标需要 7 个指数的 20 日区间收益，当前快照数据不足，本栏留空。";
}

function renderLadder(ladder) {
  const metrics = (ladder && ladder.metrics) || {};
  const has = Object.keys(metrics).length > 0;
  $("#ladder-metrics").innerHTML = has ? [["涨停", metrics.limit_up], ["跌停", metrics.limit_down],
    ["炸板", metrics.zha_ban], ["封板率", metrics.seal_rate === null ? "—" : `${metrics.seal_rate}%`],
    ["晋级率", metrics.promotion_rate === null ? "—" : `${metrics.promotion_rate}%`],
    ["最高连板", metrics.max_board ? `${metrics.max_board} 板` : "—"]]
    .map(([key, value]) => `<div class="metric"><strong>${dash(value)}</strong><small>${escapeHtml(key)}</small></div>`).join("") : "";

  const dist = (ladder && ladder.distribution) || {};
  $("#ladder-dist").innerHTML = has ? Object.entries(dist)
    .map(([board, count]) => `<span class="board-chip${Number(board) >= 3 ? " is-high" : ""}">${escapeHtml(board)} 板 · ${count} 家</span>`).join("") : "";

  const ladderRows = (ladder && ladder.ladder) || [];
  $("#ladder-table").innerHTML = ladderRows.length ? ladderRows.map(row => `<tr><td><strong>${row.board} 板</strong></td><td>${escapeHtml(row.stock)}<br><small>${escapeHtml(row.code)}</small></td><td>${escapeHtml(row.sector)}</td><td>${row.seal_ratio === null ? "—" : `${row.seal_ratio.toFixed(2)}%`}</td><td>${row.amount.toFixed(2)} 亿</td><td>${row.turnover_rate.toFixed(2)}%</td><td>${row.zbc > 0 ? `${row.zbc} 次` : "—"}</td><td>${escapeHtml(row.note)}</td></tr>`).join("") : `<tr><td colspan="8" class="empty-row">本次快照未包含涨停梯队数据。</td></tr>`;

  const notes = (ladder && ladder.notes) || [];
  const head = metrics.promoted !== null && metrics.promoted !== undefined && metrics.prev_limit_up
    ? `晋级口径：昨日涨停 ${metrics.prev_limit_up} 家中 ${metrics.promoted} 家今日继续涨停。` : "";
  $("#ladder-notes").innerHTML = [head, ...notes].filter(Boolean).map(item => `<span>${escapeHtml(item)}</span>`).join("");
}

function renderGlobal(rows, asOf, usSession) {
  $("#global-table").innerHTML = rows.length ? rows.map(row => `<tr><td>${escapeHtml(row.category)}</td><td><strong>${escapeHtml(row.name)}</strong></td><td>${row.close === null ? "—" : money(row.close)}</td><td class="${classOrDash(row.pct)}">${signedOrDash(row.pct, "%")}</td></tr>`).join("") : `<tr><td colspan="4" class="empty-row">全球行情为软依赖，本次未采集到数据，不影响其余章节。</td></tr>`;
  const lagged = rows.filter(row => row.lagged);
  let note = rows.length
    ? `行情截至 ${dash(asOf) || "采集时刻"}；美股与欧股取北京时间次日凌晨收盘，亚太与港股取当日收盘。美债收益率无免费稳定数据源，未列示。`
    : "";
  if (usSession === "intraday" && rows.length) {
    note += " ⚠ 采集时美股尚未收盘，美股与相关商品为最新盘中价，非收盘价。";
  }
  if (lagged.length) {
    note += ` 以下标的数据日期早于卡片市场日：${lagged.map(r => `${r.name}（${r.as_of}）`).join("、")}。`;
  }
  $("#global-notes").textContent = note;
}

function renderMargin(margin) {
  if (!margin) {
    $("#margin").innerHTML = `<h4>两融余额</h4><p class="unit-note">本次快照未采集到两融数据（软依赖，T+1 披露）。</p>`;
    return;
  }
  const cells = [["融资融券余额", `${money(margin.balance)} 亿`],
                 ["较前一交易日", margin.change === null ? "—" : `${signed(margin.change)} 亿`],
                 ["占流通市值", margin.pct_of_float === null ? "—" : `${margin.pct_of_float}%`]]
    .map(([key, value]) => `<div class="metric"><strong>${value}</strong><small>${escapeHtml(key)}</small></div>`).join("");
  $("#margin").innerHTML = `<h4>两融余额</h4><div class="metric-grid">${cells}</div>`
    + `<p class="unit-note">数据日期 ${escapeHtml(margin.as_of)}（T+1 披露，落后卡片市场日）；`
    + `融资余额 ${money(margin.financing)} 亿、融券余额 ${money(margin.securities_loan)} 亿，`
    + `当日融资净买入 ${margin.financing_net_buy === null ? "—" : `${signed(margin.financing_net_buy)} 亿`}。${escapeHtml(margin.note)}</p>`;
}

function render(data) {
  const { meta, status, verdicts, market_days: days, breadth, flows, accumulation_pool: pool, events, scenarios, risk_notes: risks,
          index_panel: panel, style, limit_ladder: ladder, margin, global_markets: globals, global_as_of: globalAsOf } = data;
  document.title = `A 股每日复盘决策卡 · ${meta.market_date}`;
  $("#data-badge").textContent = `更新 ${meta.updated_at}`;
  $("#demo-warning").innerHTML = meta.demo
    ? "<strong>演示数据，不构成投资建议</strong><span>本页不会联网，所有结论由本地 JSON 和确定性规则生成。</span>"
    : `<strong>真实行情快照，不构成投资建议</strong><span>行情与资金数据来自公开接口（腾讯/中证指数官网/深交所官网/东方财富/新浪，逐项见来源说明），由本地脚本离线采集固化，页面本身不联网、非实时。</span>`;
  $("#hero-summary").textContent = `${meta.market_date} · ${status.market_tone} · 情绪 ${status.emotion.label}`;
  $("#status-panel").innerHTML = [
    ["市场情绪", status.emotion.label], ["情绪评分", `${status.emotion.score} / 5`],
    ["上涨占比", `${status.emotion.up_ratio}%`], ["成交额", `${status.turnover.toFixed(2)} 万亿`]
  ].map(([label, value]) => `<div class="status-cell"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></div>`).join("");

  $("#verdicts").innerHTML = verdicts.map((item, index) => `<article class="verdict"><span class="tag">${escapeHtml(item.tag)}</span><h3>${index + 1}. ${escapeHtml(item.title)}</h3><p>${escapeHtml(item.evidence)}</p><p class="action"><strong>执行：</strong>${escapeHtml(item.action)}</p></article>`).join("");
  $("#market-table").innerHTML = days.map(day => `<tr><td><strong>${escapeHtml(day.date)}</strong></td><td class="${directionClass(day.shanghai)}">${signed(day.shanghai, "%")}</td><td class="${directionClass(day.chinext)}">${signed(day.chinext, "%")}</td><td class="${directionClass(day.star50)}">${signed(day.star50, "%")}</td><td>${day.turnover.toFixed(2)} 万亿</td><td>${day.limit_up} / ${day.limit_down}</td><td>${escapeHtml(day.feature)}</td></tr>`).join("");
  $("#flow-table").innerHTML = flows.map(row => `<tr><td><strong>${escapeHtml(row.sector)}</strong></td><td class="${directionClass(row.today)}">${signed(row.today)}</td><td class="${directionClass(row.day5)}">${signed(row.day5)}</td><td class="${directionClass(row.day10)}">${signed(row.day10)}</td><td><span class="flow-label">${escapeHtml(row.classification)}</span></td></tr>`).join("");

  renderIndexPanel(panel, style);
  renderLadder(ladder);
  renderGlobal(globals || [], globalAsOf, data.global_us_session);
  renderMargin(margin);

  const total = breadth.up + breadth.down + breadth.flat;
  $("#breadth").innerHTML = `<div class="breadth-bar" title="上涨 / 平盘 / 下跌"><span class="rise" style="width:${breadth.up / total * 100}%"></span><span class="flat" style="width:${breadth.flat / total * 100}%"></span></div><div class="metric-grid">${[["上涨",breadth.up],["下跌",breadth.down],["涨停",breadth.limit_up],["跌停",breadth.limit_down],["5日新高",breadth.new_high],["5日新低",breadth.new_low]].map(([key,value]) => `<div class="metric"><strong>${value ?? "—"}</strong><small>${key}</small></div>`).join("")}</div>`;
  $("#pool").innerHTML = pool.length ? pool.map((item, index) => `<article class="pool-item"><div class="pool-head"><strong><span class="rank">0${index + 1}</span> ${escapeHtml(item.sector)}</strong><span>${item.score.toFixed(1)} 分</span></div><p>${escapeHtml(item.reason)}</p></article>`).join("") : "<p>当前规则下无候选板块。</p>";
  $("#events").innerHTML = events.length ? events.map(item => `<article class="event-card"><div class="event-head"><h3>${escapeHtml(item.title)}</h3><span class="level">${escapeHtml(item.level)} · ${escapeHtml(item.direction)}</span></div><p>${escapeHtml(item.summary)}</p><dl><dt>传导</dt><dd>${escapeHtml(item.transmission)}</dd><dt>证据</dt><dd>${escapeHtml(item.evidence)}</dd><dt>风险</dt><dd>${escapeHtml(item.risk)}</dd></dl></article>`).join("") : "<p class=\"unit-note\">当前数据源未接入资讯事件，本栏留空。热点与产业传导需人工或离线分析层补充。</p>";
  $("#scenarios").innerHTML = scenarios.length ? scenarios.map(item => `<article class="scenario"><div class="scenario-head"><strong>${escapeHtml(item.name)}</strong><span class="probability">${item.probability}%</span></div><div class="prob-bar"><span style="width:${item.probability}%"></span></div><p><strong>触发：</strong>${escapeHtml(item.trigger)}</p><p><strong>动作：</strong>${escapeHtml(item.action)}</p></article>`).join("") : "<p class=\"unit-note\">次日情景预案属于离线分析层，当前数据源未提供，本栏留空。</p>";
  const sourceItems = meta.sources.map(source => `<li><strong>${escapeHtml(source.name)}</strong> · ${escapeHtml(source.as_of)}<br>${escapeHtml(source.note)}</li>`).join("");
  $("#sources").innerHTML = `<div><h3>数据来源</h3><ul>${sourceItems}<li>输入文件：${escapeHtml(meta.input_file)}</li><li>页面更新时间：${escapeHtml(meta.updated_at)}</li></ul></div><div><h3>风险与口径</h3><ul>${risks.map(item => `<li>${escapeHtml(item)}</li>`).join("")}<li>情绪算法：${escapeHtml(status.emotion.method)}</li></ul></div>`;
}

function setCollapsed(section, collapsed) {
  section.classList.toggle("is-collapsed", collapsed);
  section.querySelector(".section-toggle").setAttribute("aria-expanded", String(!collapsed));
}

all("[data-section] .section-toggle").forEach(button => button.addEventListener("click", () => {
  const section = button.closest("[data-section]");
  setCollapsed(section, !section.classList.contains("is-collapsed"));
}));
$("#toggle-all").addEventListener("click", () => {
  const sections = all("[data-section]");
  const shouldCollapse = sections.some(section => !section.classList.contains("is-collapsed"));
  sections.forEach(section => setCollapsed(section, shouldCollapse));
  $("#toggle-all").textContent = shouldCollapse ? "⌄" : "⌃";
});
$("#print").addEventListener("click", () => window.print());

fetch("data/market-card.json", { cache: "no-store" })
  .then(response => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
  .then(render)
  .catch(error => {
    $("#error").hidden = false;
    $("#error").innerHTML = `<strong>数据加载失败：</strong>${escapeHtml(error.message)}。请通过本地 HTTP 服务器打开页面，不要直接双击 index.html。`;
    $("#data-badge").textContent = "数据不可用";
  });
