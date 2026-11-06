/* 医智呼 · 医生工作台（原生 JS，无构建步骤，便于离线答辩与一键发布） */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const RISK = { blue: "蓝色·平稳", yellow: "黄色·轻度", orange: "橙色·中度", red: "红色·紧急" };
const STATUS = { queued: "待外呼", calling: "通话中", completed: "已完成", no_answer: "未接通", failed: "失败", terminated: "已终止" };
const PERSONAS = ["稳定", "漏服", "血糖偏高", "足部麻木", "自行停药", "低血糖", "足部破溃", "酮症", "家属代接", "转院", "拒接"];

const state = { page: "dashboard", overview: null, protocols: [], patients: { page: 1 }, callTab: "queued" };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...opts,
    body: opts.body && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

const UI = {
  toast(msg, ms = 2600) {
    const t = $("#toast"); t.textContent = msg; t.classList.add("on");
    clearTimeout(UI._t); UI._t = setTimeout(() => t.classList.remove("on"), ms);
  },
  open(title, body, foot = "", tag = "") {
    $("#drawerTitle").textContent = title;
    $("#drawerTag").innerHTML = tag;
    $("#drawerBody").innerHTML = body;
    $("#drawerFoot").innerHTML = foot;
    $("#mask").classList.add("on"); $("#drawer").classList.add("on");
  },
  closeAll() { $("#mask").classList.remove("on"); $("#drawer").classList.remove("on"); },
  loading(text = "加载中") { return `<div class="loading-line"><span class="spin"></span>${text}</div>`; },
};

/* ------------------------------------------------------------------ 路由 */
const PAGES = {
  dashboard: ["工作台", "出院患者 AI 分阶段随访全景"],
  protocols: ["病种随访方案", "医生编排病种、阶段与提问，AI 按方案自动外呼"],
  patients: ["患者档案", "出院患者名单、随访计划与通话记录"],
  calls: ["外呼中心", "任务队列、通话详情、语音转写与结构化结果"],
  tickets: ["风险工单", "异常随访分级处置与闭环"],
  cloud: ["云服务接入", "医生身份、随访数据、资料文件与免密钥大模型"],
  settings: ["系统设置", "线路、白名单、业务日期与模型状态"],
};

function route() {
  const page = (location.hash.replace("#/", "") || "dashboard").split("?")[0];
  state.page = PAGES[page] ? page : "dashboard";
  $$("#nav a").forEach(a => a.classList.toggle("on", a.dataset.page === state.page));
  $("#pageTitle").textContent = PAGES[state.page][0];
  $("#pageSub").textContent = PAGES[state.page][1];
  // 关闭抽屉并滚动到顶
  UI.closeAll(); window.scrollTo({ top: 0 });
  api("/api/settings").then(s => {
    $("#chipDate").textContent = "业务日 " + s.business_date;
    const line = s.line || {};
    $("#chipLine").textContent = line.mode === "real" ? "真实外呼 · " + (line.name || "") : "演练模式 · " + (line.name || "");
  }).catch(() => {});
  const render = {
    dashboard: renderDashboard, protocols: renderProtocols, patients: renderPatients,
    calls: renderCalls, tickets: renderTickets, cloud: renderCloud, settings: renderSettings,
  }[state.page];
  $("#view").innerHTML = `<div class="card"><div class="card-b">${UI.loading()}</div></div>`;
  render().catch(e => { $("#view").innerHTML = `<div class="card"><div class="card-b empty">${esc(e.message)}</div></div>`; });
}

