"use strict";

const $ = (selector) => document.querySelector(selector);
const all = (selector) => [...document.querySelectorAll(selector)];
const signed = (value, suffix = "") => `${value > 0 ? "+" : ""}${Number(value).toFixed(2)}${suffix}`;
const directionClass = (value) => value > 0 ? "positive" : value < 0 ? "negative" : "";
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);

function render(data) {
  const { meta, status, verdicts, market_days: days, breadth, flows, accumulation_pool: pool, events, scenarios, risk_notes: risks } = data;
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
  $("#flow-table").innerHTML = flows.map(row => `<tr><td><strong>${escapeHtml(row.sector)}</strong></td><td class="${directionClass(row.today)}">${signed(row.today)}</td><td class="${directionClass(row.day5)}">${signed(row.day5)}</td><td class="${directionClass(row.day20)}">${signed(row.day20)}</td><td><span class="flow-label">${escapeHtml(row.classification)}</span></td></tr>`).join("");

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
