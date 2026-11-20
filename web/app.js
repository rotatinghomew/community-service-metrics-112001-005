"use strict";

const state = { protocol: null, report: null, sources: null };

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}
function pct(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}
function signedPct(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  const v = value * 100;
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)} 个百分点`;
}
function list(items) {
  return items && items.length ? items.map((x) => `<span class="pill">${esc(x)}</span>`).join(" ") : '<span class="muted">无</span>';
}
function monthLabel(m) { return m || "—"; }

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = { error: "返回内容无法解析" }; }
  if (!response.ok) {
    const detail = payload && payload.detail ? `：${JSON.stringify(payload.detail)}` : "";
    throw new Error((payload && payload.error ? payload.error : response.statusText) + detail);
  }
  return payload;
}

function banner(message, kind = "ok") {
  const el = $("#banner");
  if (!message) { el.hidden = true; return; }
  el.hidden = false;
  el.className = `banner ${kind}`;
  el.textContent = message;
}

// ---------------- tabs ----------------
$$("#tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$("#tabs button").forEach((b) => b.classList.toggle("active", b === btn));
    $$(".view").forEach((view) => { view.hidden = view.id !== `view-${btn.dataset.view}`; });
  });
});

// ---------------- protocol form ----------------
const form = $("#protocol-form");
const FIELDS = [
  "title", "estimand", "outcome_measure", "outcome_definition", "comparison_start",
  "comparison_end", "data_through", "start_month_policy", "partial_exposure_policy",
  "paused_policy", "control_source", "weighting", "shock_policy", "confidence_level",
  "minimum_matured_per_arm", "minimum_control_months", "minimum_treated_grids",
  "max_ci_width", "mature_completion_floor", "notes",
];

function fillForm(spec) {
  FIELDS.forEach((name) => {
    const input = form.elements.namedItem(name);
    if (!input) return;
    if (input.type === "checkbox") input.checked = Boolean(spec[name]);
    else input.value = spec[name] ?? "";
  });
  if (form.elements.namedItem("require_common_months")) {
    form.elements.namedItem("require_common_months").checked = Boolean(spec.require_common_months);
  }
}

function readForm() {
  const spec = {};
  FIELDS.forEach((name) => {
    const input = form.elements.namedItem(name);
    if (!input) return;
    if (input.type === "number") spec[name] = input.value === "" ? 0 : Number(input.value);
    else if (input.type === "checkbox") spec[name] = input.checked;
    else spec[name] = input.value;
  });
  spec.require_common_months = Boolean(form.elements.namedItem("require_common_months").checked);
  spec.control_policy = "not_yet_started";
  spec.maturation_policy = "matured_units_only";
  return spec;
}

$("#load-draft").addEventListener("click", async () => {
  try {
    const data = await api("/api/draft");
    const current = state.protocol ? state.protocol.spec : data.draft;
    const merged = { ...data.draft, ...(state.protocol ? pickComparison(current) : {}) };
    fillForm(merged);
    banner("已载入草案：请先确认口径与比较时段，再冻结。", "ok");
  } catch (err) { banner(err.message, "error"); }
});

function pickComparison(spec) {
  const out = {};
  ["comparison_start", "comparison_end", "data_through", "title", "confidence_level",
   "minimum_matured_per_arm", "minimum_control_months", "minimum_treated_grids",
   "max_ci_width", "mature_completion_floor", "notes"].forEach((k) => { out[k] = spec[k]; });
  return out;
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const spec = readForm();
  const reason = form.elements.namedItem("reason").value;
  try {
    const data = await api("/api/protocol/freeze", {
      method: "POST",
      body: JSON.stringify({ protocol: spec, reason }),
    });
    banner(`已冻结方案版本 v${data.protocol.version}。比较对象与口径已固定；之后修订会记录理由与影响。`, "ok");
    form.elements.namedItem("reason").value = "";
    await refreshState();
  } catch (err) { banner(err.message, "error"); }
});

function renderProtocolPanel(data) {
  const versions = $("#protocol-versions");
  if (!data.protocols.length) {
    versions.innerHTML = '<div class="card"><span class="muted">还没有冻结方案。分析前必须先冻结：口径和比较时段不能等看到结果再定。</span></div>';
    return;
  }
  versions.innerHTML = data.protocols.slice().reverse().map((p) => `
    <div class="card">
      <div class="section-title">
        <h3>v${p.version} ${p.active ? '<span class="pill good">生效中</span>' : '<span class="pill">已被 v' + (data.protocols.find(x => x.version > p.version && x.active)?.version ?? "?") + ' 取代</span>'}</h3>
        <span class="muted">${esc(p.created_at || "")}</span>
      </div>
      <p class="muted">理由：${esc(p.reason || "—")}</p>
      ${p.changes_from_previous && p.changes_from_previous.length ? `
        <div class="kv">
          ${p.changes_from_previous.map((c) => `<dt>${esc(c.label)}</dt><dd>${esc(fmt(c.old))} → ${esc(fmt(c.new))}</dd>`).join("")}
        </div>` : '<span class="muted">与上一版相比没有字段变化</span>'}
    </div>`).join("");
}

function fmt(value) {
  if (value === null || value === undefined || value === "") return "（空）";
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value);
}

async function refreshState() {
  const data = await api("/api/state");
  state.protocol = data.active_protocol;
  if (data.active_protocol) fillForm(data.active_protocol.spec);
  renderProtocolPanel(data);
  try {
    const diff = await api("/api/diff");
    $("#protocol-diff").innerHTML = diff.diff && diff.diff.length
      ? `<div class="card"><p class="muted">${esc(diff.reason || "")}</p><div class="kv">${diff.diff.map((c) => `<dt>${esc(c.label)}</dt><dd>${esc(fmt(c.old))} → ${esc(fmt(c.new))}</dd>`).join("")}</div></div>`
      : '<span class="muted">只有一版方案，或没有字段变化。</span>';
  } catch (_) { /* ignore */ }
  renderDataPanel(data).catch(() => {});
}

// ---------------- data panel ----------------
async function renderDataPanel() {
  const data = await api("/api/sources");
  state.sources = data;
  const issues = data.issues || [];
  const blocked = issues.filter((i) => i.severity === "warning");
  $("#data-issues").innerHTML = issues.length
    ? issues.map((i) => `<div class="card"><span class="pill ${i.severity === "error" ? "bad" : "warn"}">${i.severity === "error" ? "阻断" : "口径待核"}</span> ${esc(i.message)}</div>`).join("")
    : '<div class="card"><span class="pill good">无</span> 资料结构与计数口径检查通过。</div>';
  if (blocked.length) {
    $("#data-issues").innerHTML += '<p class="hint">标为“口径待核”的网格月份不进入估计，也不会被当作失败；该网格仍保留在覆盖表中。</p>';
  }
  $("#empty-rule").textContent = data.empty_value_rule;
  const tables = [["grids", "网格"], ["rollouts", "实施计划"], ["outcomes", "结果"], ["shocks", "同期冲击"]];
  $("#raw-tables").innerHTML = tables.map(([name, label]) => `
    <h3>${label}（${(data.tables[name] || []).length} 条）</h3>
    <pre class="raw">${esc(JSON.stringify(data.tables[name] || [], null, 1))}</pre>`).join("");
}

$("#outcome-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const raw = Object.fromEntries(new FormData(ev.target).entries());
  ["received", "matured", "timely"].forEach((k) => { raw[k] = Number(raw[k]); });
  await postRow("/api/outcomes", raw, ev.target);
});
$("#shock-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const raw = Object.fromEntries(new FormData(ev.target).entries());
  await postRow("/api/shocks", raw, ev.target);
});
async function postRow(url, body, formEl) {
  try {
    await api(url, { method: "POST", body: JSON.stringify(body) });
    banner("已写入资料表。请回到第 3 步重算；旧报告会标出受影响部分。", "ok");
    formEl.reset();
    await refreshState();
  } catch (err) { banner(err.message, "error"); }
}

// ---------------- analysis ----------------
$("#run-analysis").addEventListener("click", runAnalysis);

async function runAnalysis() {
  if (!state.protocol) {
    banner("还没有冻结方案：先在第 1 步固定结果口径和比较时段。", "warn");
    return;
  }
  try {
    $("#design-status").textContent = "计算中…";
    const data = await api("/api/analyze", { method: "POST", body: JSON.stringify({ save: true }) });
    state.report = data.report;
    $("#design-status").textContent = `报告 ${data.report.id} · 方案 v${data.report.protocol_version}`;
    banner("已按冻结方案重算，并保存为新报告。" + (data.report.previous ? "上一份报告的影响范围见结论页。" : ""), "ok");
    renderDesign(data.report);
    renderResult(data.report);
    await refreshState();
  } catch (err) {
    $("#design-status").textContent = "";
    banner(err.message, "error");
  }
}

// ---------------- design rendering ----------------
function renderDesign(report) {
  const d = report.design;
  const statusPill = (status, label) => {
    const cls = status === "treated" || status === "partial" ? "good"
      : status === "transition" || status === "paused" ? "warn"
      : status === "post_exit" ? "bad" : "";
    return `<span class="pill ${cls}">${esc(label || status)}</span>`;
  };

  const objects = d.control_objects.map((o) => `
    <tr class="${o.value ? "" : "month-row excluded"}">
      <td>${esc(o.month)}</td>
      <td>${list(o.treated)}</td>
      <td>${list(o.controls)}</td>
      <td>${o.soon_controls.length ? list(o.soon_controls) : '<span class="muted">—</span>'}</td>
      <td>${o.notes.map((n) => `<div class="muted">${esc(n)}</div>`).join("") || '<span class="pill good">可比</span>'}</td>
    </tr>`).join("");

  const coverage = d.grid_coverage.map((g) => `
    <tr>
      <td>${esc(g.grid)}</td>
      <td>${esc(g.area_type)}</td>
      <td class="num">${g.observed_months}</td>
      <td class="num">${g.matured}</td>
      <td class="num">${g.control_months}</td>
      <td class="num">${g.treated_months}</td>
      <td>${g.used_as_control ? '<span class="pill">对照</span>' : ""} ${g.used_as_treated ? '<span class="pill good">实施</span>' : ""}</td>
      <td class="muted">${g.excluded_months.map((m) => `${m.month}（${m.label}）`).join("；") || "—"}</td>
    </tr>`).join("");

  const changes = d.control_changes.length ? `
    <div class="card">
      <h3>对照池何时变化</h3>
      <ul class="plain">${d.control_changes.map((c) => `
        <li><b>${esc(c.month)}</b>：${c.added.length ? "加入 " + c.added.join("、") : ""}
        ${c.removed.length ? "移出 " + c.removed.join("、") : ""}</li>`).join("")}</ul>
    </div>` : "";

  const shocks = d.shocks.length ? `
    <div class="card">
      <h3>比较时段内的同期因素</h3>
      <ul class="plain">${d.shocks.map((s) => `
        <li>${esc(s.month)} · ${esc(s.grid)} · ${esc(s.type)}（${esc(s.intensity)}）：
        与措施在同一网格月份重叠，<b>无法区分</b></li>`).join("")}</ul>
      <p class="hint">${esc(report.shock_analysis.statement)}</p>
    </div>` : `<div class="card"><h3>同期因素</h3><p class="muted">${esc(report.shock_analysis.statement)}</p></div>`;

  const rows = d.rows.filter((r) => r.observed).map((r) => `
    <tr>
      <td>${esc(r.grid)}</td>
      <td>${esc(r.month)}</td>
      <td>${statusPill(r.status, r.status_label)}</td>
      <td class="num">${r.exposed_days}</td>
      <td class="num">${r.paused_days}</td>
      <td>${r.control_eligible ? '<span class="pill good">可作对照</span>' : '<span class="muted">不可</span>'}</td>
      <td class="num">${r.received ?? "—"}</td>
      <td class="num">${r.matured ?? "—"}</td>
      <td class="muted">${r.eligibility_reasons.join("；")}</td>
    </tr>`).join("");

  $("#design-body").innerHTML = `
    <div class="card">
      <h3>这一期的对照是什么</h3>
      <p>${esc(d.control_rule)}</p>
      <p class="hint">比较时段 ${esc(d.comparison_months[0] || "—")} 至 ${esc(d.comparison_months[d.comparison_months.length - 1] || "—")}；
      有实施观测的月份 ${d.months_with_treated.length} 个，有合格对照的月份 ${d.months_with_control.length} 个，
      两者同月可比 ${d.common_months.length} 个${d.no_control_months.length ? "；无对照月份：" + d.no_control_months.join("、") : ""}。</p>
      <div class="scroll"><table>
        <thead><tr><th>月份</th><th>实施网格</th><th>合格对照</th><th>临近启动的对照</th><th>说明</th></tr></thead>
        <tbody>${objects}</tbody>
      </table></div>
    </div>
    ${changes}
    <div class="card">
      <h3>各网格覆盖</h3>
      <div class="scroll"><table>
        <thead><tr><th>网格</th><th>类型</th><th class="num">有观测月</th><th class="num">成熟事项</th><th class="num">对照月</th><th class="num">实施月</th><th>用途</th><th>被排除的月份</th></tr></thead>
        <tbody>${coverage}</tbody>
      </table></div>
      <p class="hint">被排除的月份仍列在此处，不会从资料或分析中消失。</p>
    </div>
    <div class="card">
      <h3>逐网格月份判定</h3>
      <div class="scroll"><table>
        <thead><tr><th>网格</th><th>月份</th><th>状态</th><th class="num">实施天数</th><th class="num">暂停天数</th><th>对照资格</th><th class="num">受理</th><th class="num">已成熟</th><th>判定理由</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <p class="hint">启动当月按方案为过渡月；不足月和暂停月不自动视为整月实施。已实施网格从当月起不再进入对照。</p>
    </div>
    ${shocks}
    <div class="card">
      <h3>成熟度与观察延迟</h3>
      <dl class="kv">
        <dt>受理</dt><dd>${report.maturity.received} 件</dd>
        <dt>已成熟</dt><dd>${report.maturity.matured} 件（${pct(report.maturity.mature_share)}）</dd>
        <dt>期末仍有未成熟</dt><dd>${report.maturity.open_items_at_end} 件</dd>
        <dt>规则</dt><dd>${esc(report.maturity.rule)}</dd>
      </dl>
      ${report.maturity.end_of_window_incomplete ? '<p class="pill warn">期末成熟比例低于预设下限，结论需等待</p>' : ""}
    </div>
    <div class="card">
      <h3>无法区分的因素</h3>
      <ul class="plain">
        <li>${esc(d.season_note)}</li>
        <li>${esc(report.shock_analysis.statement)}</li>
        <li>网格不是随机选择试点：启动较早的网格可能本就与其他网格不同，估计只能在可比性成立时解读。</li>
      </ul>
    </div>`;
}

// ---------------- result rendering ----------------
function ciBar(est) {
  if (est.value === null) return "";
  const values = [0, est.value];
  if (est.ci_low !== null) values.push(est.ci_low, est.ci_high);
  let lo = Math.min(...values), hi = Math.max(...values);
  const pad = Math.max(0.02, (hi - lo) * 0.15);
  lo -= pad; hi += pad;
  const pos = (v) => ((v - lo) / (hi - lo)) * 100;
  const span = est.ci_low === null ? "" : `<div class="span" style="left:${pos(est.ci_low)}%;width:${Math.max(0.6, pos(est.ci_high) - pos(est.ci_low))}%"></div>`;
  return `
    <div class="ci-bar">
      <div class="zero" style="left:${pos(0)}%"></div>
      ${span}
      <div class="point" style="left:${pos(est.value)}%"></div>
    </div>
    <div class="axis"><span>${signedPct(lo)}</span><span>0</span><span>${signedPct(hi)}</span></div>`;
}

function renderResult(report) {
  const est = report.estimate;
  const conclusion = report.conclusion;
  const can = conclusion.can_conclude;
  const statusClass = can ? "good" : ["no_control", "no_common_month", "unidentified"].includes(est.status) ? "bad" : "warn";

  const monthly = (est.month_estimates || []).map((m) => {
    if (!m.estimated) {
      return `<tr class="month-row excluded"><td>${esc(m.month)}</td><td colspan="5" class="muted">${esc(m.reason)}</td></tr>`;
    }
    return `<tr>
      <td>${esc(m.month)}</td>
      <td class="num">${pct(m.treated.rate)} <span class="muted">(${m.treated.timely}/${m.treated.matured})</span></td>
      <td class="num">${pct(m.control.rate)} <span class="muted">(${m.control.timely}/${m.control.matured})</span></td>
      <td class="num">${signedPct(m.diff)}</td>
      <td>${list(m.treated.grids)}</td>
      <td>${list(m.control.grids)}</td>
    </tr>`;
  }).join("");

  const sensitivity = (report.sensitivity || []).map((s) => {
    const e = s.estimate;
    const ci = e.ci_low === null ? "区间不可估计" : `${signedPct(e.ci_low)} ~ ${signedPct(e.ci_high)}`;
    const cls = e.status === "estimated" ? "" : "warn";
    return `<div class="card">
      <div class="section-title"><b>${esc(s.name)}</b><span class="pill ${cls}">${esc(e.status_label || e.status)}</span></div>
      <p>${e.value === null ? "本设定下无法估计" : `差异 ${signedPct(e.value)}；区间 ${ci}`}</p>
      <p class="muted">${esc(s.depends_on_primary_structure)}</p>
    </div>`;
  }).join("");

  const adj = report.adjustment || {};
  const adjCard = (adj.adjusted || adj.unadjusted) ? `
    <div class="card">
      <h3>结构、季节与规模的调整估计</h3>
      <p class="hint">在月份内部比较（抵消共同季节与共同冲击），并按城乡类型、网格规模调整构成差异。该区间按观测独立计算，未按网格聚类，比主估计的聚类区间窄；两者不一致时以主估计的区间为准。</p>
      <dl class="kv">
        <dt>未调整</dt><dd>${adj.unadjusted && adj.unadjusted.ok ? `${signedPct(adj.unadjusted.coef)}，区间 ${signedPct(adj.unadjusted.ci_low)} ~ ${signedPct(adj.unadjusted.ci_high)}` : esc(adj.unadjusted ? adj.unadjusted.reason : "无法估计")}</dd>
        <dt>调整后</dt><dd>${adj.adjusted && adj.adjusted.ok ? `${signedPct(adj.adjusted.coef)}，区间 ${signedPct(adj.adjusted.ci_low)} ~ ${signedPct(adj.adjusted.ci_high)}` : esc(adj.adjusted ? adj.adjusted.reason : "无法估计")}</dd>
        <dt>说明</dt><dd>${esc((adj.adjusted || {}).ci_method || "")}</dd>
      </dl>
    </div>` : "";

  const scope = report.scope;
  const previous = report.previous ? `
    <div class="card">
      <h3>与上一份报告相比</h3>
      <dl class="kv">
        <dt>上一份</dt><dd>${esc(report.previous.old.id || "")} · ${esc(report.previous.old.status_label)}${report.previous.old.value_pct === null ? "" : ` · ${report.previous.old.value_pct}%`}</dd>
        <dt>本份</dt><dd>${esc(report.previous.new.id || "")} · ${esc(report.previous.new.status_label)}</dd>
      </dl>
      <ul class="plain">${report.previous.affected_parts.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>
      ${report.previous.changed_cells.length ? `<p class="hint">判定变化月份：${report.previous.changed_cells.slice(0, 12).map((c) => `${esc(c.grid)} ${esc(c.month)}（${esc(c.change)}）`).join("；")}</p>` : ""}
    </div>` : "";

  $("#result-body").innerHTML = `
    <div class="card">
      <div class="section-title">
        <h2 style="margin:0">主估计</h2>
        <span class="pill ${statusClass}">${esc(est.status_label)}</span>
      </div>
      <div class="big">${est.value === null ? "不能估计" : signedPct(est.value)}</div>
      <p class="muted">${esc(est.ci_method)}${est.naive_low !== null ? `；合并二项区间 ${signedPct(est.naive_low)} ~ ${signedPct(est.naive_high)}（低估不确定性，仅参考）` : ""}</p>
      ${ciBar(est)}
      <dl class="kv" style="margin-top:12px">
        <dt>可比月份</dt><dd>${list(est.months_used)}</dd>
        <dt>实施网格</dt><dd>${list(est.treated_grids)}</dd>
        <dt>对照网格</dt><dd>${list(est.control_grids)}</dd>
        <dt>成熟事项</dt><dd>实施侧 ${est.treated_matured} 件，对照侧 ${est.control_matured} 件</dd>
        <dt>区间方法</dt><dd>${esc(est.ci_method)}</dd>
      </dl>
      ${est.notes.length ? `<ul class="plain">${est.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
    </div>

    <div class="card">
      <h2 style="margin-top:0">结论与建议</h2>
      <p class="${can ? "" : "muted"}"><b>${can ? "可以据此讨论下一步" : "不能下结论"}</b>：${esc(conclusion.text)}</p>
      <p>${esc(conclusion.recommendation)}</p>
      <div class="sens-grid">
        <div><h3>扩大</h3><p class="muted">${esc(conclusion.decision_options.expand)}</p></div>
        <div><h3>调整</h3><p class="muted">${esc(conclusion.decision_options.adjust)}</p></div>
        <div><h3>继续观察</h3><p class="muted">${esc(conclusion.decision_options.observe)}</p></div>
      </div>
      <h3>本报告不允许的读法</h3>
      <ul class="plain">${conclusion.prohibited.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>
    </div>

    <div class="card">
      <h3>按月差异（同期对照）</h3>
      <div class="scroll"><table>
        <thead><tr><th>月份</th><th class="num">实施侧按期率</th><th class="num">对照侧按期率</th><th class="num">差异</th><th>实施网格</th><th>对照网格</th></tr></thead>
        <tbody>${monthly || '<tr><td colspan="6" class="muted">没有可比月份</td></tr>'}</tbody>
      </table></div>
      <p class="hint">逐月差异只用于看结构；主估计按成熟事项（及人口）汇总，逐月数值波动不应单独解读。</p>
    </div>

    <div class="card">
      <h3>适用范围</h3>
      <dl class="kv">
        <dt>实施网格覆盖人口</dt><dd>${scope.treated_population} / ${scope.total_population}（${pct(scope.population_share)}）</dd>
        <dt>网格类型</dt><dd>${list(scope.area_types)}</dd>
        <dt>对照观测</dt><dd>${scope.control_observations} 个网格月；其中临近自身启动 ${pct(scope.soon_to_start_share)}，始终未启动 ${pct(scope.never_started_share)}</dd>
        <dt>单对照月份</dt><dd>${scope.single_grid_control_months.length ? scope.single_grid_control_months.join("、") + "（仅 1 个对照网格）" : "每个可比月份都有多个对照网格"}</dd>
        <dt>同期冲击</dt><dd>${scope.shock_types_present.length ? list(scope.shock_types_present) : "比较时段内未登记"}</dd>
      </dl>
      <p class="hint">这些范围之外（例如启动后的月份、未覆盖的网格类型）不能直接套用本估计。</p>
    </div>

    ${adjCard}

    <h2>各设定下的结果（不是挑选，而是看结论依赖什么）</h2>
    <div class="sens-grid">${sensitivity}</div>

    <div class="card">
      <h3>局限</h3>
      <ul class="plain">${report.limitations.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
    </div>
    ${previous}`;
}

// ---------------- boot ----------------
(async function boot() {
  try {
    await refreshState();
    if (!state.protocol) {
      banner("第一次使用：先冻结方案（口径与比较时段），再运行分析。", "warn");
      const draft = await api("/api/draft");
      fillForm(draft.draft);
    }
  } catch (err) {
    banner(err.message, "error");
  }
})();