/* ------------------------------------------------------------------ 工作台 */
async function renderDashboard() {
  const d = await api("/api/overview");
  state.overview = d;
  $("#chipDate").textContent = "业务日 " + d.business_date;
  const line = d.line;
  $("#chipLine").textContent = line ? (line.mode === "real" ? "真实外呼 · " + line.name : "演练模式 · " + line.name) : "未配置线路";
  $("#dotLine").className = "dot " + (line && line.enabled ? (line.mode === "real" ? "ok" : "warn") : "off");
  $("#lineText").textContent = line ? (line.mode === "real" ? "阿里云线路已连接" : "阿里云线路（演练）") : "未配置线路";
  $("#dotLlm").className = "dot " + (d.llm ? "ok" : "warn");
  $("#llmText").textContent = d.llm ? "大模型已就绪" : "大模型未配置";
  if (d.kpi.pending_tickets) { $("#navTicket").style.display = ""; $("#navTicket").textContent = d.kpi.pending_tickets; }

  const max = Math.max(1, ...d.trend.map(x => x.value));
  const riskTotal = Math.max(1, d.risk.reduce((a, b) => a + b.value, 0));
  const colors = { red: "#e0483c", orange: "#e8871a", yellow: "#d9a800", blue: "#3b7ddd" };

  $("#view").innerHTML = `
  <div class="kpis">
    <div class="kpi blue"><div class="t">在访患者</div><div class="v">${d.kpi.in_followup}</div>
      <div class="d">累计建档 ${d.kpi.patients} 人 · 执行中计划 ${d.kpi.active_plans}</div>
      <div class="spark">☰</div></div>
    <div class="kpi teal"><div class="t">今日待外呼</div><div class="v">${d.kpi.due_today}</div>
      <div class="d">今日已完成 ${d.kpi.done_today} 通 · 完成率 ${d.kpi.completion}%</div>
      <div class="spark">☎</div></div>
    <div class="kpi orange"><div class="t">待处理工单</div><div class="v">${d.kpi.pending_tickets}</div>
      <div class="d">橙色及以上需 24 小时内处置</div><div class="spark">⚑</div></div>
    <div class="kpi red"><div class="t">红色预警累计</div><div class="v">${(d.risk.find(r => r.key === "red") || {}).value || 0}</div>
      <div class="d">需 12 小时内联系患者与家属</div><div class="spark">!</div></div>
  </div>

  <div class="dash-grid">
    <div class="grid" style="gap:16px">
      <div class="card">
        <div class="card-h"><h3>近 7 日外呼量</h3><span class="hint">按接通任务统计</span></div>
        <div class="card-b">
          <div class="trend">
            ${d.trend.map((x, i) => `<div class="bar-wrap">
              <div class="bar ${i === d.trend.length - 1 ? "" : "gray"}" style="height:${Math.max(6, x.value / max * 118)}px"></div>
              <div class="lb">${x.date}</div></div>`).join("")}
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-h"><h3>今日随访队列</h3>
          <button class="btn sm primary" onclick="batchCall()">一键批量外呼</button></div>
        <div class="card-b" id="dashQueue">${UI.loading("读取队列")}</div>
      </div>
    </div>

    <div class="grid" style="gap:16px">
      <div class="card">
        <div class="card-h"><h3>风险分级分布</h3><span class="hint">累计 ${riskTotal} 份随访结果</span></div>
        <div class="card-b risk-row">
          ${d.risk.map(r => `<div class="risk-item"><span class="nm">${RISK[r.key]}</span>
            <span class="track"><span class="fill" style="width:${r.value / riskTotal * 100}%;background:${colors[r.key]}"></span></span>
            <span class="num">${r.value}</span></div>`).join("")}
        </div>
      </div>
      <div class="card">
        <div class="card-h"><h3>最近通话</h3><a class="hint link" href="#/calls">全部</a></div>
        <div class="card-b call-list">
          ${d.recent_calls.length ? d.recent_calls.map(c => `
            <div class="call-item" onclick="openCall('${c.id}')" style="cursor:pointer">
              <div class="who">${esc(c.patient_name)}</div>
              <div style="flex:1;min-width:0">
                <div class="sm" style="color:var(--ink-2)">${esc(c.stage)} · ${c.status === "completed" ? c.duration + "s" : STATUS[c.status]}</div>
                <div class="sm">${esc(c.summary || "—")}</div>
              </div>
              ${c.risk ? `<span class="pill ${c.risk}">${RISK[c.risk].split("·")[1]}</span>` : `<span class="pill gray">未接通</span>`}
            </div>`).join("") : `<div class="empty">暂无通话</div>`}
        </div>
      </div>
      <div class="card">
        <div class="card-h"><h3>待办工单</h3><a class="hint link" href="#/tickets">处理</a></div>
        <div class="card-b">
          ${d.tickets.length ? d.tickets.map(t => `
            <div class="ticket-mini ${t.level}">
              <div class="tt">${esc(t.patient_name)} · ${RISK[t.level]} · ${t.sla_hours}h 时限</div>
              <div class="dd">${esc(t.reason).slice(0, 60)}</div>
            </div>`).join("") : `<div class="empty">暂无待办</div>`}
        </div>
      </div>
    </div>
  </div>`;

  loadQueue();
}

async function loadQueue() {
  const d = await api("/api/calls?status=queued");
  const items = d.items.slice(0, 6);
  $("#dashQueue").innerHTML = items.length ? `
    <table><thead><tr><th>患者</th><th>阶段</th><th>计划日期</th><th></th></tr></thead><tbody>
    ${items.map(t => `<tr>
      <td><b>${esc(t.patient_name)}</b><div class="tiny muted">${esc(t.phone)}</div></td>
      <td>${esc(t.stage)}</td>
      <td class="muted tiny">${(t.scheduled_at || "").slice(0, 10)}</td>
      <td style="text-align:right"><button class="btn sm primary" onclick="runCall('${t.id}')">外呼</button></td>
    </tr>`).join("")}</tbody></table>` : `<div class="empty">今日队列已清空</div>`;
}

/* ------------------------------------------------------------------ 方案 */
async function renderProtocols() {
  const rows = await api("/api/protocols");
  state.protocols = rows;
  $("#view").innerHTML = `
    <div class="row" style="margin-bottom:14px">
      <span class="chip soft-blue">${rows.length} 个病种方案</span>
      <span class="chip">医生审核后发布才可对外呼使用</span>
    </div>
    <div class="proto-grid">
      ${rows.map(p => `<div class="proto" onclick="openProtocol('${p.id}')">
        <div class="top">
          <div><h3>${esc(p.name)}</h3><div class="meta">${esc(p.dept)} · 版本 v${p.version} · ${p.stage_count} 个随访节点</div></div>
          <span class="pill ${p.status === "published" ? "green" : p.status === "reviewing" ? "orange" : "gray"}">
            ${p.status === "published" ? "已发布" : p.status === "reviewing" ? "待审核" : "草稿"}</span>
        </div>
        <div class="stages">${(p.stages || []).map(s => `<span class="stage-chip"><b>${esc(s.label)}</b></span>`).join("")}</div>
        <div class="foot"><span>审核人：${esc(p.reviewer || "—")}</span><span class="spacer"></span><span>${esc(p.source)}</span></div>
      </div>`).join("")}
    </div>`;
}

async function openProtocol(id) {
  const p = await api(`/api/protocols/${id}`);
  state.curProto = JSON.parse(JSON.stringify(p));
  state.curStage = (p.stages[0] || {}).key;
  UI.open(p.name, protoBody(), protoFoot(p), `<span class="pill ${p.status === "published" ? "green" : "orange"}">${p.status === "published" ? "已发布" : "待审核"} · v${p.version}</span>`);
}

function protoBody() {
  const p = state.curProto;
  return `
    <div class="note-box">${esc(p.note || "暂无说明")}<br/>
      <span class="muted">红旗症状：</span>${(p.red_flags || []).map(f => `<span class="pill red" style="margin:3px 4px 0 0">${esc(f)}</span>`).join("")}
    </div>
    <div class="section-t" style="margin-top:20px">随访节点</div>
    <div class="stage-editor">
      ${p.stages.map(s => `<div class="stage-tab ${s.key === state.curStage ? "on" : ""}" onclick="switchStage('${s.key}')">
        ${esc(s.label)} · D${s.day} · 高危 D${s.high_risk_day}</div>`).join("")}
    </div>
    <div id="stagePanel">${stagePanel()}</div>`;
}

