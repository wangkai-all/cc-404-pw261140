/* 指标口径双算平台前端：原生 JS，无外部依赖。 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const api = {
  async get(path) {
    const r = await fetch(path);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const j = await r.json();
    if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
    return j.data;
  },
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => Number(n).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
const STATUS_ZH = { candidate: "候选", approved: "已批准", superseded: "已替换", withdrawn: "已撤回" };
const POLICY_ZH = { dual: "双算过渡", whole: "整版替换", delayed: "延后升级" };
const KIND_ZH = { same: "一致", added: "新口径新增", removed: "新口径移除", changed: "值变化", excluded_both: "均排除" };

let META = { dimension_fields: [], formulas: {} };
let OVER = null;

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  setTimeout(() => t.classList.add("hidden"), 3200);
  t.classList.remove("hidden");
}

// ------------------------------------------------------------ Tab 切换
$$(".tab").forEach((btn) => btn.addEventListener("click", () => {
  $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`));
}));

// ------------------------------------------------------------ 总览
function renderDashboard() {
  const o = OVER;
  const stale = o.impacts.filter((i) => i.status === "pending").length;
  $("#kpis").innerHTML = [
    ["原始订单", fmt(o.order_count), "虚构订单（确定性种子）"],
    ["指标", o.metrics.length, "GMV / 有效订单数"],
    ["口径版本", o.calibers.length, `${o.calibers.filter((c) => c.status === "candidate").length} 个候选中`],
    ["报表", o.reports.length, `${o.reports.filter((r) => r.upgrade_policy === "delayed").length} 份延后升级`],
    ["待处理影响", stale, "口径变化待重算/审批事项"],
  ].map(([lbl, num, sub]) => `<div class="kpi"><div class="lbl">${lbl}</div>
     <div class="num">${num}</div><div class="muted">${sub}</div></div>`).join("");

  $("#dash-grid").innerHTML = `
    <div>${o.calibers.map(caliberCard).join("")}</div>
    <div>${o.reports.map(reportCard).join("")}
      <div class="item"><div class="title">计算引擎</div>
      ${o.engines.map((e) => `<div class="meta"><span>${esc(e.name)} <code>${e.engine_id}</code></span>
        <span class="tag ${e.status === "active" ? "approved" : "withdrawn"}">${e.status === "active" ? "在役" : "已退出"}</span>
        <span>服务口径: ${e.calibers.map(esc).join(", ") || "—"}</span></div>`).join("")}
      </div></div>`;
}

function caliberCard(c) {
  const filters = c.filters.length
    ? c.filters.map((f) => `${f.field} ${f.op} ${JSON.stringify(f.values ?? f.value)}`).join("；")
    : "无过滤";
  return `<div class="item">
    <div class="title"><code>${esc(c.caliber_id)}</code>
      <span class="tag ${c.status}">${STATUS_ZH[c.status]}</span>
      <span class="muted">rev ${c.revision}</span></div>
    <div class="meta"><span>${esc(c.description)}</span></div>
    <div class="rule-box">
      <b>时间归属：</b>${c.time_attr === "order_time" ? "下单时间" : "支付时间"}
      <b>公式：</b>${esc(c.formula)}（${esc(META.formulas[c.formula] || "")}）<br>
      <b>维度过滤：</b>${esc(filters)}
      ${c.change_note ? `<br><b>变更说明：</b>${esc(c.change_note)}` : ""}
    </div></div>`;
}

function reportCard(r) {
  const prods = OVER.products.filter((p) => p.report_id === r.report_id);
  return `<div class="item">
    <div class="title">${esc(r.name)} <code>${r.report_id}</code>
      <span class="tag policy-${r.upgrade_policy}">${POLICY_ZH[r.upgrade_policy]}</span></div>
    <div class="meta"><span>指标: ${esc(r.metric_id)}</span>
      <span>拆分: ${r.breakdown_dim === "none" ? "不拆分" : esc(r.breakdown_dim)}</span>
      ${r.delayed_until ? `<span>延后至 ${esc(r.delayed_until)}</span>` : ""}
      <span>产物 ${prods.length} 份</span></div></div>`;
}

// ------------------------------------------------------------ 口径登记
function renderCaliberTab() {
  const metricSel = $('#form-candidate select[name="metric_id"]');
  metricSel.innerHTML = OVER.metrics.map((m) =>
    `<option value="${m.metric_id}">${esc(m.name)}（${m.metric_id}）</option>`).join("");
  const formulaSel = $('#form-candidate select[name="formula"]');
  formulaSel.innerHTML = Object.entries(META.formulas).map(([k, v]) =>
    `<option value="${k}">${esc(k)} — ${esc(v)}</option>`).join("");
  metricSel.addEventListener("change", () => {
    const m = OVER.metrics.find((x) => x.metric_id === metricSel.value);
    if (m) formulaSel.value = m.default_formula;
  });
  metricSel.dispatchEvent(new Event("change"));

  $("#filter-rows").innerHTML = "";
  $("#caliber-list").innerHTML = OVER.calibers.map((c) => {
    const cand = c.status === "candidate";
    return `<div class="item">${caliberCard(c).replace('<div class="item">', "").replace(/<\/div>$/, "")}
      <div class="actions">
        ${cand ? `<button class="mini" data-act="approve" data-id="${c.caliber_id}">批准口径</button>
        <button class="mini ghost" data-act="amend" data-id="${c.caliber_id}">修订</button>
        <button class="mini danger" data-act="withdraw" data-id="${c.caliber_id}">撤回</button>` : ""}
      </div></div>`;
  }).join("");
  $("#caliber-list").addEventListener("click", onCaliberAction, { once: true });
}

function onCaliberAction(ev) {
  const btn = ev.target.closest("button[data-act]");
  $("#caliber-list").addEventListener("click", onCaliberAction, { once: true });
  if (!btn) return;
  const { act, id } = btn.dataset;
  if (act === "approve") doApprove(id);
  if (act === "withdraw") doWithdraw(id);
  if (act === "amend") showAmend(id);
}

function addFilterRow(container, f = {}) {
  const row = document.createElement("div");
  row.className = "filter-row";
  row.innerHTML = `
    <select class="f-field">${META.dimension_fields.map((d) =>
      `<option value="${d}" ${f.field === d ? "selected" : ""}>${d}</option>`).join("")}</select>
    <select class="f-op">
      ${["in", "not_in", "eq", "ne"].map((op) =>
        `<option value="${op}" ${f.op === op ? "selected" : ""}>${op}</option>`).join("")}
    </select>
    <input class="f-val" placeholder='in/not_in 填逗号分隔值；eq/ne 填单值' value="${esc(
      f.values ? f.values.join(",") : (f.value ?? ""))}">
    <button type="button" class="mini danger f-del">×</button>`;
  row.querySelector(".f-del").addEventListener("click", () => row.remove());
  container.appendChild(row);
}

function collectFilters(container) {
  return $$(".filter-row", container).map((row) => {
    const field = row.querySelector(".f-field").value;
    const op = row.querySelector(".f-op").value;
    const raw = row.querySelector(".f-val").value.trim();
    const vals = raw.split(",").map((s) => s.trim()).filter(Boolean);
    if (op === "in" || op === "not_in") {
      const num = vals.map(Number);
      return { field, op, values: num.every((n) => !Number.isNaN(n)) ? num : vals };
    }
    const v = vals[0] ?? "";
    return { field, op, value: Number.isNaN(Number(v)) ? v : Number(v) };
  });
}

$("#btn-add-filter").addEventListener("click", () => addFilterRow($("#filter-rows")));

$("#form-candidate").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  try {
    const data = await api.post("/api/calibers", {
      metric_id: fd.get("metric_id"),
      version: fd.get("version"),
      description: fd.get("description"),
      time_attr: fd.get("time_attr"),
      formula: fd.get("formula"),
      filters: collectFilters($("#filter-rows")),
      change_note: fd.get("change_note"),
    });
    toast(`已登记 ${data.caliber_id}，自动双算 ${data.periods.length} 个期间 × ${data.reports.length} 份报表`);
    ev.target.reset();
    $("#filter-rows").innerHTML = "";
    await refreshAll();
  } catch (e) { toast(e.message, true); }
});

function showAmend(caliberId) {
  const c = OVER.calibers.find((x) => x.caliber_id === caliberId);
  const box = $("#amend-box");
  box.classList.remove("hidden");
  $("#amend-id").textContent = caliberId;
  $("#form-amend").innerHTML = `
    <label>时间归属 <select name="time_attr">
      <option value="order_time" ${c.time_attr === "order_time" ? "selected" : ""}>下单时间</option>
      <option value="pay_time" ${c.time_attr === "pay_time" ? "selected" : ""}>支付时间</option></select></label>
    <label>公式 <select name="formula">${Object.entries(META.formulas).map(([k, v]) =>
      `<option value="${k}" ${c.formula === k ? "selected" : ""}>${esc(k)} — ${esc(v)}</option>`).join("")}</select></label>
    <div class="filters-edit"><div class="row-head">维度过滤（清空=无过滤）</div>
      <div id="amend-filter-rows"></div>
      <button type="button" class="ghost" id="btn-amend-add">+ 增加过滤条件</button></div>
    <label>变更说明 <textarea name="change_note" rows="2">${esc(c.change_note)}</textarea></label>
    <button type="submit">提交修订并重算</button>
    <button type="button" class="ghost" id="btn-amend-cancel">取消</button>`;
  const fc = $("#amend-filter-rows");
  c.filters.forEach((f) => addFilterRow(fc, f));
  $("#btn-amend-add").addEventListener("click", () => addFilterRow(fc));
  $("#btn-amend-cancel").addEventListener("click", () => box.classList.add("hidden"));
  $("#form-amend").onsubmit = async (ev2) => {
    ev2.preventDefault();
    const f2 = new FormData(ev2.target);
    try {
      await api.post("/api/calibers/amend", {
        caliber_id: caliberId,
        time_attr: f2.get("time_attr"),
        formula: f2.get("formula"),
        filters: collectFilters(fc),
        change_note: f2.get("change_note"),
      });
      toast("修订完成，影响已重新登记，候选产物已重算");
      box.classList.add("hidden");
      await refreshAll();
    } catch (e) { toast(e.message, true); }
  };
}

async function doApprove(id) {
  try {
    const d = await api.post("/api/calibers/approve", { caliber_id: id });
    toast(d.delayed_reports.length
      ? `已批准 ${id}；${d.delayed_reports.join("、")} 按策略保持旧口径（延后升级）`
      : `已批准 ${id}，旧口径产物已标记为已替换`);
    await refreshAll();
  } catch (e) { toast(e.message, true); }
}
async function doWithdraw(id) {
  if (!confirm(`确认撤回候选口径 ${id}？其产物将全部作废、引擎解绑。`)) return;
  try {
    await api.post("/api/calibers/withdraw", { caliber_id: id });
    toast(`已撤回 ${id}`);
    await refreshAll();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------ 报表与差异
function renderReportTab() {
  const sel = $("#report-select");
  sel.innerHTML = OVER.reports.map((r) =>
    `<option value="${r.report_id}">${esc(r.name)}</option>`).join("");
  sel.onchange = loadReportDetail;
  loadReportDetail();
}

async function loadReportDetail() {
  const reportId = $("#report-select").value;
  if (!reportId) return;
  const d = await api.get(`/api/report-detail?report=${encodeURIComponent(reportId)}`);
  const r = d.report;
  $("#report-policy").innerHTML = `升级策略：<span class="tag policy-${r.upgrade_policy}">${POLICY_ZH[r.upgrade_policy]}</span>
    ${r.delayed_until ? `（延后至 ${esc(r.delayed_until)}）` : ""}
    ${r.upgrade_policy === "delayed"
      ? `<button class="mini" id="btn-lift">结束延后，切换到当前批准口径</button>` : ""}`;
  const lift = $("#btn-lift");
  if (lift) lift.onclick = async () => {
    try { await api.post("/api/reports/lift-delay", { report_id: reportId });
      toast("已结束延后，报表切换至当前批准口径"); await refreshAll(); }
    catch (e) { toast(e.message, true); }
  };

  const periods = [...new Set(d.periods.map((p) => p.period_start))].sort();
  $("#diff-period").innerHTML = periods.map((p) =>
    `<option value="${p}">${p.slice(0, 7)}</option>`).join("");

  $("#report-detail").innerHTML = periods.map((ps) => {
    const prods = d.periods.filter((p) => p.period_start === ps);
    return `<h3>${ps.slice(0, 7)}（期间 ${ps} ~ ${prods[0].period_end}）</h3>
      <div class="table-wrap"><table><thead><tr>
        <th>口径</th><th>状态</th><th class="num">合计</th><th class="num">计入行数</th>
        <th>输入已不同</th><th>计算引擎</th><th>维度拆分</th></tr></thead><tbody>
      ${prods.map((p) => `<tr>
        <td><code>${esc(p.caliber_version)}</code></td>
        <td><span class="tag ${p.calc_status}">${p.calc_status}</span></td>
        <td class="num">${fmt(p.total)}</td>
        <td class="num">${p.row_count}</td>
        <td>${p.inputs_differ ? '<span class="tag no">是（输入行集合不同）</span>' : '<span class="tag yes">否</span>'}</td>
        <td><code>${esc(p.engine_id || "—")}</code></td>
        <td class="muted">${p.groups.map((g) => `${esc(g.group_key)}:${fmt(g.subtotal)}`).join(" / ")}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }).join("");
}

$("#btn-diff").addEventListener("click", async () => {
  const reportId = $("#report-select").value;
  const ps = $("#diff-period").value;
  try {
    const pairs = await api.get(
      `/api/report-detail?report=${encodeURIComponent(reportId)}`);
    const prods = pairs.periods.filter((p) => p.period_start === ps);
    if (prods.length < 2) { toast("该期间只有一个口径的产物，无法对比（先登记候选口径完成双算）", true); return; }
    const oldP = prods.find((p) => p.caliber_id.endsWith("-v1")) || prods[0];
    const newP = prods.find((p) => !p.caliber_id.endsWith("-v1")) || prods[1];
    const d = await api.get(`/api/diff?a=${oldP.product_id}&b=${newP.product_id}`);
    renderDiff(d);
  } catch (e) { toast(e.message, true); }
});

function renderDiff(d) {
  const onlyChanges = $("#diff-only-changes").checked;
  const rows = onlyChanges ? d.rows.filter((r) => r.kind !== "same" && r.kind !== "excluded_both") : d.rows;
  $("#diff-result").innerHTML = `
    <div class="verdict ${d.bridge_closed ? "safe" : "unsafe"}">
      桥接校验：${d.bridge_closed ? "闭合 ✓ 旧合计 ± 逐行差异 = 新合计" : "未闭合 ✗"}
    </div>
    <div class="bridge">
      ${d.bridge.map((s, i) => `${i ? '<div class="arrow">→</div>' : ""}
        <div class="step"><div class="lbl muted">${esc(s.stage)}</div>
        <div class="v">${fmt(s.amount)}</div></div>`).join("")}
    </div>
    <p class="muted">差异统计：一致 ${d.summary.same} 单 · 新口径新增 ${d.summary.added} 单 ·
      新口径移除 ${d.summary.removed} 单 · 值变化 ${d.summary.changed} 单 ·
      共扫描 ${d.summary.total_orders} 单；存储合计与逐行重建合计：
      ${d.product_a.total_stored} / ${d.product_a.total_rebuilt}，
      ${d.product_b.total_stored} / ${d.product_b.total_rebuilt}</p>
    <div class="table-wrap"><table><thead><tr>
      <th>差异类型</th><th>订单号</th><th>下单时间</th><th>支付时间</th><th>状态</th>
      <th>渠道</th><th>大区</th><th>品类</th><th>赠品</th>
      <th class="num">旧口径贡献</th><th class="num">候选贡献</th><th class="num">差异</th>
      <th>旧口径原因</th><th>候选口径原因</th></tr></thead><tbody>
      ${rows.map((r) => `<tr class="kind-${r.kind}">
        <td><span class="diff-kind ${r.kind}">${KIND_ZH[r.kind]}</span></td>
        <td><code>${esc(r.order_id)}</code></td>
        <td>${esc(r.order_time)}</td><td>${esc(r.pay_time || "—")}</td>
        <td>${esc(r.status)}</td><td>${esc(r.channel)}</td><td>${esc(r.region)}</td>
        <td>${esc(r.category)}</td><td>${r.is_gift ? "是" : "否"}</td>
        <td class="num">${r.in_a ? fmt(r.value_a) : "—"}</td>
        <td class="num">${r.in_b ? fmt(r.value_b) : "—"}</td>
        <td class="num ${r.delta > 0 ? "delta-pos" : r.delta < 0 ? "delta-neg" : ""}">${fmt(r.delta)}</td>
        <td class="muted">${esc(r.reason_a)}</td><td class="muted">${esc(r.reason_b)}</td>
      </tr>`).join("")}
    </tbody></table></div>`;
}
$("#diff-only-changes").addEventListener("change", () => {
  if (lastDiff) renderDiff(lastDiff);
});
let lastDiff = null;
const origRenderDiff = renderDiff;
renderDiff = function (d) { lastDiff = d; origRenderDiff(d); };

// ------------------------------------------------------------ 影响列表
function renderImpacts() {
  $("#impact-list").innerHTML = OVER.impacts.map((i) => `
    <div class="item">
      <div class="title">
        <code>${esc(i.caliber_version)}</code> 影响 <b>${esc(i.report_name)}</b>
        <code>${esc(i.period_start.slice(0, 7))}</code>
        <span class="tag ${i.status}">${{ pending: "待处理", recomputed: "已重算", withdrawn: "随撤回关闭", ignored: "忽略" }[i.status]}</span>
      </div>
      <div class="meta">
        <span>变更点: ${esc(i.change_types.replaceAll(",", "、").replaceAll("time_attr", "时间归属").replaceAll("filter:", "过滤-").replaceAll("formula", "公式"))}</span>
        <span class="tag ${i.comparable ? "yes" : "no"}">${i.comparable ? "可比较：同窗口同公式" : "不可直接比较"}</span>
        <span class="tag ${i.inputs_differ ? "no" : "yes"}">${i.inputs_differ ? "输入已不同" : "输入行集合相同"}</span>
        <span>策略: ${POLICY_ZH[i.upgrade_policy]}</span>
      </div>
      <div class="rule-box">${esc(i.note)}</div>
    </div>`).join("") || '<p class="muted">暂无影响登记。登记候选口径后自动生成。</p>';
}

// ------------------------------------------------------------ 模拟
function renderSimTab() {
  const cands = OVER.calibers.filter((c) => c.status === "candidate");
  $("#sim-caliber").innerHTML = cands.length
    ? cands.map((c) => `<option value="${c.caliber_id}">${esc(c.caliber_id)}（${esc(c.description)}）</option>`).join("")
    : `<option value="">（暂无候选口径）</option>`;
  $("#sim-engine").innerHTML = OVER.engines.map((e) =>
    `<option value="${e.engine_id}">${esc(e.name)}（${e.status}）</option>`).join("");
  $("#sim-engine").onchange = () => {
    $("#btn-reactivate").classList.toggle("hidden",
      OVER.engines.find((e) => e.engine_id === $("#sim-engine").value)?.status !== "retired");
  };
  $("#sim-engine").dispatchEvent(new Event("change"));
}

$("#btn-sim-withdraw").addEventListener("click", async () => {
  const id = $("#sim-caliber").value;
  if (!id) return;
  try {
    const s = await api.get(`/api/simulate/withdraw?caliber=${encodeURIComponent(id)}`);
    $("#sim-withdraw-result").innerHTML = `
      <div class="verdict ${s.safe ? "safe" : "unsafe"}">
        ${s.safe ? "可以安全撤回：所有报表期间都能回退到父口径 " + s.parent_caliber : "撤回会造成断档：存在无法回退的报表期间！"}
      </div>
      <div class="item"><div class="meta"><span>拟作废候选产物: ${s.would_withdraw_products} 份</span>
        <span>父口径状态: ${s.parent_still_approved ? "在役已批准" : "已被替换"}</span></div>
        <div class="rule-box">${esc(s.explanation)}</div>
        <div class="table-wrap"><table><thead><tr><th>报表</th><th>期间</th><th>回退产物</th></tr></thead><tbody>
        ${s.report_coverage.map((x) => `<tr><td>${esc(x.report_name)}</td><td>${esc(x.period)}</td>
          <td>${x.fallback_product_id ? `<code>#${x.fallback_product_id}</code>` : '<span class="tag no">无回退</span>'}</td></tr>`).join("")}
        </tbody></table></div></div>`;
  } catch (e) { toast(e.message, true); }
});

$("#btn-real-withdraw").addEventListener("click", async () => {
  const id = $("#sim-caliber").value;
  if (!id) return;
  await doWithdraw(id);
  $("#sim-withdraw-result").innerHTML = "";
  renderSimTab();
});

$("#btn-sim-retire").addEventListener("click", async () => {
  const id = $("#sim-engine").value;
  try {
    const s = await api.get(`/api/simulate/retire?engine=${encodeURIComponent(id)}`);
    $("#sim-retire-result").innerHTML = `
      <div class="verdict ${s.safe ? "safe" : "unsafe"}">
        ${s.safe ? "可以安全退出：所有口径均有其他在役引擎承接" : "退出被阻塞：存在独占口径或仍由该引擎计算的有效产物"}
      </div>
      <div class="item"><div class="meta">
        <span>服务口径: ${s.served_calibers.map(esc).join(", ") || "—"}</span>
        <span>独占口径: ${s.exclusive_calibers.map(esc).join(", ") || "无"}</span>
        <span>在役产物: ${s.active_products_on_engine} 份</span></div>
        <div class="rule-box">${esc(s.explanation)}</div>
        ${s.blocking_products.length ? `<div class="table-wrap"><table><thead><tr><th>阻塞产物</th><th>报表</th><th>期间</th><th>口径</th></tr></thead><tbody>
          ${s.blocking_products.map((b) => `<tr><td><code>#${b.product_id}</code></td><td>${esc(b.report_name)}</td>
            <td>${esc(b.period)}</td><td><code>${esc(b.caliber_id)}</code></td></tr>`).join("")}
        </tbody></table></div>` : '<p class="muted">无阻塞项。</p>'}</div>`;
  } catch (e) { toast(e.message, true); }
});

$("#btn-real-retire").addEventListener("click", async () => {
  const id = $("#sim-engine").value;
  try { await api.post("/api/engines/retire", { engine_id: id });
    toast(`引擎 ${id} 已退出`); await refreshAll(); }
  catch (e) { toast(e.message, true); }
});
$("#btn-reactivate").addEventListener("click", async () => {
  const id = $("#sim-engine").value;
  try { await api.post("/api/engines/reactivate", { engine_id: id });
    toast(`引擎 ${id} 已重新启用`); await refreshAll(); }
  catch (e) { toast(e.message, true); }
});

// ------------------------------------------------------------ 订单查询
async function renderOrdersTab() {
  const sample = OVER.products.length ? null : null;
  const orders = await api.get("/api/orders?limit=10");
  fillOrderSelects(orders);
  doOrderQuery();
}

function fillOrderSelects(orders) {
  const uniq = (key) => [...new Set(orders.map((o) => o[key]))].sort();
  api.get("/api/orders?limit=1000").then((all) => {
    for (const key of ["status", "channel", "region", "category"]) {
      const sel = $(`#form-orders select[name="${key}"]`);
      const cur = sel.value;
      sel.innerHTML = `<option value="">全部</option>` +
        unixToOpts(uniq2(all, key));
      sel.value = cur;
    }
  });
}
const uniq2 = (arr, key) => [...new Set(arr.map((o) => o[key]))].sort();
const unixToOpts = (vals) => vals.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");

$("#form-orders").addEventListener("submit", (ev) => { ev.preventDefault(); doOrderQuery(); });

async function doOrderQuery() {
  const fd = new FormData($("#form-orders"));
  const qs = new URLSearchParams();
  for (const [k, v] of fd.entries()) if (v) qs.set(k, v);
  qs.set("limit", "500");
  const rows = await api.get(`/api/orders?${qs.toString()}`);
  $("#order-count").textContent = `（命中 ${rows.length} 条，最多展示 500）`;
  $("#orders-result").innerHTML = `<div class="table-wrap"><table><thead><tr>
    <th>订单号</th><th>下单时间</th><th>支付时间</th><th>状态</th><th>渠道</th><th>大区</th>
    <th>品类</th><th class="num">订单金额</th><th class="num">支付金额</th><th class="num">退款</th>
    <th>赠品</th><th>客户</th></tr></thead><tbody>
    ${rows.map((o) => `<tr><td><code>${esc(o.order_id)}</code></td>
      <td>${esc(o.order_time)}</td><td>${esc(o.pay_time || "—")}</td>
      <td>${esc(o.status)}</td><td>${esc(o.channel)}</td><td>${esc(o.region)}</td>
      <td>${esc(o.category)}</td><td class="num">${fmt(o.amount)}</td>
      <td class="num">${fmt(o.pay_amount)}</td><td class="num">${fmt(o.refund_amount)}</td>
      <td>${o.is_gift ? "是" : "否"}</td><td>${esc(o.customer_id)}</td></tr>`).join("")}
  </tbody></table></div>`;
}

// ------------------------------------------------------------ 审计
async function renderAudit() {
  const logs = await api.get("/api/audit?limit=150");
  $("#audit-list").innerHTML = `<div class="table-wrap"><table><thead><tr>
    <th>时间</th><th>实体</th><th>对象</th><th>动作</th><th>详情</th><th>操作人</th></tr></thead><tbody>
    ${logs.map((l) => `<tr><td>${esc(l.ts)}</td><td>${esc(l.entity_type)}</td>
      <td><code>${esc(l.entity_id)}</code></td><td>${esc(l.action)}</td>
      <td class="muted">${esc(l.detail_json)}</td><td>${esc(l.actor)}</td></tr>`).join("")}
  </tbody></table></div>`;
}

// ------------------------------------------------------------ 全局刷新
async function refreshAll() {
  META = await api.get("/api/meta");
  OVER = await api.get("/api/overview");
  renderDashboard();
  renderCaliberTab();
  renderReportTab();
  renderImpacts();
  renderSimTab();
  renderAudit();
}

(async function init() {
  try {
    await refreshAll();
    await renderOrdersTab();
  } catch (e) {
    toast("初始化失败: " + e.message, true);
  }
})();
