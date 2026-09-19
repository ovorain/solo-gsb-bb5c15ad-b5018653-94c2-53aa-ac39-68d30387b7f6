/* 公交刷卡票价核验台 —— 原生前端逻辑（无框架、无构建） */
"use strict";

const $ = (id) => document.getElementById(id);

/* ---------------- 通用请求 / 提示 ---------------- */

async function api(path, options = {}) {
  const opts = Object.assign({ headers: {} }, options);
  if (opts.body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(path, opts);
  let data = null;
  try {
    data = await resp.json();
  } catch (_) { /* 非 JSON 响应 */ }
  if (!resp.ok) {
    const err = new Error("HTTP " + resp.status);
    err.payload = data;
    throw err;
  }
  return data;
}

let toastTimer = null;
function toast(message, ok = true) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast " + (ok ? "ok" : "bad");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

function putText(path, text) {
  return api(path, { method: "PUT", body: JSON.stringify({ text }) });
}

function yuan(v) {
  return Number(v).toFixed(2);
}

function actionText(a) {
  return a === "IN" ? "进 ▲" : "出 ▼";
}

/* ---------------- 1. 规则参数 ---------------- */

async function loadConfig() {
  const cfg = await api("/api/config");
  $("cfg-split").value = cfg.day_split_hour;
  $("cfg-minutes").value = cfg.transfer_minutes;
  $("cfg-discount").value = cfg.transfer_discount;
  $("cfg-cap").value = cfg.daily_cap;
  return cfg;
}

async function saveConfig() {
  const body = {
    day_split_hour: Number($("cfg-split").value),
    transfer_minutes: Number($("cfg-minutes").value),
    transfer_discount: Number($("cfg-discount").value),
    daily_cap: Number($("cfg-cap").value),
  };
  try {
    await api("/api/config", { method: "PUT", body: JSON.stringify(body) });
    toast("规则参数已保存");
  } catch (e) {
    toast(summaryError(e, "参数校验失败"), false);
  }
}

/* ---------------- 2. 规则试算对比 ---------------- */

function fillTrialInputs(cfg) {
  $("trial-split").value = cfg.day_split_hour;
  $("trial-minutes").value = cfg.transfer_minutes;
  $("trial-discount").value = cfg.transfer_discount;
  $("trial-cap").value = cfg.daily_cap;
}

async function copyCurrentToTrial() {
  fillTrialInputs(await api("/api/config"));
  toast("已将当前已保存参数填入草案，可在此基础上修改");
}

function fmtConfig(c) {
  return `分界 ${c.day_split_hour}:00 · 换乘 ${c.transfer_minutes} 分钟 · ` +
         `抵扣 ${yuan(c.transfer_discount)} 元 · 封顶 ${yuan(c.daily_cap)} 元`;
}

function deltaClass(v) {
  return v > 0 ? "delta-up" : v < 0 ? "delta-down" : "delta-zero";
}

function deltaText(v) {
  return (v > 0 ? "+" : "") + yuan(v);
}

function renderTrialErrors(p) {
  const panel = $("trial-errors");
  panel.hidden = false;
  $("trial-result").hidden = true;

  const sides = [
    ["trial-err-current", p.current],
    ["trial-err-draft", p.draft],
  ];
  for (const [blockId, side] of sides) {
    const block = $(blockId);
    const ul = block.querySelector("ul");
    ul.innerHTML = "";
    const errors = (side && side.errors) || [];
    for (const e of errors) ul.appendChild(renderErrorLi(e));
    block.hidden = side ? side.ok !== false : true;
  }
}

function renderTrialResult(data) {
  $("trial-errors").hidden = true;
  const panel = $("trial-result");
  panel.hidden = false;

  $("trial-params").innerHTML =
    `<div><span class="tag tag-cur">当前配置</span>${fmtConfig(data.current.config)}</div>` +
    `<div><span class="tag tag-draft">草案配置</span>${fmtConfig(data.draft.config)}</div>`;

  const b = data.batch_summary;
  $("trial-batch").innerHTML =
    `全批次 <strong>${b.trip_count}</strong> 笔行程：` +
    `实扣合计 <strong>${yuan(b.current_charge_total)}</strong> 元 → ` +
    `<strong>${yuan(b.draft_charge_total)}</strong> 元，` +
    `<span class="${deltaClass(b.charge_diff)}">实扣差额 ${deltaText(b.charge_diff)} 元</span>` +
    `；优惠合计 ${yuan(b.current_discount_total)} → ${yuan(b.draft_discount_total)} 元` +
    `（优惠差额 ${deltaText(b.discount_diff)} 元，正=优惠更多）`;

  const tb = $("trial-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const r of data.rows) {
    const tr = document.createElement("tr");
    const trip = `${escapeHtml(r.entry_station)} → ${escapeHtml(r.exit_station)}` +
      `<div class="sub">${r.entry_time} ～ ${r.exit_time}</div>`;
    // 优惠差额正负的颜色与实扣相反：优惠变多（正）对乘客是好事，用绿色
    const discCls = r.discount_diff > 0 ? "delta-down"
      : r.discount_diff < 0 ? "delta-up" : "delta-zero";
    tr.innerHTML = `
      <td>${escapeHtml(r.card)}</td>
      <td class="num">${r.seq}</td>
      <td class="trip">${trip}</td>
      <td class="${r.day_changed ? "day-changed" : ""}">${r.current_operating_day}</td>
      <td class="${r.day_changed ? "day-changed" : ""}">${r.draft_operating_day}</td>
      <td class="num discount">-${yuan(r.current_discount)}</td>
      <td class="num discount">-${yuan(r.draft_discount)}</td>
      <td class="num ${discCls}">${deltaText(r.discount_diff)}</td>
      <td class="num">${yuan(r.current_charge)}</td>
      <td class="num charge">${yuan(r.draft_charge)}</td>
      <td class="num ${deltaClass(r.charge_diff)}">${deltaText(r.charge_diff)}</td>
      <td class="wrap">${escapeHtml(r.draft_discount_source)}</td>`;
    tb.appendChild(tr);
  }

  const cb = $("trial-cards-table").querySelector("tbody");
  cb.innerHTML = "";
  for (const c of data.card_summaries) {
    const tr = document.createElement("tr");
    const discCls = c.discount_diff > 0 ? "delta-down"
      : c.discount_diff < 0 ? "delta-up" : "delta-zero";
    tr.innerHTML = `
      <td>${escapeHtml(c.card)}</td>
      <td class="num">${c.trip_count}</td>
      <td class="num">${yuan(c.current_charge_total)}</td>
      <td class="num">${yuan(c.draft_charge_total)}</td>
      <td class="num ${deltaClass(c.charge_diff)}">${deltaText(c.charge_diff)}</td>
      <td class="num discount">${yuan(c.current_discount_total)}</td>
      <td class="num discount">${yuan(c.draft_discount_total)}</td>
      <td class="num ${discCls}">${deltaText(c.discount_diff)}</td>`;
    cb.appendChild(tr);
  }
}

async function runTrialCompare() {
  const status = $("trial-status");
  const body = {
    day_split_hour: Number($("trial-split").value),
    transfer_minutes: Number($("trial-minutes").value),
    transfer_discount: Number($("trial-discount").value),
    daily_cap: Number($("trial-cap").value),
  };
  if (Object.values(body).some((v) => !Number.isFinite(v))) {
    status.textContent = "草案四项参数必须全部填写为有效数字";
    status.className = "status bad";
    return;
  }
  status.textContent = "试算中…";
  status.className = "status";
  $("trial-errors").hidden = true;
  $("trial-result").hidden = true;
  try {
    const data = await api("/api/trial-compare", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderTrialResult(data);
    status.textContent =
      `试算成功：共对比 ${data.batch_summary.trip_count} 笔行程（草案未保存、账单未写入）`;
    status.className = "status ok";
    toast("规则试算完成");
  } catch (e) {
    const p = e.payload;
    if (p && (p.current || p.draft)) {
      renderTrialErrors(p);
      const failed = [];
      if (p.current && p.current.ok === false) failed.push("当前");
      if (p.draft && p.draft.ok === false) failed.push("草案");
      status.textContent =
        `试算未通过：${failed.join("、")}方案存在错误（配置未改动、账单未写入）`;
    } else {
      status.textContent = summaryError(e, "试算请求失败");
    }
    status.className = "status bad";
    toast("规则试算未通过", false);
  }
}

/* ---------------- 2-4. 文本录入与表格渲染 ---------------- */

function renderStations(items) {
  const tb = $("stations-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const s of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(s.name)}</td><td class="num">${s.zone}</td>`;
    tb.appendChild(tr);
  }
}

function renderFares(items) {
  const tb = $("fares-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const f of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="num">${f.from_zone}</td><td class="num">${f.to_zone}</td>
      <td class="num">${yuan(f.price)}</td>`;
    tb.appendChild(tr);
  }
}

function renderRecords(items) {
  const tb = $("records-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const r of items) {
    const tr = document.createElement("tr");
    tr.dataset.srcLine = r.src_line;
    tr.innerHTML = `<td>${escapeHtml(r.card)}</td><td>${r.time}</td>
      <td>${escapeHtml(r.station)}</td><td>${actionText(r.action)}</td>
      <td class="num">第 ${r.src_line} 行</td>`;
    tb.appendChild(tr);
  }
}

async function saveTextSection(key, path, text, render) {
  try {
    const data = await putText(path, text);
    const fresh = await api(path);
    render(fresh.items);
    toast(`已保存 ${data.count} 条 ${key}`);
  } catch (e) {
    const errs = (e.payload && e.payload.errors) || [];
    if (errs.length) showParseErrors(errs);
    toast(summaryError(e, `保存${key}失败`), false);
  }
}

function showParseErrors(errors) {
  const list = errors.slice(0, 12).map((e) =>
    `第 ${e.location && e.location.line || "?"} 行：${e.message}`).join("\n");
  toast(`录入解析失败：\n${list}`, false);
  // toast 不支持换行样式时，仍可在核验结果区展示
  const ul = $("errors-list");
  ul.innerHTML = "";
  const panel = $("errors-panel");
  for (const e of errors) {
    ul.appendChild(renderErrorLi(e));
  }
  panel.hidden = false;
  $("bills-panel").hidden = true;
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function loadBack(kind) {
  const fresh = await api(`/api/${kind}`);
  let text = "";
  if (kind === "stations") {
    text = fresh.items.map((s) => `${s.name}, ${s.zone}`).join("\n");
    renderStations(fresh.items);
  } else if (kind === "fares") {
    text = fresh.items.map((f) =>
      `${f.from_zone}, ${f.to_zone}, ${yuan(f.price)}`).join("\n");
    renderFares(fresh.items);
  } else if (kind === "records") {
    text = fresh.items.map((r) =>
      `${r.card}, ${r.time}, ${r.station}, ${r.action}`).join("\n");
    renderRecords(fresh.items);
  }
  $(`${kind}-text`).value = text;
  toast("已从数据库回填");
}

/* ---------------- 票价矩阵模板 ---------------- */

function genFareTemplate() {
  const n = Math.max(1, Math.min(20, Number($("fare-zones").value) || 3));
  const lines = [];
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= n; j++) {
      // 模板建议价：同区 2 元，每跨一区 +1 元（用户可自行改价）
      const price = 2 + Math.abs(i - j);
      lines.push(`${i}, ${j}, ${price}.00`);
    }
  }
  $("fares-text").value = lines.join("\n");
  toast(`已生成 ${n}×${n} 对称模板，请核对并保存`);
}

/* ---------------- 5. 核验 ---------------- */

function renderErrorLi(e) {
  const li = document.createElement("li");
  const code = document.createElement("span");
  code.className = "error-code";
  code.textContent = e.code;
  li.appendChild(code);
  li.appendChild(document.createTextNode(e.message));
  if (e.location) {
    const parts = [];
    const loc = e.location;
    if (loc.line !== undefined) parts.push(`文本第 ${loc.line} 行`);
    if (loc.card) parts.push(`卡号 ${loc.card}`);
    if (loc.time) parts.push(`时刻 ${loc.time}`);
    if (loc.station) parts.push(`站点 ${loc.station}`);
    if (parts.length) {
      const span = document.createElement("span");
      span.className = "error-loc";
      span.textContent = "定位：" + parts.join(" · ");
      li.appendChild(span);
    }
  }
  return li;
}

function renderBills(bills) {
  const tb = $("bills-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const b of bills) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(b.card)}</td>
      <td class="num">${b.seq}</td>
      <td>${b.operating_day}</td>
      <td>${b.entry_time}</td>
      <td>${b.exit_time}</td>
      <td>${escapeHtml(b.entry_station)}（${b.from_zone}区）</td>
      <td>${escapeHtml(b.exit_station)}（${b.to_zone}区）</td>
      <td class="num">${yuan(b.base_fare)}</td>
      <td class="num discount">-${yuan(b.discount)}</td>
      <td class="num charge">${yuan(b.charged_fare)}</td>
      <td class="num">${yuan(b.daily_cumulative)} / ${yuan(b.daily_cap)}</td>
      <td class="wrap">${escapeHtml(b.discount_source)}</td>`;
    tb.appendChild(tr);
  }
}

async function runVerify() {
  const status = $("verify-status");
  status.textContent = "核验中…";
  status.className = "status";
  try {
    const data = await api("/api/verify", { method: "POST" });
    renderBills(data.bills || []);
    $("bills-panel").hidden = false;
    $("errors-panel").hidden = true;
    status.textContent = `核验通过，共 ${data.bill_count} 笔行程账单`;
    status.className = "status ok";
    toast(`核验通过：${data.bill_count} 笔行程`);
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    const ul = $("errors-list");
    ul.innerHTML = "";
    for (const err of errors) ul.appendChild(renderErrorLi(err));
    $("errors-panel").hidden = false;
    $("bills-panel").hidden = true;
    status.textContent = `核验未通过：${errors.length} 个错误（未生成账单）`;
    status.className = "status bad";
    toast(`核验未通过：${errors.length} 个错误`, false);
  }
}

/* ---------------- 6. 配对修复预览（只读） ---------------- */

function renderRepairErrors(errors) {
  const ul = $("repair-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("repair-errors").hidden = false;
  $("repair-result").hidden = true;
}

function changeItemHtml(ch) {
  const where = `${escapeHtml(ch.card)} ${ch.time} @ ${escapeHtml(ch.station)}` +
    `<span class="sub">（来源第 ${ch.line} 行）</span>`;
  if (ch.op === "delete") {
    return `<span class="tag tag-del">删除</span>${where}` +
      `<span class="sub">原方向 ${actionText(ch.from_action)}</span>`;
  }
  return `<span class="tag tag-flip">翻转</span>${where}` +
    `<span class="sub">${actionText(ch.from_action)} → ${actionText(ch.to_action)}</span>`;
}

function renderRepairPlans(data) {
  $("repair-errors").hidden = true;
  const panel = $("repair-result");
  panel.hidden = false;

  const summary = $("repair-summary");
  const plansBox = $("repair-plans");
  plansBox.innerHTML = "";

  if (data.min_edits === 0) {
    summary.innerHTML = "✅ 已保存的记录可完整配对，<strong>无需修复</strong>。" +
      "（预览为只读操作，未改动任何数据）";
    return;
  }

  const hiddenNote = data.hidden_plans > 0
    ? `，另有 <strong>${data.hidden_plans}</strong> 个同成本方案未展示` : "";
  summary.innerHTML =
    `最小编辑数 <strong>${data.min_edits}</strong> 次 · ` +
    `共 <strong>${data.total_plans}</strong> 个同成本方案，` +
    `按稳定顺序展示 ${data.shown_plans} 个${hiddenNote}。<br>` +
    `<span class="sub">请人工核对后自行选择：系统不会替您选定方案；` +
    `预览全程只读，未保存记录、未清空账单。</span>`;

  data.plans.forEach((p, idx) => {
    const div = document.createElement("div");
    div.className = "repair-plan";

    const head = document.createElement("div");
    head.className = "repair-plan-head";
    head.innerHTML =
      `<h3>方案 ${idx + 1}</h3>` +
      `<span class="hint">编辑 ${p.edit_count} 次 · 变更 ${p.change_count} 条 · ` +
      `修复后 ${p.kept_count} 条记录 / ${p.trip_count} 笔行程</span>`;
    const btn = document.createElement("button");
    btn.className = "btn btn-ghost";
    btn.type = "button";
    btn.textContent = "📋 复制修复后记录";
    btn.addEventListener("click", () =>
      copyText(p.fixed_text, "已复制修复后的记录文本（仍未保存，可粘贴到记录区核对）"));
    head.appendChild(btn);
    div.appendChild(head);

    const ul = document.createElement("ul");
    ul.className = "change-list";
    for (const ch of p.changes) {
      const li = document.createElement("li");
      li.innerHTML = changeItemHtml(ch);
      ul.appendChild(li);
    }
    div.appendChild(ul);

    const pre = document.createElement("pre");
    pre.className = "fixed-text";
    pre.textContent = p.fixed_text;
    div.appendChild(pre);

    plansBox.appendChild(div);
  });
}

async function copyText(text, message) {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    toast(message || "已复制到剪贴板（仍未保存任何改动）");
  } catch (_) {
    toast("复制失败，请手动选中文本复制", false);
  }
}

async function runRepairPreview() {
  const status = $("repair-status");
  status.textContent = "搜索最小编辑方案…";
  status.className = "status";
  $("repair-errors").hidden = true;
  $("repair-result").hidden = true;
  try {
    const data = await api("/api/repair-preview", { method: "POST" });
    renderRepairPlans(data);
    if (data.min_edits === 0) {
      status.textContent = "记录已可完整配对，无需修复";
    } else {
      status.textContent =
        `已生成 ${data.shown_plans} 个最小编辑方案` +
        (data.hidden_plans > 0 ? `（另有 ${data.hidden_plans} 个未展示）` : "") +
        "，记录与账单均未改动";
    }
    status.className = "status ok";
    toast("修复预览已生成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderRepairErrors(errors);
      status.textContent =
        `存在 ${errors.length} 个非配对错误，未生成修复方案（记录与账单均未改动）`;
    } else {
      status.textContent = summaryError(e, "修复预览请求失败");
    }
    status.className = "status bad";
    toast("无法生成修复预览", false);
  }
}

/* ---------------- 8. 票价矩阵路径审计（只读） ---------------- */

let lastSuggestLines = [];

function renderAuditErrors(errors) {
  const ul = $("audit-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("audit-errors").hidden = false;
  $("audit-result").hidden = true;
}

function affectedCellHtml(list) {
  return list.map((a) =>
    `<div>${escapeHtml(a.card)} 第${a.seq}程` +
    `<span class="sub">（进站第 ${a.entry_line} 行 / 出站第 ${a.exit_line} 行）</span>` +
    `<div class="sub">${a.entry_time} ${escapeHtml(a.entry_station)} → ` +
    `${escapeHtml(a.exit_station)}</div></div>`
  ).join("");
}

function pathCellHtml(item) {
  const legs = (item.legs || []).map((l) =>
    `${l.from_zone}→${l.to_zone} ${yuan(l.price)}`).join(" ＋ ");
  return `<div class="path-text">${escapeHtml(item.path_text)}</div>` +
    (legs ? `<div class="sub">${escapeHtml(legs)}</div>` : "");
}

function renderFareAudit(data) {
  $("audit-errors").hidden = true;
  $("audit-result").hidden = false;

  const miss = data.missing || [];
  const over = data.overpriced || [];
  const unreachable = miss.filter((m) => !m.reachable).length;
  const reachable = miss.length - unreachable;

  const summary = $("audit-summary");
  if (!miss.length && !over.length) {
    summary.innerHTML =
      `✅ 共审计 <strong>${data.trip_count}</strong> 笔行程 / ` +
      `<strong>${data.pair_count}</strong> 个实际行程分区对：均有直达票价，` +
      `且直达票价不高于任何间接路径。<br>` +
      `<span class="sub">审计全程只读：未修改票价矩阵，未写入或清空账单。</span>`;
  } else {
    summary.innerHTML =
      `共审计 <strong>${data.trip_count}</strong> 笔行程 / ` +
      `<strong>${data.pair_count}</strong> 个实际行程分区对：` +
      `缺直达票价 <strong>${miss.length}</strong> 对` +
      `（可达可补录 ${reachable} 对，不可达保留 MISSING_FARE ${unreachable} 对），` +
      `直达票价高于间接路径 <strong>${over.length}</strong> 对。<br>` +
      `<span class="sub">审计全程只读：未修改票价矩阵，未写入或清空账单；` +
      `建议补录行请人工核对后粘贴到「4. 票价矩阵」保存。</span>`;
  }

  // 缺直达票价
  const mBlock = $("audit-missing-block");
  mBlock.hidden = miss.length === 0;
  const mtb = $("audit-missing-table").querySelector("tbody");
  mtb.innerHTML = "";
  lastSuggestLines = [];
  for (const m of miss) {
    const tr = document.createElement("tr");
    let statusHtml, priceHtml, pathHtml, lineHtml;
    if (m.reachable) {
      statusHtml = `<span class="tag tag-ok">可补录</span>`;
      priceHtml = `<span class="charge">${yuan(m.suggested_price)}</span>`;
      pathHtml = pathCellHtml(m);
      lineHtml = `<code>${escapeHtml(m.suggest_line)}</code> `;
      lastSuggestLines.push(m.suggest_line);
    } else {
      statusHtml = `<span class="tag tag-unreachable">不可达</span>` +
        `<div class="sub">保留 MISSING_FARE</div>` +
        (m.errors || []).map((e) =>
          `<div class="sub err-line">${escapeHtml(e.message)}</div>`).join("");
      priceHtml = "—";
      pathHtml = "—";
      lineHtml = "—";
    }
    tr.innerHTML = `
      <td class="num">${m.from_zone} → ${m.to_zone}</td>
      <td>${statusHtml}</td>
      <td class="num">${priceHtml}</td>
      <td class="wrap">${pathHtml}</td>
      <td class="wrap">${affectedCellHtml(m.affected)}</td>
      <td class="suggest-cell">${lineHtml}</td>`;
    if (m.reachable) {
      const btn = document.createElement("button");
      btn.className = "btn btn-ghost btn-mini";
      btn.type = "button";
      btn.textContent = "📋 复制";
      btn.addEventListener("click", () =>
        copyText(m.suggest_line,
          `已复制建议补录行「${m.suggest_line}」（矩阵未改动，粘贴保存后生效）`));
      tr.querySelector(".suggest-cell").appendChild(btn);
    }
    mtb.appendChild(tr);
  }
  $("btn-copy-suggests").hidden = lastSuggestLines.length === 0;

  // 直达票价高于间接路径
  const oBlock = $("audit-overpriced-block");
  oBlock.hidden = over.length === 0;
  const otb = $("audit-overpriced-table").querySelector("tbody");
  otb.innerHTML = "";
  for (const o of over) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="num">${o.from_zone} → ${o.to_zone}</td>
      <td class="num">${yuan(o.direct_price)}</td>
      <td class="num">${yuan(o.best_price)}</td>
      <td class="num delta-up">+${yuan(o.diff)}</td>
      <td class="wrap">${pathCellHtml(o)}</td>
      <td class="wrap">${affectedCellHtml(o.affected)}</td>`;
    otb.appendChild(tr);
  }
}

async function runFareAudit() {
  const status = $("audit-status");
  status.textContent = "审计中…";
  status.className = "status";
  $("audit-errors").hidden = true;
  $("audit-result").hidden = true;
  $("btn-copy-suggests").hidden = true;
  try {
    const data = await api("/api/fare-path-audit", { method: "POST" });
    renderFareAudit(data);
    const miss = data.missing || [];
    const over = data.overpriced || [];
    if (!miss.length && !over.length) {
      status.textContent = `审计通过：${data.pair_count} 个分区对均无异常（只读，未改动数据）`;
    } else {
      status.textContent =
        `审计完成：${miss.length} 对缺直达票价，${over.length} 对直达偏贵` +
        "（只读，矩阵与账单均未改动）";
    }
    status.className = "status ok";
    toast("路径审计完成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderAuditErrors(errors);
      status.textContent =
        `存在 ${errors.length} 个数据错误，未生成审计结果（矩阵与账单均未改动）`;
    } else {
      status.textContent = summaryError(e, "路径审计请求失败");
    }
    status.className = "status bad";
    toast("无法执行路径审计", false);
  }
}

function copyAllSuggests() {
  if (!lastSuggestLines.length) return;
  copyText(lastSuggestLines.join("\n"),
    `已复制 ${lastSuggestLines.length} 行建议补录行（可粘贴到「4. 票价矩阵」，核对后保存）`);
}

/* ---------------- 9. 行程耗时异常审计（只读） ---------------- */

function renderDurationErrors(errors) {
  const ul = $("duration-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("duration-errors").hidden = false;
  $("duration-result").hidden = true;
}

function fmtMin(v) {
  return Number(v).toFixed(2);
}

function renderDurationAudit(data) {
  $("duration-errors").hidden = true;
  $("duration-result").hidden = false;

  const anomalies = data.anomalies || [];
  const insufficient = data.insufficient || [];
  const longCount = anomalies.filter((a) => a.kind === "long").length;
  const shortCount = anomalies.length - longCount;

  const insNote = insufficient.length
    ? `另有 <strong>${insufficient.length}</strong> 个站点对样本不足` +
      `（&lt; ${data.min_samples} 程），未参与判定。`
    : "";
  const summary = $("duration-summary");
  if (!anomalies.length) {
    summary.innerHTML =
      `✅ 共统计 <strong>${data.trip_count}</strong> 笔行程 / ` +
      `<strong>${data.pair_count}</strong> 个有向站点对，其中 ` +
      `<strong>${data.audited_pairs}</strong> 个站点对样本数达标` +
      `（≥ ${data.min_samples} 程）：未发现耗时异常。${insNote}<br>` +
      `<span class="sub">审计全程只读：未修改记录、配置或账单。</span>`;
  } else {
    summary.innerHTML =
      `共统计 <strong>${data.trip_count}</strong> 笔行程 / ` +
      `<strong>${data.pair_count}</strong> 个有向站点对，其中 ` +
      `<strong>${data.audited_pairs}</strong> 个站点对样本数达标` +
      `（≥ ${data.min_samples} 程）：发现耗时异常 ` +
      `<strong>${anomalies.length}</strong> 笔` +
      `（过长 ${longCount} 笔 / 过短 ${shortCount} 笔）。${insNote}<br>` +
      `<span class="sub">阈值 = max(10 分钟, 3 × 1.4826 × MAD)，` +
      `基线已排除行程自身；审计全程只读：未修改记录、配置或账单。</span>`;
  }

  // 耗时异常行程
  const aBlock = $("duration-anomalies-block");
  aBlock.hidden = anomalies.length === 0;
  const atb = $("duration-anomalies-table").querySelector("tbody");
  atb.innerHTML = "";
  for (const a of anomalies) {
    const isLong = a.kind === "long";
    const devText = (a.deviation_min > 0 ? "+" : "") + fmtMin(a.deviation_min);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><span class="tag ${isLong ? "tag-long" : "tag-short"}">${isLong ? "过长" : "过短"}</span></td>
      <td>${escapeHtml(a.card)}</td>
      <td class="num">${a.seq}</td>
      <td>${escapeHtml(a.entry_station)} → ${escapeHtml(a.exit_station)}</td>
      <td>${a.entry_time}</td>
      <td>${a.exit_time}</td>
      <td class="num">${fmtMin(a.duration_min)}</td>
      <td class="num">${a.baseline_count}</td>
      <td class="num">${fmtMin(a.median_min)}</td>
      <td class="num">${fmtMin(a.mad_min)}</td>
      <td class="num">${fmtMin(a.threshold_min)}</td>
      <td class="num ${isLong ? "delta-up" : "delta-down"}">${devText}</td>
      <td class="num">进 ${a.entry_line} / 出 ${a.exit_line}</td>`;
    atb.appendChild(tr);
  }

  // 样本不足的站点对
  const iBlock = $("duration-insufficient-block");
  iBlock.hidden = insufficient.length === 0;
  const itb = $("duration-insufficient-table").querySelector("tbody");
  itb.innerHTML = "";
  for (const p of insufficient) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(p.entry_station)} → ${escapeHtml(p.exit_station)}</td>
      <td class="num">${p.trip_count}</td>
      <td>需 ≥ ${p.required} 程才参与判定，当前 ${p.trip_count} 程</td>`;
    itb.appendChild(tr);
  }
}

async function runDurationAudit() {
  const status = $("duration-status");
  status.textContent = "审计中…";
  status.className = "status";
  $("duration-errors").hidden = true;
  $("duration-result").hidden = true;
  try {
    const data = await api("/api/duration-audit", { method: "POST" });
    renderDurationAudit(data);
    if (!data.anomaly_count) {
      status.textContent =
        `审计完成：${data.audited_pairs} 个达标站点对均无耗时异常（只读，未改动数据）`;
    } else {
      status.textContent =
        `审计完成：标记 ${data.anomaly_count} 笔耗时异常` +
        "（只读，记录 / 配置 / 账单均未改动）";
    }
    status.className = "status ok";
    toast("耗时审计完成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderDurationErrors(errors);
      status.textContent =
        `存在 ${errors.length} 个配对 / 时间类错误，未生成审计结果（数据均未改动）`;
    } else {
      status.textContent = summaryError(e, "耗时审计请求失败");
    }
    status.className = "status bad";
    toast("无法执行耗时审计", false);
  }
}

/* ---------------- 10. 站点闸机峰值审计（只读） ---------------- */

function renderPeakErrors(errors) {
  const ul = $("peak-errors-list");
  ul.innerHTML = "";
  for (const e of errors) ul.appendChild(renderErrorLi(e));
  $("peak-errors").hidden = false;
  $("peak-result").hidden = true;
}

function peakLineChips(lines) {
  return lines.map((ln) =>
    `<button type="button" class="line-chip" data-line="${ln}">第 ${ln} 行</button>`
  ).join(" ");
}

function renderPeakAudit(data) {
  $("peak-errors").hidden = true;
  $("peak-result").hidden = false;

  const groups = data.groups || [];
  const summary = $("peak-summary");
  if (!groups.length) {
    summary.innerHTML =
      `已保存记录数 <strong>${data.record_count}</strong>，没有可统计的站点 / 方向分组。<br>` +
      `<span class="sub">审计全程只读：未修改记录、配置或账单。</span>`;
  } else {
    const alertNote = data.alert_count
      ? `其中 <strong>${data.alert_count}</strong> 组达到最低告警数 ` +
        `（≥ ${data.min_alert} 笔），已用 🚨 标记。`
      : `没有组达到最低告警数（峰值 ≥ ${data.min_alert} 笔才标记）。`;
    summary.innerHTML =
      `统计窗口 <strong>${data.window_minutes}</strong> 分钟 · ` +
      `已保存刷卡 <strong>${data.record_count}</strong> 条 · ` +
      `<strong>${data.group_count}</strong> 个「站点 × 方向」组，` +
      `${alertNote}<br>` +
      `<span class="sub">候选窗口只从实际刷卡时刻起算，采用半开区间 [开始, 结束)；` +
      `并列时按更早开始时刻、站点名排序。审计全程只读：未修改记录、配置或账单。` +
      `点击行号可定位到「5. 刷卡记录」中的原始行。</span>`;
  }

  const tb = $("peak-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const g of groups) {
    const tr = document.createElement("tr");
    if (g.alert) tr.classList.add("peak-alert-row");
    tr.innerHTML = `
      <td>${g.alert ? '<span class="tag tag-alert">🚨 告警</span>' : "—"}</td>
      <td>${escapeHtml(g.station)}</td>
      <td><span class="tag ${g.action === "IN" ? "tag-in" : "tag-out"}">` +
        `${g.action === "IN" ? "进 ▲" : "出 ▼"}</span></td>
      <td class="num charge">${g.peak_count}</td>
      <td class="num">${g.card_count}</td>
      <td>${g.start_time}</td>
      <td>${g.end_time}</td>
      <td class="num">${g.total_count}</td>
      <td class="line-chip-cell">${peakLineChips(g.lines)}</td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll(".line-chip").forEach((chip) => {
    chip.addEventListener("click", () =>
      locateRecordLine(Number(chip.dataset.line)));
  });
}

async function locateRecordLine(line) {
  // 以已保存记录为准（忽略文本框中未保存的改动），在「5. 刷卡记录」
  // 结果表中按原始行号 src_line 定位对应行并高亮。
  const tb = $("records-table").querySelector("tbody");
  let row = tb.querySelector(`tr[data-src-line="${line}"]`);
  if (!row) {
    // 页面表格可能不是最新的已保存数据，重新拉取后再定位
    try {
      const fresh = await api("/api/records");
      renderRecords(fresh.items);
      row = tb.querySelector(`tr[data-src-line="${line}"]`);
    } catch (e) {
      toast(summaryError(e, "定位原始行失败"), false);
      return;
    }
  }
  if (!row) {
    toast(`已保存记录中找不到第 ${line} 行（记录可能已被重新保存）`, false);
    return;
  }
  tb.querySelectorAll("tr.loc-hit").forEach((tr) =>
    tr.classList.remove("loc-hit"));
  row.classList.add("loc-hit");
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  toast(`已定位到已保存刷卡记录的第 ${line} 行（只读定位，未修改数据）`);
}

async function runPeakAudit() {
  const status = $("peak-status");
  const body = {
    window_minutes: Number($("peak-window").value),
    min_alert: Number($("peak-min-alert").value),
  };
  if (!Number.isFinite(body.window_minutes) || !Number.isFinite(body.min_alert)) {
    status.textContent = "统计窗口与最低告警数必须填写为有效数字";
    status.className = "status bad";
    return;
  }
  status.textContent = "审计中…";
  status.className = "status";
  $("peak-errors").hidden = true;
  $("peak-result").hidden = true;
  try {
    const data = await api("/api/station-peak-audit", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderPeakAudit(data);
    status.textContent =
      `审计完成：${data.group_count} 个站点 / 方向组，${data.alert_count} 组达标` +
      "（只读，记录 / 配置 / 账单均未改动）";
    status.className = "status ok";
    toast("峰值审计完成（只读，未保存任何改动）");
  } catch (e) {
    const errors = (e.payload && e.payload.errors) || [];
    if (errors.length) {
      renderPeakErrors(errors);
      status.textContent =
        `参数不合法，未生成审计结果（数据均未改动）`;
    } else {
      status.textContent = summaryError(e, "峰值审计请求失败");
    }
    status.className = "status bad";
    toast("无法执行峰值审计", false);
  }
}

/* ---------------- 演示数据 ---------------- */

const DEMO = {
  config: { day_split_hour: 4, transfer_minutes: 90,
            transfer_discount: 1, daily_cap: 10 },
  stations: `科技园, 1
中心公园, 2
大学城, 3`,
  fares: `1, 1, 2.00
1, 2, 3.00
1, 3, 4.00
2, 1, 3.00
2, 2, 2.00
2, 3, 3.00
3, 1, 4.00
3, 2, 3.00
3, 3, 2.00`,
  records: `A001, 2026-09-18 08:00:00, 科技园, 进
A001, 2026-09-18 08:25:00, 中心公园, 出
A001, 2026-09-18 09:10:00, 中心公园, 进
A001, 2026-09-18 09:50:00, 大学城, 出
A001, 2026-09-18 17:30:00, 大学城, 进
A001, 2026-09-18 18:20:00, 科技园, 出
A001, 2026-09-18 19:30:00, 科技园, 进
A001, 2026-09-18 20:00:00, 中心公园, 出
B002, 2026-09-19 03:20:00, 科技园, 进
B002, 2026-09-19 03:45:00, 中心公园, 出
# —— 以下用于「10. 站点闸机峰值审计」演示跨整点早高峰（科技园进站）——
C003, 2026-09-18 07:56:00, 科技园, 进
D004, 2026-09-18 07:58:00, 科技园, 进
E005, 2026-09-18 08:02:00, 科技园, 进
C003, 2026-09-18 08:05:00, 大学城, 出
D004, 2026-09-18 08:09:00, 中心公园, 出
E005, 2026-09-18 08:12:00, 中心公园, 出
F006, 2026-09-18 08:04:00, 科技园, 进
F006, 2026-09-18 08:30:00, 中心公园, 出
G007, 2026-09-18 08:07:00, 科技园, 进
G007, 2026-09-18 08:40:00, 大学城, 出`,
};

async function loadDemo() {
  if (!confirm("将用演示数据覆盖当前参数 / 站点 / 票价 / 记录，是否继续？")) return;
  $("cfg-split").value = DEMO.config.day_split_hour;
  $("cfg-minutes").value = DEMO.config.transfer_minutes;
  $("cfg-discount").value = DEMO.config.transfer_discount;
  $("cfg-cap").value = DEMO.config.daily_cap;
  $("stations-text").value = DEMO.stations;
  $("fares-text").value = DEMO.fares;
  $("records-text").value = DEMO.records;

  await saveConfig();
  await putText("/api/stations", DEMO.stations);
  await putText("/api/fares", DEMO.fares);
  await putText("/api/records", DEMO.records);

  renderStations((await api("/api/stations")).items);
  renderFares((await api("/api/fares")).items);
  renderRecords((await api("/api/records")).items);
  toast("演示数据载入完成，开始核验…");
  await runVerify();
}

/* ---------------- 工具 ---------------- */

function summaryError(e, fallback) {
  const errs = e.payload && e.payload.errors;
  if (errs && errs.length) {
    const first = errs[0];
    const line = first.location && first.location.line;
    return `${fallback}：${line ? "第 " + line + " 行 " : ""}${first.message}`;
  }
  if (e.payload && e.payload.detail) return e.payload.detail;
  return fallback;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------------- 初始化 ---------------- */

function bind() {
  $("btn-save-config").addEventListener("click", saveConfig);

  $("btn-run-trial").addEventListener("click", runTrialCompare);
  $("btn-trial-copy").addEventListener("click", copyCurrentToTrial);

  $("btn-save-stations").addEventListener("click", () =>
    saveTextSection("站点", "/api/stations", $("stations-text").value, renderStations));
  $("btn-save-fares").addEventListener("click", () =>
    saveTextSection("票价", "/api/fares", $("fares-text").value, renderFares));
  $("btn-save-records").addEventListener("click", () =>
    saveTextSection("记录", "/api/records", $("records-text").value, renderRecords));

  $("btn-load-stations").addEventListener("click", () => loadBack("stations"));
  $("btn-load-fares").addEventListener("click", () => loadBack("fares"));
  $("btn-load-records").addEventListener("click", () => loadBack("records"));

  $("btn-gen-fares").addEventListener("click", genFareTemplate);
  $("btn-repair").addEventListener("click", runRepairPreview);
  $("btn-fare-audit").addEventListener("click", runFareAudit);
  $("btn-duration-audit").addEventListener("click", runDurationAudit);
  $("btn-peak-audit").addEventListener("click", runPeakAudit);
  $("btn-copy-suggests").addEventListener("click", copyAllSuggests);
  $("btn-verify").addEventListener("click", runVerify);
  $("btn-verify-top").addEventListener("click", runVerify);
  $("btn-demo").addEventListener("click", loadDemo);
}

async function init() {
  bind();
  try {
    const cfg = await loadConfig();
    fillTrialInputs(cfg);
    renderStations((await api("/api/stations")).items);
    renderFares((await api("/api/fares")).items);
    renderRecords((await api("/api/records")).items);
    const bills = await api("/api/bills");
    if (bills.count > 0) {
      renderBills(bills.items);
      $("bills-panel").hidden = false;
      $("verify-status").textContent =
        `已加载上次核验结果（${bills.count} 笔，${bills.last_verified_at || ""}），修改数据后请重新核验`;
      $("verify-status").className = "status";
    }
  } catch (e) {
    toast("初始化加载数据失败：" + e.message, false);
  }
}

document.addEventListener("DOMContentLoaded", init);