function stagePanel() {
  const p = state.curProto;
  const s = p.stages.find(x => x.key === state.curStage);
  if (!s) return "";
  return `
    <div class="card" style="box-shadow:none">
      <div class="card-h"><h3>${esc(s.label)} 提问脚本</h3>
        <div class="row">
          <span class="hint">关注：${(s.focus || []).join(" / ")}</span>
          <button class="btn sm" onclick="aiGen('${s.key}')">AI 重写话术</button>
          <button class="btn sm" onclick="addQuestion()">＋ 新增问题</button>
        </div>
      </div>
      <div class="card-b">
        <div class="q-list">
          ${(s.questions || []).map((q, i) => `<div class="q-item">
            <span class="q-del" onclick="delQuestion(${i})">✕</span>
            <input value="${esc(q.text)}" oninput="state.curProto.stages.find(x=>x.key==='${s.key}').questions[${i}].text=this.value"/>
            <div class="q-m"><span class="pill gray">${esc(q.purpose || "未分类")}</span><span>${esc(q.branch || "")}</span></div>
          </div>`).join("") || `<div class="empty">该阶段暂无提问</div>`}
        </div>
      </div>
    </div>`;
}

function switchStage(key) {
  state.curStage = key;
  $$(".stage-tab").forEach(el => el.classList.toggle("on", el.textContent.includes(state.curProto.stages.find(s => s.key === key).label)));
  $("#stagePanel").innerHTML = stagePanel();
}

function protoFoot(p) {
  return `<button class="btn" onclick="UI.closeAll()">关闭</button>
    <button class="btn" onclick="saveProtocol('${p.id}')">保存并提交审核</button>
    <button class="btn primary" onclick="publishProtocol('${p.id}')">审核通过并发布</button>`;
}

function addQuestion() {
  const s = state.curProto.stages.find(x => x.key === state.curStage);
  s.questions.push({ text: "您好，", purpose: "自定义", branch: "" });
  $("#stagePanel").innerHTML = stagePanel();
}
function delQuestion(i) {
  const s = state.curProto.stages.find(x => x.key === state.curStage);
  s.questions.splice(i, 1);
  $("#stagePanel").innerHTML = stagePanel();
}

async function aiGen(stageKey) {
  const p = state.curProto;
  try {
    UI.toast("大模型正在按病种与出院天数生成话术…");
    const d = await api(`/api/protocols/${p.id}/generate`, { method: "POST", body: { stage_key: stageKey } });
    const s = p.stages.find(x => x.key === stageKey);
    s.questions = d.questions;
    p.status = "reviewing";
    $("#stagePanel").innerHTML = stagePanel();
    UI.toast("已生成 " + d.questions.length + " 条提问，请医生逐条审核");
  } catch (e) { UI.toast(e.message); }
}

async function saveProtocol(id) {
  try {
    const d = await api(`/api/protocols/${id}`, { method: "PATCH", body: { stages: state.curProto.stages } });
    state.curProto = d;
    UI.toast("已保存，状态：待审核");
    renderProtocols();
  } catch (e) { UI.toast(e.message); }
}

async function publishProtocol(id) {
  try {
    await api(`/api/protocols/${id}/publish`, { method: "POST", body: { reviewer: "李医师" } });
    UI.closeAll(); UI.toast("方案已发布，可用于生成随访计划"); renderProtocols();
  } catch (e) { UI.toast(e.message); }
}

/* ------------------------------------------------------------------ 患者 */
// 状态 → 轻底语义标签；只映射展示色，不改变状态取值与判断
function statusPill(s) { return s === "在访" ? "teal" : s === "转院" ? "gray" : "yellow"; }
// 出院日期 → 星期，仅用于时间轴标注；非法日期返回空串
function weekOf(dt) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dt || "")) return "";
  const d = new Date(dt + "T00:00:00");
  return isNaN(d.getTime()) ? "" : "周" + "日一二三四五六"[d.getDay()];
}

async function renderPatients(page = 1) {
  state.patients.page = page;
  const q = state.patients.q || "", dept = state.patients.dept || "", status = state.patients.status || "";
  const qs = `q=${encodeURIComponent(q)}&dept=${encodeURIComponent(dept)}&status=${encodeURIComponent(status)}`;
  // 左侧时间轴按出院日期分组；右侧名单与分页沿用原 12 条/页
  const [d, all] = await Promise.all([
    api(`/api/patients?page=${page}&size=12&${qs}`),
    api(`/api/patients?page=1&size=200&${qs}`),
  ]);
  state.patients.all = all.items || [];

  // 仅按出院日期归组，不按科室切列
  const byDate = {}, dateCount = {};
  state.patients.all.forEach(p => {
    const dt = p.discharge_date || "未记录";
    dateCount[dt] = (dateCount[dt] || 0) + 1;
    (byDate[dt] = byDate[dt] || []).push(p);
  });
  const dates = Object.keys(byDate).sort().reverse();

  const timeline = dates.length ? dates.map(dt => `
    <section class="tl-group">
      <div class="tl-date">
        <b>${esc(dt)}</b>
        <span>${weekOf(dt) ? weekOf(dt) + " · " : ""}${dateCount[dt]} 人</span>
      </div>
      <div class="tl-list">
        ${byDate[dt].map(p => `
          <div class="tl-card" onclick="openPatient('${p.id}')" onmouseenter="hoverPatient(event,'${p.id}')" onmouseleave="hidePatientCard()">
            <div class="tl-top"><b>${esc(p.name)}</b><span class="pill ${statusPill(p.status)}">${esc(p.status)}</span></div>
            <div class="tl-dg">${esc(p.diagnosis || "—")}</div>
            <div class="tl-meta">${esc(p.dept || "未分科室")}${p.risk_group ? " · " + esc(p.risk_group) : ""}</div>
          </div>`).join("")}
      </div>
    </section>`).join("") : `<div class="empty">没有符合条件的患者</div>`;

  $("#view").innerHTML = `
    <div class="filters">
      <input placeholder="搜索姓名 / 住院号 / 诊断 / 电话" value="${esc(q)}" onkeydown="if(event.key==='Enter'){state.patients.q=this.value;renderPatients(1)}"/>
      <select onchange="state.patients.dept=this.value;renderPatients(1)">
        <option value="">全部科室</option>
        ${d.depts.map(x => `<option ${x === dept ? "selected" : ""}>${esc(x)}</option>`).join("")}
      </select>
      <select onchange="state.patients.status=this.value;renderPatients(1)">
        <option value="">全部状态</option>
        ${["在访", "待建档", "转院", "已终止", "失联"].map(x => `<option ${x === status ? "selected" : ""}>${x}</option>`).join("")}
      </select>
      <span class="spacer"></span>
      <button class="btn" onclick="importDialog()">导入出院名单</button>
      <button class="btn primary" onclick="newPatient()">＋ 新建患者</button>
    </div>
    <div class="patients-grid">
      <div class="card patients-timeline">
        <div class="card-h"><h3>出院时间轴</h3>
          <span class="hint">按出院日期分组 · 共 ${state.patients.all.length} 人 · ${dates.length} 个出院日</span></div>
        <div class="tl-wrap">${timeline}</div>
      </div>
      <aside class="patients-side">
        <div class="card">
          <div class="card-h"><h3>患者名单</h3><span class="hint">共 ${d.total} 人</span></div>
          <div class="side-list">
            ${d.items.length ? d.items.map(p => `
              <div class="side-item">
                <div class="side-av">${esc((p.name || "?").slice(0, 1))}</div>
                <div class="side-bd">
                  <div class="side-nm"><b class="link" onclick="openPatient('${p.id}')">${esc(p.name)}</b>
                    <span class="side-meta">${esc(p.sex)} · ${p.age || "—"} 岁</span></div>
                  <div class="side-sub">${esc(p.dept)} · ${esc((p.diagnosis || "—").slice(0, 16))}</div>
                  <div class="side-st"><span class="pill ${statusPill(p.status)}">${esc(p.status)}</span>
                    <span class="side-plan ${p.plan_id ? "" : "muted"}">${p.plan_id ? "计划已生成" : "待生成计划"}</span></div>
                </div>
                ${p.plan_id ? `<button class="btn sm" onclick="openPlan('${p.plan_id}')">计划</button>`
                  : `<button class="btn sm primary" onclick="makePlan('${p.id}')">生成计划</button>`}
              </div>`).join("") : `<div class="empty">没有符合条件的患者</div>`}
          </div>
          <div class="pager">共 ${d.total} 人
            <button class="btn sm" ${page <= 1 ? "disabled" : ""} onclick="renderPatients(${page - 1})">上一页</button>
            <span>${page}</span>
            <button class="btn sm" ${page * 12 >= d.total ? "disabled" : ""} onclick="renderPatients(${page + 1})">下一页</button>
          </div>
        </div>
      </aside>
    </div>`;
}

/* 悬停档案浮层：只读取列表里已有的字段，不额外请求接口 */
function hoverPatient(e, id) {
  const p = (state.patients.all || []).find(x => x.id === id);
  if (!p) return;
  let el = $("#hoverCard");
  if (!el) {
    el = document.createElement("div");
    el.id = "hoverCard";
    el.className = "hover-card";
    document.body.appendChild(el);
  }
  el.innerHTML = `
    <div class="hc-h"><b>${esc(p.name)}</b><span class="pill ${statusPill(p.status)}">${esc(p.status)}</span></div>
    <div class="hc-sub">${esc(p.sex)} · ${p.age || "—"} 岁 · 住院号 ${esc(p.inpatient_no)}</div>
    <div class="hc-grid">
      <span>诊断</span><em>${esc(p.diagnosis || "—")}</em>
      <span>科室</span><em>${esc(p.dept || "—")}</em>
      <span>出院日期</span><em>${esc(p.discharge_date || "—")}</em>
      <span>随访方案</span><em>${esc(p.protocol_name || "未生成计划")}</em>
      <span>风险分组</span><em>${esc(p.risk_group || "—")}</em>
      <span>出院医嘱</span><em>${esc(p.review_advice || p.discharge_advice || "—")}</em>
    </div>
    <div class="hc-foot">点击查看完整档案与通话记录</div>`;
  el.classList.add("on");
  const pad = 14, r = el.getBoundingClientRect();
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + r.width > innerWidth - 8) x = e.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = e.clientY - r.height - pad;
  el.style.left = Math.max(8, x) + "px";
  el.style.top = Math.max(8, y) + "px";
}
function hidePatientCard() {
  const el = $("#hoverCard");
  if (el) el.classList.remove("on");
}

async function openPatient(id) {
  const p = await api(`/api/patients/${id}`);
  const plan = p.plans[0];
  UI.open(p.name + " · 患者档案", `
    <div class="kv">
      <div class="k">基本信息</div><div>${esc(p.sex)} · ${p.age || "—"} 岁 · 住院号 ${esc(p.inpatient_no)}</div>
      <div class="k">联系电话</div><div>${esc(p.phone)}</div>
      <div class="k">科室</div><div>${esc(p.dept)}</div>
      <div class="k">诊断</div><div>${esc(p.diagnosis)}</div>
      <div class="k">手术</div><div>${esc(p.surgery || "—")}</div>
      <div class="k">出院日期</div><div>${p.discharge_date || "—"}　<span class="pill ${p.status === "在访" ? "teal" : "gray"}">${p.status}</span></div>
      <div class="k">用药方案</div><div class="tiny">${esc(p.medications || "—")}</div>
      <div class="k">出院医嘱</div><div class="tiny">${esc(p.discharge_advice || "—")}</div>
      <div class="k">复诊建议</div><div class="tiny">${esc(p.review_advice || "—")}</div>
    </div>
    <div class="section-t">随访计划</div>
    ${plan ? `<div class="row" style="margin-bottom:8px">
        <span class="pill ${plan.status === "active" ? "teal" : plan.status === "terminated" ? "gray" : "green"}">${plan.status === "active" ? "执行中" : plan.status === "terminated" ? "已终止" : "已完成"}</span>
        <span class="lock">计划已固化 v${plan.protocol_version || "—"}</span>
        <span class="tiny muted">${esc(plan.protocol_name)}</span></div>
      ${timeline(plan.stages)}
      <div class="note-box" style="margin-top:12px">${(plan.events || []).map(e => `· ${e.at.slice(0, 16).replace("T", " ")} ${esc(e.text)}`).join("<br/>")}</div>`
      : `<div class="empty">尚未生成随访计划</div>`}
    <div class="section-t">通话记录</div>
    ${p.calls.length ? p.calls.map(c => `<div class="call-item" style="cursor:pointer" onclick="openCall('${c.id}')">
      <div style="width:96px" class="tiny muted">${(c.started_at || c.scheduled_at || "").slice(0, 16).replace("T", " ")}</div>
      <div style="flex:1"><div class="tiny">${esc(c.stage)} · ${STATUS[c.status]}</div>
        <div class="tiny muted">${esc(c.summary || "")}</div></div>
      ${c.risk ? `<span class="pill ${c.risk}">${RISK[c.risk].split("·")[1]}</span>` : ""}
    </div>`).join("") : `<div class="empty">暂无通话</div>`}
  `, `
    <button class="btn" onclick="readmission('${p.id}')">登记再次入院</button>
    ${plan && plan.status === "active" ? `<button class="btn danger" onclick="terminatePlan('${plan.id}')">终止随访</button>` : ""}
    ${!plan ? `<button class="btn primary" onclick="makePlan('${p.id}')">生成随访计划</button>` : ""}
  `);
}

function timeline(stages) {
  return `<div class="timeline">${stages.map(s => `
    <div class="tl-item ${s.status === "done" ? "done" : s.status === "terminated" ? "terminated" : s.due ? "due" : ""}">
      <div class="tl-head">${esc(s.label)}
        <span class="pill ${s.status === "done" ? "green" : s.status === "terminated" ? "gray" : s.due ? "blue" : "gray"}">
          ${s.status === "done" ? "已完成" : s.status === "terminated" ? "已终止" : s.due ? "待外呼" : "待执行"}</span>
        ${s.locked ? `<span class="lock">已固化</span>` : ""}
      </div>
      <div class="tl-body">计划 ${s.scheduled_date} · ${s.questions.length} 个问题 · 关注 ${(s.focus || []).join("/")}
        ${s.task_id ? ` · <span class="link" onclick="event.stopPropagation();openCall('${s.task_id}')">查看通话</span>` : ""}</div>
    </div>`).join("")}</div>`;
}

async function openPlan(id) {
  const p = await api(`/api/plans/${id}`);
  UI.open(p.patient_name + " · 随访计划", `
    <div class="row" style="margin-bottom:14px">
      <span class="pill ${p.status === "active" ? "teal" : "gray"}">${p.status === "active" ? "执行中" : p.status === "terminated" ? "已终止" : "已完成"}</span>
      <span class="lock">计划固化于 ${(p.frozen_at || "").slice(0, 16).replace("T", " ")}</span>
      <span class="tiny muted">${esc(p.protocol_name)} · 出院 ${p.discharge_date}</span>
    </div>
    ${timeline(p.stages)}
    <div class="section-t">计划事件</div>
    <div class="note-box">${(p.events || []).map(e => `· ${esc(e.text)}`).join("<br/>")}</div>
  `, p.status === "active" ? `<button class="btn danger" onclick="terminatePlan('${p.id}')">终止计划</button>` : "");
}

async function makePlan(pid) {
  try {
    const d = await api(`/api/patients/${pid}/plan`, { method: "POST", body: {} });
    UI.closeAll(); UI.toast("已生成并固化 " + d.stages.length + " 个随访节点"); renderPatients(state.patients.page);
  } catch (e) { UI.toast(e.message); }
}

function readmission(pid) {
  UI.open("登记再次入院 / 新医嘱", `
    <div class="note-box">计划一旦生成即固化。患者再次入院或医生下达新医嘱，<b>不会改写</b>原有出院随访计划，仅追加一条事件记录，保证随访口径稳定、可追溯。</div>
    <div style="margin-top:14px"><label class="f">事件说明</label>
      <textarea id="readmitNote" rows="3">患者因血糖控制不佳再次入院，原出院随访计划保持不变</textarea></div>`,
    `<button class="btn" onclick="UI.closeAll()">取消</button>
     <button class="btn primary" onclick="doReadmit('${pid}')">确认登记</button>`);
}
async function doReadmit(pid) {
  try {
    await api(`/api/patients/${pid}/readmission`, { method: "POST", body: { note: $("#readmitNote").value } });
    UI.closeAll(); UI.toast("已记录再入院事件，原计划不变"); openPatient(pid);
  } catch (e) { UI.toast(e.message); }
}

function terminatePlan(id) {
  UI.open("终止随访计划", `<label class="f">终止原因</label>
    <textarea id="termReason" rows="3">患者已转往上级医院继续治疗，按脱访规则终止随访</textarea>
    <div class="note-box" style="margin-top:12px">终止后该患者未执行的节点全部关闭，已产生的通话与工单保留可追溯。</div>`,
    `<button class="btn" onclick="UI.closeAll()">取消</button>
     <button class="btn danger" onclick="doTerminate('${id}')">确认终止</button>`);
}
async function doTerminate(id) {
  try {
    await api(`/api/plans/${id}/terminate`, { method: "POST", body: { reason: $("#termReason").value } });
    UI.closeAll(); UI.toast("计划已终止"); route();
  } catch (e) { UI.toast(e.message); }
}

function newPatient() {
  UI.open("新建患者", `
    <div class="grid" style="grid-template-columns:1fr 1fr">
      <div><label class="f">姓名</label><input id="npName"/></div>
      <div><label class="f">性别</label><select id="npSex"><option>男</option><option>女</option></select></div>
      <div><label class="f">年龄</label><input id="npAge" type="number"/></div>
      <div><label class="f">联系电话</label><input id="npPhone"/></div>
      <div><label class="f">科室</label><input id="npDept" placeholder="如 内分泌科"/></div>
      <div><label class="f">出院日期</label><input id="npDate" type="date"/></div>
    </div>
    <div style="margin-top:12px"><label class="f">诊断</label><input id="npDx" placeholder="如 2型糖尿病伴周围神经病变"/></div>
    <div style="margin-top:12px"><label class="f">手术（可选）</label><input id="npSurgery" placeholder="如 腹腔镜胆囊切除术"/></div>
    <div style="margin-top:12px"><label class="f">风险分层</label>
      <select id="npRisk"><option>稳定</option><option>高危并发症</option></select></div>`,
    `<button class="btn" onclick="UI.closeAll()">取消</button>
     <button class="btn primary" onclick="doNewPatient()">保存并生成计划</button>`);
}
async function doNewPatient() {
  try {
    await api("/api/patients", { method: "POST", body: {
      name: $("#npName").value, sex: $("#npSex").value, age: +$("#npAge").value || null,
      phone: $("#npPhone").value, dept: $("#npDept").value, diagnosis: $("#npDx").value,
      surgery: $("#npSurgery").value, discharge_date: $("#npDate").value || null,
      risk_group: $("#npRisk").value,
    }});
    UI.closeAll(); UI.toast("已建档并生成随访计划"); renderPatients(1);
  } catch (e) { UI.toast(e.message); }
}

function importDialog() {
  UI.open("导入出院患者名单", `
    <div class="note-box">支持 HIS 导出的 .xlsx：表头包含 姓名、性别、年龄、住院号、联系电话、科室、诊断、出院日期 即可。重复住院号自动跳过。</div>
    <div style="margin-top:14px"><input type="file" id="impFile" accept=".xlsx"/></div>`,
    `<button class="btn" onclick="UI.closeAll()">取消</button>
     <button class="btn primary" onclick="doImport()">开始导入</button>`);
}
async function doImport() {
  const f = $("#impFile").files[0];
  if (!f) return UI.toast("请选择文件");
  const fd = new FormData(); fd.append("file", f);
  try {
    const d = await api("/api/import/patients", { method: "POST", body: fd });
    UI.closeAll(); UI.toast(d.message); renderPatients(1);
  } catch (e) { UI.toast(e.message); }
}

/* ------------------------------------------------------------------ 外呼中心 */
async function renderCalls() {
  const d = await api("/api/calls");
  state.calls = d.items;
  const tabs = [["queued", "待外呼"], ["completed", "已完成"], ["no_answer", "未接通"], ["", "全部"]];
  const list = d.items.filter(x => !state.callTab || x.status === state.callTab);
  $("#view").innerHTML = `
    <div class="row" style="margin-bottom:14px">
      <div class="tabs" style="margin:0;border:none">
        ${tabs.map(([k, l]) => `<div class="tab ${state.callTab === k ? "on" : ""}" onclick="state.callTab='${k}';renderCalls()">${l}
          <span class="tiny muted">${k ? (d.counts[k] || 0) : d.items.length}</span></div>`).join("")}
      </div>
      <span class="spacer"></span>
      <select id="personaSel" style="width:auto;min-width:150px" title="演示场景">
        <option value="">按患者真实情况</option>
        ${PERSONAS.map(p => `<option>${p}</option>`).join("")}
      </select>
      <button class="btn primary" onclick="batchCall()">一键批量外呼</button>
    </div>
    <div class="card"><div class="card-b" style="padding:0 4px">
      <table><thead><tr><th>患者</th><th>随访阶段</th><th>计划时间</th><th>通话状态</th><th>风险</th><th>结果摘要</th><th></th></tr></thead><tbody>
      ${list.map(t => `<tr>
        <td><b>${esc(t.patient_name)}</b><div class="tiny muted">${esc(t.phone)}</div></td>
        <td class="tiny">${esc(t.stage)}</td>
        <td class="tiny muted">${(t.scheduled_at || "").slice(0, 16).replace("T", " ")}</td>
        <td><span class="pill ${t.status === "completed" ? "green" : t.status === "queued" ? "blue" : "gray"}">${STATUS[t.status] || t.status}</span>
          ${t.attempts ? `<span class="tiny muted">第${t.attempts}次</span>` : ""}</td>
        <td>${t.risk ? `<span class="pill ${t.risk}">${RISK[t.risk]}</span>` : `<span class="muted tiny">—</span>`}</td>
        <td class="tiny muted" style="max-width:260px">${esc((t.summary || "").slice(0, 40))}</td>
        <td style="text-align:right;white-space:nowrap">
          ${t.status === "queued" ? `<button class="btn sm primary" onclick="runCall('${t.id}')">外呼</button>` : ""}
          <button class="btn sm" onclick="openCall('${t.id}')">详情</button>
        </td>
      </tr>`).join("")}</tbody></table>
      ${list.length ? "" : `<div class="empty">该分类下暂无任务</div>`}
    </div></div>`;
}

async function batchCall() {
  const sel = $("#personaSel");
  const persona = sel ? sel.value : "";
  try {
    UI.toast("正在批量外呼，请稍候…");
    const d = await api("/api/calls/batch", { method: "POST", body: { limit: 5, persona: persona || null } });
    UI.toast(`已执行 ${d.count} 通外呼`);
    route();
  } catch (e) { UI.toast(e.message); }
}

async function runCall(id) {
  const sel = $("#personaSel");
  const persona = sel ? sel.value : "";
  UI.open("外呼进行中", `
    <div style="text-align:center;padding:26px 0">
      <div class="wave" style="height:26px;justify-content:center;gap:4px">
        <i style="height:10px"></i><i style="height:24px"></i><i style="height:16px"></i><i style="height:26px"></i>
      </div>
      <div style="margin-top:16px;font-size:14px">正在呼叫患者，等待接听…</div>
      <div class="tiny muted" style="margin-top:6px">AI 将按已审核的分阶段话术提问，通话结束后自动转写并解析</div>
    </div>`);
  try {
    const d = await api(`/api/calls/${id}/run`, { method: "POST", body: { persona: persona || null, use_llm: true } });
    if (!d.ok) { UI.toast(d.message); UI.closeAll(); return; }
    if (d.status === "no_answer") {
      UI.open("未接通", `<div class="empty">患者未接听，系统已按重拨策略安排下一次呼叫（30 分钟后 / 次日同时间段）。</div>`);
      return;
    }
    showCall(d.task, true);
  } catch (e) { UI.toast(e.message); UI.closeAll(); }
}

async function openCall(id) {
  try {
    const t = await api(`/api/calls/${id}`);
    showCall(t, false);
  } catch (e) { UI.toast(e.message); }
}

function showCall(t, animate) {
  const r = t.result;
  const level = r ? r.risk_level : null;
  const riskHtml = r ? `
    <div class="risk-banner ${level}">
      <div>
        <b>${RISK[level]}</b>
        <p>${(r.risk_reasons || []).map(esc).join("；")}</p>
      </div>
    </div>` : "";

  const struct = r ? `
    <div class="struct-grid">
      ${[["身份确认", r.structured.identity], ["用药依从性", r.structured.adherence],
         ["空腹血糖", r.structured.fasting_glucose ? r.structured.fasting_glucose + " mmol/L" : "—"],
         ["餐后 2h 血糖", r.structured.postprandial_glucose ? r.structured.postprandial_glucose + " mmol/L" : "—"],
         ["症状", (r.structured.symptoms || []).join("、") || "无"],
         ["复诊落实", r.structured.recheck]]
        .map(([k, v]) => `<div class="struct-cell"><div class="k">${k}</div><div class="v">${esc(v || "—")}</div></div>`).join("")}
    </div>
    <div class="section-t">随访小结</div>
    <div class="note-box">${esc(r.summary)}</div>
    <div class="section-t">处置建议</div>
    <div class="note-box">${esc(r.advice)}<br/><span class="muted">跟进时限：${esc(r.follow_up)} · 解析引擎：${r.engine === "llm" ? "大模型结构化" : "规则引擎"}</span></div>` : "";

  UI.open(`${t.patient_name} · ${t.stage}`, `
    <div class="row" style="margin-bottom:12px">
      <span class="pill ${t.status === "completed" ? "green" : "gray"}">${STATUS[t.status]}</span>
      <span class="chip">${esc(t.phone)}</span>
      <span class="chip">通话 ${t.duration || 0} 秒</span>
      <span class="chip">${t.provider === "aliyun" ? "阿里云线路" : "演练线路"}</span>
      ${t.persona ? `<span class="chip soft-teal">场景：${esc(t.persona)}</span>` : ""}
    </div>
    ${riskHtml}
    <div class="section-t">通话转写</div>
    <div class="dialog" id="dialog">${(t.transcript || []).map((x, i) => turnHtml(x, animate ? i : -1)).join("")}</div>
    <div id="afterCall" style="${animate ? "display:none" : ""}">${struct}
      ${t.ticket ? `<div class="section-t">关联工单</div>
        <div class="ticket-mini ${t.ticket.level}"><div class="tt">${t.ticket.id} · ${RISK[t.ticket.level]} · ${t.ticket.sla_hours}h 时限</div>
        <div class="dd">${esc(t.ticket.reason)}</div></div>` : ""}
    </div>
  `, `<button class="btn" onclick="UI.closeAll()">关闭</button>
      ${t.status === "queued" ? `<button class="btn primary" onclick="runCall('${t.id}')">发起外呼</button>` : ""}
      ${r && level !== "blue" ? `<button class="btn primary" onclick="UI.closeAll();location.hash='#/tickets'">去处理工单</button>` : ""}`,
    level ? `<span class="pill ${level}">${RISK[level]}</span>` : "");

  if (animate) {
    const turns = $$("#dialog .turn");
    turns.forEach(el => el.style.opacity = 0);
    let i = 0;
    const reveal = () => {
      if (i >= turns.length) { $("#afterCall").style.display = ""; return; }
      turns[i].style.opacity = 1; i++;
      setTimeout(reveal, 520);
    };
    setTimeout(reveal, 260);
  }
}

function turnHtml(t, idx) {
  const ai = t.speaker !== "患者";
  return `<div class="turn ${ai ? "ai" : "pt"}">
    <div class="av">${ai ? "AI" : "患"}</div>
    <div class="bubble">${esc(t.text)}<span class="tm">${t.second || 0}s</span></div>
  </div>`;
}

/* ------------------------------------------------------------------ 工单 */
async function renderTickets() {
  const d = await api("/api/tickets");
  $("#view").innerHTML = `
    <div class="row" style="margin-bottom:14px">
      <span class="chip soft-blue">待处理 ${d.items.filter(t => t.status === "pending").length}</span>
      <span class="chip">橙色 / 红色工单 24 小时内处置</span>
    </div>
    <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(340px,1fr))">
      ${d.items.map(t => `<div class="card" style="border-left:3px solid ${t.level === "red" ? "#e0483c" : t.level === "orange" ? "#e8871a" : "#d9a800"}">
        <div class="card-b">
          <div class="row"><b>${esc(t.patient_name)}</b>
            <span class="pill ${t.level}">${RISK[t.level]}</span>
            <span class="spacer"></span>
            <span class="tiny muted">${t.sla_hours}h 时限</span></div>
          <div class="tiny" style="margin-top:8px;line-height:1.7">${esc(t.reason)}</div>
          <div class="note-box" style="margin-top:10px;font-size:12px">${esc(t.suggestion)}</div>
          ${t.doctor_note ? `<div class="tiny" style="margin-top:8px"><span class="muted">医生处理：</span>${esc(t.doctor_note)}</div>` : ""}
          <div class="row" style="margin-top:12px">
            <span class="pill ${t.status === "closed" ? "gray" : "orange"}">${t.status === "closed" ? "已结案" : "待处理"}</span>
            <span class="spacer"></span>
            <button class="btn sm" onclick="openCall('${t.task_id}')">看通话</button>
            ${t.status !== "closed" ? `<button class="btn sm primary" onclick="handleTicket('${t.id}')">处置</button>` : ""}
          </div>
        </div>
      </div>`).join("") || `<div class="empty">暂无工单</div>`}
    </div>`;
}

function handleTicket(id) {
  UI.open("工单处置", `
    <label class="f">处置意见</label>
    <textarea id="tNote" rows="4">已电话联系患者，指导按时用药并调整饮食，预约 3 日后门诊复查血糖与神经传导。</textarea>
    <div class="note-box" style="margin-top:12px">结案后工单归档，通话录音与结构化结果一并留存，形成随访闭环。</div>`,
    `<button class="btn" onclick="UI.closeAll()">取消</button>
     <button class="btn primary" onclick="doHandle('${id}')">结案</button>`);
}
async function doHandle(id) {
  try {
    await api(`/api/tickets/${id}/handle`, { method: "POST", body: { status: "closed", doctor_note: $("#tNote").value } });
    UI.closeAll(); UI.toast("工单已结案"); renderTickets();
  } catch (e) { UI.toast(e.message); }
}

/* ------------------------------------------------------------------ 设置 */
async function renderSettings() {
  const s = await api("/api/settings");
  const line = s.line || {};
  const h = await api("/api/llm/health").catch(() => ({ ok: false }));
  $("#view").innerHTML = `
    <div class="grid" style="grid-template-columns:1fr 1fr">
      <div class="card"><div class="card-h"><h3>外呼线路</h3>
        <span class="pill ${line.mode === "real" ? "green" : "orange"}">${line.mode === "real" ? "真实外呼" : "演练模式"}</span></div>
        <div class="card-b">
          <label class="f">线路名称</label><input id="setLineName" value="${esc(line.name || "")}"/>
          <div class="grid" style="grid-template-columns:1fr 1fr;margin-top:12px">
            <div><label class="f">主叫号码</label><input id="setCaller" value="${esc(line.caller_number || "")}"/></div>
            <div><label class="f">并发数</label><input id="setConcurrent" type="number" value="${line.concurrent || 5}"/></div>
          </div>
          <label class="f" style="margin-top:12px">外呼模式</label>
          <select id="setMode">
            <option value="mock" ${line.mode !== "real" ? "selected" : ""}>演练模式（本地模拟通话，答辩演示用）</option>
            <option value="real" ${line.mode === "real" ? "selected" : ""}>真实外呼（阿里云语音双呼）</option>
          </select>
          <label class="f" style="margin-top:12px">白名单号码（逗号分隔，真实外呼仅拨打白名单）</label>
          <input id="setWhite" value="${esc((line.whitelist || []).join(","))}"/>
          <div class="note-box" style="margin-top:14px">
            阿里云 AccessKey：<b>${line.aliyun_configured ? "已配置" : "未配置"}</b><br/>
            配置环境变量 <code>ALIYUN_ACCESS_KEY_ID</code> / <code>ALIYUN_ACCESS_KEY_SECRET</code> 后，
            切换为真实外呼即可通过 dyvmsapi 发起语音双呼，回执由 <code>/api/calls/aliyun-callback</code> 接收并回写转写结果。
          </div>
          <div class="row" style="margin-top:14px"><button class="btn primary" onclick="saveSettings()">保存线路设置</button></div>
        </div>
      </div>

      <div class="grid" style="gap:16px">
        <div class="card"><div class="card-h"><h3>业务日期</h3><span class="hint">用于演示不同的到期节点</span></div>
          <div class="card-b">
            <div class="row"><input id="setDate" type="date" value="${s.business_date}" style="max-width:200px"/>
              <button class="btn" onclick="saveDate()">更新</button></div>
            <div class="tiny muted" style="margin-top:8px">调整业务日期后，计划中到期的节点会进入待外呼队列。</div>
          </div>
        </div>
        <div class="card"><div class="card-h"><h3>大模型</h3>
          <span class="pill ${h.ok ? "green" : "orange"}">${h.ok ? "已连通" : "未连通"}</span></div>
          <div class="card-b">
            <div class="kv">
              <div class="k">模型</div><div>${esc(s.llm.model)}</div>
              <div class="k">用途</div><div class="tiny">分阶段话术生成 · 通话文本结构化抽取</div>
              <div class="k">兜底</div><div class="tiny">模型不可用时自动切换规则引擎，外呼流程不中断</div>
            </div>
          </div>
        </div>
        <div class="card"><div class="card-h"><h3>数据</h3></div>
          <div class="card-b tiny muted">数据库：${esc(s.db)}<br/>演示数据包含 25 名脱敏糖尿病患者与 40 名 HIS 出院患者。</div>
        </div>
      </div>
    </div>`;
}

async function saveSettings() {
  try {
    await api("/api/settings", { method: "PUT", body: { line: {
      name: $("#setLineName").value, caller_number: $("#setCaller").value,
      concurrent: +$("#setConcurrent").value || 5, mode: $("#setMode").value,
      whitelist: $("#setWhite").value.split(",").map(x => x.trim()).filter(Boolean),
    }}});
    UI.toast("线路设置已保存"); route();
  } catch (e) { UI.toast(e.message); }
}
async function saveDate() {
  try {
    await api("/api/settings", { method: "PUT", body: { business_date: $("#setDate").value } });
    UI.toast("业务日期已更新"); route();
  } catch (e) { UI.toast(e.message); }
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);
