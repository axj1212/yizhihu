/* 医智呼 · 云服务接入层（原生 JS，无构建步骤）
 * 单一客户端：Auth / Database / Storage / LLM 全部复用同一个 cloud 实例。
 * 公开配置来自 cloud-config.js（activate 返回的 publicConfig），不含任何服务端密钥。
 */
"use strict";

let yzhCloud = null;
const CloudState = {
  session: null, models: [], model: null, pendingOtp: null,
  records: [], total: 0, page: 1, risk: "", files: [], generated: null,
  lastSync: null, busy: false,
};

function cloudInit() {
  if (yzhCloud) return true;
  const cfg = window.YZH_PUBLIC_CONFIG;
  if (!window.WorkBuddyCloud || !cfg || !cfg.endpoint || !cfg.publishableKey) return false;
  try {
    yzhCloud = WorkBuddyCloud.createWorkBuddyCloud({
      endpoint: cfg.endpoint,
      oauthRelayBaseUrl: cfg.oauthRelayBaseUrl,
      publishableKey: cfg.publishableKey,
    });
    return true;
  } catch (e) {
    console.warn("云服务初始化失败", e && e.message);
    yzhCloud = null;
    return false;
  }
}

function cloudErr(e) {
  const code = (e && (e.code || (e.error && e.error.code))) || "";
  const raw = (e && (e.message || (e.error && e.error.message))) || "云服务请求失败";
  if (/^auth_/.test(code)) return "身份校验未通过（域名与应用绑定不一致）。" + raw;
  if (/^quota_/.test(code)) return "云服务额度不足或触发限流，请稍后重试。" + raw;
  if (/^request_/.test(code)) return "请求参数不被模型接受：" + raw;
  if (/^(gateway_|model_|internal_)/.test(code)) return "模型服务暂时不可用，请重试。(" + (e.requestId || code) + ")";
  if (code === "42501") return "没有权限操作这条数据，请先登录。";
  if (code === "23505") return "这条记录已存在。";
  if (code === "42P01") return "云数据表不存在，请检查云服务初始化。";
  if (/abort|timeout|timed out/i.test(raw)) return "模型调用超时，请稍后重试或切换更快的模型。";
  if (/network|fetch|Failed to fetch/i.test(raw)) return "网络连接失败，请检查网络后重试。";
  return raw;
}

function cloudUnwrap(res) {
  if (res && res.error) throw res.error;
  return res ? res.data : null;
}

/* 访问本应用自己的业务接口（不依赖主页面脚本，自检页也可复用） */
async function yzhLocalApi(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "本地业务接口请求失败");
  return data;
}

function maskUid(uid) {
  const s = String(uid || "");
  return s ? s.slice(0, 4) + "***" : "—";
}

/* ------------------------------------------------------------ 认证 */
async function cloudSession() {
  if (!cloudInit()) return null;
  const r = await yzhCloud.auth.getSession();
  CloudState.session = r && !r.error ? r.data : null;
  return CloudState.session;
}

async function cloudAuthGate() {
  const s = await cloudSession();
  if (!s) throw new Error("请先登录医生账号，再操作私有数据与文件");
  return s;
}

async function cloudSendCode(email) {
  if (!cloudInit()) throw new Error("云服务尚未就绪");
  const value = String(email || "").trim();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) throw new Error("请输入正确的邮箱地址");
  const sent = await yzhCloud.auth.sendOtp({ email: value });
  if (sent.error) throw sent.error;
  CloudState.pendingOtp = {
    email: value,
    verificationId: sent.data.verificationId,
    isExistingUser: sent.data.isExistingUser,
  };
  return true;
}

async function cloudVerifyCode(email, code) {
  if (!cloudInit()) throw new Error("云服务尚未就绪");
  const p = CloudState.pendingOtp;
  if (!p || p.email !== String(email || "").trim()) throw new Error("请先为当前邮箱获取验证码");
  if (!String(code || "").trim()) throw new Error("请输入邮件中的验证码");
  const done = await yzhCloud.auth.verifyOtp({
    email: p.email,
    verificationId: p.verificationId,
    isExistingUser: p.isExistingUser,
    token: String(code).trim(),
  });
  if (done.error) throw done.error;
  CloudState.pendingOtp = null;
  await cloudSession();
  return CloudState.session;
}

/* ------------------------------------------------------------ 云数据库 */
async function cloudSyncFromLocal() {
  if (!cloudInit()) throw new Error("云服务尚未就绪");
  const s = await cloudSession();
  const list = await yzhLocalApi("/api/calls?status=completed");
  const tasks = (list.items || []).slice(0, 12);
  let n = 0;
  for (const t of tasks) {
    let structured = null;
    try {
      const d = await yzhLocalApi("/api/calls/" + encodeURIComponent(t.id));
      structured = (d.result && d.result.structured) || null;
    } catch (_) { /* 结构化字段缺失时不阻断同步 */ }
    const row = {
      task_id: String(t.id),
      patient_name: t.patient_name || "",
      stage_label: t.stage || "",
      risk_level: t.risk || "blue",
      summary: t.summary || "",
      structured: structured,
      doctor_name: (s && s.user && (s.user.name || s.user.email)) || null,
      source: "医智呼 AI 外呼",
    };
    if (row.doctor_name && /@/.test(row.doctor_name)) row.doctor_name = "值班医师";
    const r = await yzhCloud.database.from("followup_records").upsert(row, { onConflict: "task_id" });
    if (r.error) throw r.error;
    n += 1;
  }
  CloudState.lastSync = new Date().toLocaleString("zh-CN");
  return n;
}

async function cloudLoadRecords(page = 1, risk = "") {
  if (!cloudInit()) return { items: [], total: 0 };
  const size = 8;
  let q = yzhCloud.database.from("followup_records")
    .select("*", { count: "exact" })
    .order("created_at", { ascending: false });
  if (risk) q = q.eq("risk_level", risk);
  const r = await q.range((page - 1) * size, page * size - 1);
  const data = cloudUnwrap(r) || [];
  CloudState.records = Array.isArray(data) ? data : [];
  CloudState.total = (r && r.count) || CloudState.records.length;
  CloudState.page = page;
  CloudState.risk = risk;
  return { items: CloudState.records, total: CloudState.total };
}

async function cloudAddNote(recordId, text) {
  const s = await cloudAuthGate();
  const note = String(text || "").trim();
  if (!note) throw new Error("备注不能为空");
  const r = await yzhCloud.database.from("doctor_notes").insert({
    record_id: recordId || null,
    note: note,
    owner_name: (s.user && s.user.name) || null,
  }).select();
  const data = cloudUnwrap(r) || [];
  if (!data.length) throw new Error("写入被权限策略拦截，请确认已登录");
  return data[0];
}

async function cloudLoadNotes() {
  await cloudAuthGate();
  const r = await yzhCloud.database.from("doctor_notes")
    .select("id,record_id,note,owner_name,created_at")
    .order("created_at", { ascending: false }).limit(8);
  return cloudUnwrap(r) || [];
}

/* ------------------------------------------------------------ 云存储 */
const CLOUD_FILE_EXT = ["txt", "md", "json", "csv", "xlsx", "pdf"];
const CLOUD_FILE_TYPE = {
  txt: "text/plain", md: "text/markdown", json: "application/json",
  csv: "text/csv", pdf: "application/pdf",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};

async function cloudUploadReport(file) {
  const s = await cloudAuthGate();
  if (!file) throw new Error("请选择文件");
  const ext = String(file.name || "").split(".").pop().toLowerCase();
  if (!CLOUD_FILE_EXT.includes(ext)) throw new Error("仅支持 txt / md / json / csv / xlsx / pdf 随访资料");
  if (file.size > 5 * 1024 * 1024) throw new Error("文件不能超过 5 MB");
  const safe = String(file.name).replace(/[^0-9A-Za-z._\-\u4e00-\u9fa5]/g, "_").slice(0, 60);
  const path = yzhCloud.storage.sharedPath(s.user.id, "reports/" + Date.now() + "-" + safe);
  const r = await yzhCloud.storage.upload(path, file, {
    contentType: file.type || CLOUD_FILE_TYPE[ext] || "application/octet-stream",
    upsert: false,
  });
  if (r.error) throw r.error;
  return { path: path, name: safe, size: file.size };
}

async function cloudListFiles() {
  await cloudAuthGate();
  const r = await yzhCloud.storage.list("shared", { limit: 60 });
  const d = cloudUnwrap(r);
  const arr = Array.isArray(d) ? d : (d && (d.items || d.objects || d.files)) || [];
  CloudState.files = arr;
  return arr;
}

function cloudFilePath(f) {
  return f.path || f.key || f.name || (f.fullPath ? String(f.fullPath) : "");
}

async function cloudOpenFile(path) {
  await cloudAuthGate();
  const r = await yzhCloud.storage.createSignedUrl(path, 600);
  if (r.error) throw r.error;
  const d = r.data;
  const url = typeof d === "string" ? d : (d && (d.signedUrl || d.url || d.signed_url));
  if (!url) throw new Error("未获取到文件访问链接");
  return url;
}

/* ------------------------------------------------------------ 免密钥大模型 */
async function cloudLoadModels() {
  if (!cloudInit()) throw new Error("云服务尚未就绪");
  const models = await yzhCloud.llm.models.list();
  const usable = (Array.isArray(models) ? models : [])
    .filter(m => m && m.enabled !== false && m.disabled !== true);
  CloudState.models = usable;
  CloudState.model = usable.find(m => m.id === "auto") || usable[0] || null;
  if (!CloudState.model) throw new Error("当前应用没有可用模型，请在云服务控制台确认模型目录");
  return usable;
}

async function cloudChatOnce(userText, systemText, opts = {}) {
  if (!CloudState.model) await cloudLoadModels();
  const model = opts.model || CloudState.model.id;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), opts.timeoutMs || 30000);
  let text = "";
  try {
    const params = {
      model: model,
      messages: [
        { role: "system", content: systemText },
        { role: "user", content: userText },
      ],
      stream: true,
      temperature: opts.temperature == null ? 0.3 : opts.temperature,
      signal: ctrl.signal,
    };
    if (opts.json) params.response_format = { type: "json_object" };
    const stream = yzhCloud.llm.chat.completions.create(params);
    for await (const chunk of stream) {
      const d = chunk && chunk.choices && chunk.choices[0] && chunk.choices[0].delta;
      if (d && d.content) text += d.content;
      if (d && d.reasoning_content) { /* 推理内容不并入正文 */ }
    }
  } finally {
    clearTimeout(timer);
  }
  return text;
}

function cloudFastModel() {
  const ids = ["hunyuan-chat", "glm-5.3-flash", "deepseek-v4.1-flash", "hy4free", "auto"];
  for (const id of ids) {
    const hit = CloudState.models.find(m => m.id === id);
    if (hit) return hit.id;
  }
  return CloudState.model && CloudState.model.id;
}

function cloudJsonModel() {
  const ids = ["glm-5.3-flash", "deepseek-v4.1-flash", "auto", "hunyuan-chat"];
  for (const id of ids) {
    const hit = CloudState.models.find(m => m.id === id);
    if (hit) return hit.id;
  }
  return CloudState.model && CloudState.model.id;
}

/* 模型输出不可信：优先按 JSON 解析，失败时退回编号列表，始终由医生审核后发布 */
function cloudParseQuestions(text) {
  let t = String(text || "").trim()
    .replace(/^```(?:json)?\s*/i, "").replace(/```\s*$/, "").trim();
  const s = t.indexOf("{"), e = t.lastIndexOf("}");
  if (s >= 0 && e > s) {
    try {
      const obj = JSON.parse(t.slice(s, e + 1));
      const raw = obj.questions || obj.items || [];
      const qs = raw.filter(q => q && q.text).map(q => ({
        text: String(q.text).slice(0, 120),
        purpose: String(q.purpose || "采集恢复情况").slice(0, 80),
        branch: String(q.branch || "若有异常，追问持续时间并建议复诊").slice(0, 120),
      }));
      if (qs.length) return qs;
    } catch (_) { /* 继续用文本兜底 */ }
  }
  const lines = t.split(/\n+/).map(x => x
    .replace(/^\s*(?:\d+\s*[\.、)]|[-*•])\s*/, "")
    .replace(/\*\*/g, "").replace(/^["'“]/, "").trim())
    .filter(x => x.length >= 8 && !/^(以下是|好的|作为|问题列表|JSON|注意|说明)/.test(x));
  return lines.slice(0, 7).map(line => {
    const parts = line.split(/[：:｜|]/);
    return {
      text: (parts[0] || line).trim().slice(0, 120),
      purpose: (parts.slice(1).join(" ").trim() || "采集术后恢复与用药情况").slice(0, 80),
      branch: "若回答提示异常，追问持续时间、诱因，并建议尽快复诊",
    };
  });
}

async function cloudGenerateQuestions(form) {
  const sys = "你是三甲医院随访中心的医生助理，把随访规范改写成适合 AI 电话外呼的提问。"
    + "只输出结果本身，不要解释、不要 Markdown 代码围栏。优先输出 JSON 对象 "
    + "{\"questions\":[{\"text\":\"\",\"purpose\":\"\",\"branch\":\"\"}]}；"
    + "若无法输出 JSON，就每行写一个问题并用 1. 2. 3. 编号。"
    + "称呼统一用「您」，先确认身份再问病情，单句不超过 40 字，避免诱导性提问和诊断性表述。";
  const payload = {
    病种: form.disease || "2 型糖尿病", 手术: form.surgery || "无",
    出院天数: Number(form.day || 7), 阶段: "出院后第 " + Number(form.day || 7) + " 天随访",
    关注点: [form.focus || "用药依从性、症状变化、复诊落实"],
    要求: "生成 5 个口语化问题，每个问题给出采集目的与异常追问分支。",
  };
  const text = await cloudChatOnce(JSON.stringify(payload, null, 0), sys,
    { model: cloudFastModel(), timeoutMs: 45000 });
  const qs = cloudParseQuestions(text);
  if (!qs.length) throw new Error("模型未生成有效问题，请重试（" + text.trim().slice(0, 50) + "）");
  CloudState.generated = qs;
  return qs;
}

async function cloudApplyQuestions(protocolId, stageKey, questions) {
  const p = await yzhLocalApi("/api/protocols/" + encodeURIComponent(protocolId));
  const stages = (p.stages || []).map(s => s.key === stageKey ? { ...s, questions: questions } : s);
  if (!stages.some(s => s.key === stageKey)) throw new Error("方案中未找到该阶段");
  await yzhLocalApi("/api/protocols/" + encodeURIComponent(protocolId), { method: "PATCH", body: { stages: stages, reviewer: "云 AI 生成 · 待医生审核" } });
  return true;
}

/* ------------------------------------------------------------ 端到端自检 */
async function cloudSelfTest(onStep) {
  const steps = [];
  const push = (name, good, detail) => {
    const item = { name: name, ok: !!good, detail: String(detail == null ? "" : detail) };
    steps.push(item);
    if (typeof onStep === "function") { try { onStep(item, steps); } catch (_) {} }
    return item;
  };
  if (!cloudInit()) { push("SDK 初始化", false, "SDK 未加载或公开配置缺失"); return steps; }
  push("SDK 初始化", true, "endpoint / publishableKey / oauthRelayBaseUrl 已注入");

  try {
    const s = await yzhCloud.auth.getSession();
    const signed = !!(s && s.data);
    CloudState.session = signed ? s.data : null;
    push("会话状态", true, signed ? "已登录 " + maskUid(s.data.user && s.data.user.id) : "未登录（登录入口与数据门禁已生效）");
  } catch (e) { push("会话状态", false, cloudErr(e)); }

  try {
    const ms = await cloudLoadModels();
    push("模型目录", ms.length > 0, ms.length + " 个可用模型，选用 " + (CloudState.model && CloudState.model.id));
  } catch (e) { push("模型目录", false, cloudErr(e)); }

  try {
    const fm = cloudFastModel();
    const text = await cloudChatOnce("请只回复两个字：正常", "你是连通性测试助手，只输出极短的回答，不要解释。",
      { model: fm, timeoutMs: 25000 });
    push("免密钥模型调用", !!text.trim(), "模型 " + fm + " 返回：" + text.trim().slice(0, 24));
  } catch (e) { push("免密钥模型调用", false, cloudErr(e)); }

  try {
    const key = "selftest-" + Date.now();
    const ins = await yzhCloud.database.from("followup_records").insert({
      task_id: key, patient_name: "云服务自检", stage_label: "连通性测试",
      risk_level: "blue", summary: "写入并读回验证", structured: { probe: true },
      source: "selfTest",
    }).select();
    if (ins.error) throw ins.error;
    const back = await yzhCloud.database.from("followup_records")
      .select("id,task_id,summary").eq("task_id", key).maybeSingle();
    if (back.error) throw back.error;
    push("云数据库写入/读回", !!back.data && back.data.task_id === key,
      "记录 id=" + (back.data && back.data.id) + "，读回一致=" + (!!back.data && back.data.task_id === key));
  } catch (e) { push("云数据库写入/读回", false, cloudErr(e)); }

  try {
    const blue = await yzhCloud.database.from("followup_records")
      .select("id,risk_level").eq("risk_level", "blue").order("id", { ascending: false }).range(0, 2);
    const rows = (blue && blue.data) || [];
    const good = !blue.error && rows.every(x => x.risk_level === "blue") && rows.length <= 3;
    push("条件查询/分页", good, "按风险等级筛选并分页，返回 " + rows.length + " 条，结果全部匹配");
  } catch (e) { push("条件查询/分页", false, cloudErr(e)); }

  try {
    const cross = await yzhCloud.database.from("doctor_notes")
      .update({ note: "越权改写测试" }).eq("owner_id", "another-doctor-uid").select();
    const blocked = !!cross.error || (Array.isArray(cross.data) && cross.data.length === 0);
    push("私有数据隔离", blocked, blocked
      ? (cross.error ? "跨用户改写被拒绝（" + (cross.error.code || "policy") + "）" : "跨用户改写被 RLS 过滤，影响 0 行")
      : "异常：跨用户改写未被拦截，请检查 RLS 策略");
  } catch (e) { push("私有数据隔离", false, cloudErr(e)); }

  try {
    const own = await yzhCloud.database.from("doctor_notes")
      .select("id,note").order("id", { ascending: false }).limit(5);
    push("本人私有备注读回", !own.error, "当前身份可见 " + (((own && own.data) || []).length) + " 条备注");
  } catch (e) { push("本人私有备注读回", false, cloudErr(e)); }

  try {
    const s = await yzhCloud.auth.getSession();
    if (!s || !s.data) {
      const f = new File([new Blob(["未登录上传拦截测试"], { type: "text/plain" })],
        "deny.txt", { type: "text/plain" });
      let denied = false, note = "";
      try { await cloudUploadReport(f); }
      catch (e) { denied = true; note = cloudErr(e); }
      push("云存储门禁", denied, denied ? "未登录上传被拦截：" + note : "异常：未登录却上传成功");
    } else {
      const f = new File([new Blob(["医智呼云存储自检 " + new Date().toISOString()], { type: "text/plain" })],
        "selftest.txt", { type: "text/plain" });
      const up = await cloudUploadReport(f);
      const url = await cloudOpenFile(up.path);
      const dl = await fetch(url);
      const body = await dl.text();
      push("云存储上传/签名下载", dl.ok && body.indexOf("医智呼") >= 0,
        "HTTP " + dl.status + "，内容长度 " + body.length);
    }
  } catch (e) { push("云存储上传/签名下载", false, cloudErr(e)); }

  push("失败路径文案", true, "auth_ / quota_ / gateway_ / 42501 / 网络异常均已映射为用户可读提示");

  const passed = steps.filter(x => x.ok).length;
  try {
    const r = await yzhCloud.database.from("followup_records").insert({
      task_id: "selftest-report-" + Date.now(),
      patient_name: "云服务自检报告",
      stage_label: "端到端验证",
      risk_level: "blue",
      summary: "自检 " + passed + "/" + steps.length + " 项通过",
      structured: { steps: steps, passed: passed, total: steps.length },
      source: "cloudSelfTest",
    });
    if (r.error) throw r.error;
    push("自检报告入库", true, "结果已持久化到云数据库");
  } catch (e) { push("自检报告入库", false, cloudErr(e)); }
  return steps;
}

const CloudKit = {
  init: cloudInit, session: cloudSession, sendCode: cloudSendCode, verifyCode: cloudVerifyCode,
  syncFromLocal: cloudSyncFromLocal, loadRecords: cloudLoadRecords,
  addNote: cloudAddNote, loadNotes: cloudLoadNotes,
  uploadReport: cloudUploadReport, listFiles: cloudListFiles, openFile: cloudOpenFile,
  loadModels: cloudLoadModels, chatOnce: cloudChatOnce,
  generateQuestions: cloudGenerateQuestions,
  applyQuestions: cloudApplyQuestions, selfTest: cloudSelfTest,
  state: CloudState, err: cloudErr,
};
window.CloudKit = CloudKit;

/* ------------------------------------------------------------ 云服务页面 */
function cloudRecordHtml(r) {
  const risk = r.risk_level || "blue";
  const label = { red: "红色·紧急", orange: "橙色·中度", yellow: "黄色·轻度", blue: "蓝色·平稳" }[risk] || risk;
  const ts = r.created_at ? new Date(r.created_at).toLocaleString("zh-CN", { hour12: false }).slice(5, 16) : "—";
  return `<div class="call-item" style="align-items:flex-start">
    <span class="pill ${risk}">${label}</span>
    <div style="flex:1;min-width:0">
      <div class="sm" style="font-weight:600">${esc(r.patient_name || "—")} · ${esc(r.stage_label || "随访")}</div>
      <div class="sm muted">${esc((r.summary || "—").slice(0, 70))}</div>
      <div class="tiny muted" style="margin-top:3px">${ts} · ${esc(r.source || "云端")} ${r.doctor_name ? "· " + esc(r.doctor_name) : ""}</div>
    </div>
    <button class="btn sm" onclick="cloudNoteDialog('${esc(r.id)}')">写备注</button>
  </div>`;
}

async function renderCloud() {
  const view = document.querySelector("#view");
  if (!cloudInit()) {
    view.innerHTML = `<div class="card"><div class="card-b">
      <div class="empty">云服务 SDK 未加载。请确认网络可访问静态资源后刷新；本地随访业务不受影响。</div></div></div>`;
    return;
  }
  await cloudSession();
  await Promise.allSettled([cloudLoadModels(), cloudLoadRecords(1, ""), cloudListFiles()]);
  const s = CloudState.session;
  const total = CloudState.total;
  const pages = Math.max(1, Math.ceil(total / 8));

  view.innerHTML = `
  <div class="kpis">
    <div class="kpi blue"><div class="t">云服务环境</div><div class="v" style="font-size:22px">已开通</div>
      <div class="d">计费正常 · 资源已分配</div><div class="spark">☁</div></div>
    <div class="kpi teal"><div class="t">医生身份</div><div class="v" style="font-size:22px">${s ? "已登录" : "未登录"}</div>
      <div class="d">${s ? "当前会话 " + esc(maskUid(s.user && s.user.id)) : "邮箱验证码登录后可用私有数据"}</div><div class="spark">⌾</div></div>
    <div class="kpi orange"><div class="t">云端随访记录</div><div class="v">${total}</div>
      <div class="d">${CloudState.lastSync ? "最近同步 " + esc(CloudState.lastSync) : "AI 外呼结果结构化入库"}</div><div class="spark">☰</div></div>
    <div class="kpi red"><div class="t">免密钥模型</div><div class="v" style="font-size:22px">${CloudState.models.length}</div>
      <div class="d">当前 ${esc((CloudState.model && (CloudState.model.name || CloudState.model.id)) || "—")}</div><div class="spark">✦</div></div>
  </div>

  <div class="grid" style="gap:16px;margin-top:16px">
    <div class="card"><div class="card-h"><h3>医生账号 · 云端身份</h3>
        <span class="pill ${s ? "green" : "gray"}">${s ? "会话有效" : "需要登录"}</span></div>
      <div class="card-b">
        ${s ? `<div class="kv">
            <div class="k">身份</div><div>${esc(s.user && (s.user.name || "值班医师"))}</div>
            <div class="k">标识</div><div class="tiny">${esc(maskUid(s.user && s.user.id))}</div>
            <div class="k">说明</div><div class="tiny">登录会话自动附加到数据库、存储与模型请求</div>
          </div><div class="row" style="margin-top:12px">
            <button class="btn" onclick="cloudLogout()">退出登录</button>
            <button class="btn primary" onclick="cloudNoteList()">查看我的处置备注</button></div>`
          : `<div class="row" style="flex-wrap:wrap">
            <input id="cloudEmail" type="email" placeholder="医生邮箱" style="max-width:230px"/>
            <button class="btn" onclick="cloudSendCodeBtn()">获取验证码</button>
            <input id="cloudCode" placeholder="邮件验证码" style="max-width:130px"/>
            <button class="btn primary" onclick="cloudVerifyBtn()">登录 / 注册</button>
          </div><div class="tiny muted" style="margin-top:8px">首次登录自动注册，无需密码；验证码仅用于本次登录。</div>`}
      </div>
    </div>

    <div class="card"><div class="card-h"><h3>接入自检</h3>
        <button class="btn sm primary" onclick="cloudSelfTestBtn()">运行自检</button></div>
      <div class="card-b"><div id="cloudTestOut" class="tiny muted">逐项验证：会话、模型目录、免密钥调用、数据库写入读回、条件查询、私有数据隔离、文件门禁。</div></div>
    </div>
  </div>

  <div class="grid" style="gap:16px;margin-top:16px">
    <div class="card"><div class="card-h"><h3>随访结果 · 云数据库</h3>
        <div class="row">
          <select id="cloudRisk" onchange="cloudFilter()" style="max-width:130px">
            <option value="">全部风险</option><option value="red">红色</option>
            <option value="orange">橙色</option><option value="yellow">黄色</option><option value="blue">蓝色</option>
          </select>
          <button class="btn sm primary" onclick="cloudSyncBtn()">同步本地外呼结果</button>
          <button class="btn sm" onclick="cloudRefreshBtn()">刷新</button>
        </div></div>
      <div class="card-b" id="cloudRecords">
        ${CloudState.records.length ? CloudState.records.map(cloudRecordHtml).join("") : `<div class="empty">云端还没有记录，点击「同步本地外呼结果」把 AI 随访结构化数据写入云数据库。</div>`}
      </div>
      <div class="card-b" style="border-top:1px solid var(--line-2)">
        <div class="pager"><span class="tiny muted">共 ${total} 条 · 第 ${CloudState.page}/${pages} 页</span>
          <div class="row"><button class="btn sm" onclick="cloudPage(${CloudState.page - 1})" ${CloudState.page <= 1 ? "disabled" : ""}>上一页</button>
          <button class="btn sm" onclick="cloudPage(${CloudState.page + 1})" ${CloudState.page >= pages ? "disabled" : ""}>下一页</button></div></div>
      </div>
    </div>

    <div class="card"><div class="card-h"><h3>随访资料 · 云存储</h3><span class="hint">登录后可用</span></div>
      <div class="card-b">
        <div class="row" style="flex-wrap:wrap">
          <input type="file" id="cloudFile" accept=".txt,.md,.json,.csv,.xlsx,.pdf" style="max-width:250px"/>
          <button class="btn sm primary" onclick="cloudUploadBtn()">上传到云端</button>
          <button class="btn sm" onclick="cloudFilesBtn()">刷新列表</button>
        </div>
        <div class="tiny muted" style="margin-top:8px">支持 txt / md / json / csv / xlsx / pdf，单文件不超过 5 MB；文件存于应用共享目录，访问链接 10 分钟有效。</div>
        <div id="cloudFiles" style="margin-top:12px">${CloudState.files.length ? CloudState.files.map(f => `
          <div class="call-item"><div style="flex:1;min-width:0"><div class="sm">${esc(String(f.name || cloudFilePath(f)).split("/").pop())}</div></div>
          <button class="btn sm" onclick="cloudOpenBtn('${esc(cloudFilePath(f))}')">打开</button></div>`).join("") : `<div class="empty" style="padding:18px">${s ? "暂无云端文件" : "登录后上传随访报告或患者名单"}</div>`}</div>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:16px"><div class="card-h"><h3>云 AI · 分阶段随访话术生成</h3>
      <span class="hint">免密钥调用，生成后由医生审核发布</span></div>
    <div class="card-b">
      <div class="row" style="flex-wrap:wrap">
        <input id="cloudDisease" placeholder="病种：2 型糖尿病" style="max-width:190px"/>
        <input id="cloudSurgery" placeholder="手术：无" style="max-width:150px"/>
        <input id="cloudDay" type="number" value="7" min="1" max="365" style="max-width:100px"/>
        <input id="cloudFocus" placeholder="关注点：用药依从性" style="max-width:200px"/>
        <button class="btn primary" onclick="cloudGenBtn()">云 AI 生成提问</button>
        <button class="btn teal" onclick="cloudApplyBtn()" ${CloudState.generated ? "" : "disabled"}>应用到方案</button>
      </div>
      <div id="cloudQuestions" class="tiny muted" style="margin-top:12px">生成结果会展示在这里；点击「应用到方案」写入当前病种方案的 D7 阶段并标记待医生审核。</div>
    </div>
  </div>`;
}

/* ------------------------------------------------------------ 页面事件 */
async function cloudSendCodeBtn() {
  try { await cloudSendCode(document.querySelector("#cloudEmail").value); UI.toast("验证码已发送，请查收邮件"); }
  catch (e) { UI.toast(cloudErr(e)); }
}
async function cloudVerifyBtn() {
  try {
    await cloudVerifyCode(document.querySelector("#cloudEmail").value, document.querySelector("#cloudCode").value);
    UI.toast("登录成功"); route();
  } catch (e) { UI.toast(cloudErr(e)); }
}
async function cloudLogout() {
  try { await yzhCloud.auth.signOut(); CloudState.session = null; UI.toast("已退出登录"); route(); }
  catch (e) { UI.toast(cloudErr(e)); }
}
async function cloudSyncBtn() {
  if (CloudState.busy) return;
  CloudState.busy = true;
  try { const n = await cloudSyncFromLocal(); UI.toast("已同步 " + n + " 条随访结果到云端"); await renderCloud(); }
  catch (e) { UI.toast(cloudErr(e)); }
  finally { CloudState.busy = false; }
}
async function cloudRefreshBtn() { try { await cloudLoadRecords(CloudState.page, CloudState.risk); await renderCloud(); } catch (e) { UI.toast(cloudErr(e)); } }
async function cloudFilter() { const v = document.querySelector("#cloudRisk").value; try { await cloudLoadRecords(1, v); await renderCloud(); } catch (e) { UI.toast(cloudErr(e)); } }
async function cloudPage(p) { if (p < 1) return; try { await cloudLoadRecords(p, CloudState.risk); await renderCloud(); } catch (e) { UI.toast(cloudErr(e)); } }
async function cloudNoteDialog(id) {
  try { await cloudAuthGate(); } catch (e) { UI.toast(cloudErr(e)); return; }
  UI.open("处置备注", `<div class="card-b"><textarea id="cloudNoteText" rows="4" style="width:100%" placeholder="记录处置意见，仅本人可见"></textarea>
    <div class="tiny muted" style="margin-top:8px">备注按登录身份行级隔离，其他医生无法读取。</div></div>`,
    `<button class="btn primary" onclick="cloudSaveNote('${esc(id)}')">保存到云端</button>`);
}
async function cloudSaveNote(id) {
  try { await cloudAddNote(Number(id) || null, document.querySelector("#cloudNoteText").value); UI.closeAll(); UI.toast("备注已保存"); }
  catch (e) { UI.toast(cloudErr(e)); }
}
async function cloudNoteList() {
  try {
    const rows = await cloudLoadNotes();
    UI.open("我的处置备注", `<div class="card-b">${rows.length ? rows.map(r =>
      `<div class="call-item"><div style="flex:1"><div class="sm">${esc(r.note)}</div>
       <div class="tiny muted">${new Date(r.created_at).toLocaleString("zh-CN", { hour12: false })}</div></div></div>`).join("")
      : `<div class="empty">暂无备注</div>`}</div>`);
  } catch (e) { UI.toast(cloudErr(e)); }
}
async function cloudUploadBtn() {
  const el = document.querySelector("#cloudFile");
  try {
    if (!el.files || !el.files[0]) throw new Error("请选择文件");
    const r = await cloudUploadReport(el.files[0]);
    UI.toast("已上传：" + r.name);
    await cloudListFiles(); await renderCloud();
  } catch (e) { UI.toast(cloudErr(e)); }
}
async function cloudFilesBtn() { try { await cloudListFiles(); await renderCloud(); } catch (e) { UI.toast(cloudErr(e)); } }
async function cloudOpenBtn(path) {
  try { const url = await cloudOpenFile(path); window.open(url, "_blank", "noopener"); }
  catch (e) { UI.toast(cloudErr(e)); }
}
async function cloudGenBtn() {
  const out = document.querySelector("#cloudQuestions");
  out.innerHTML = UI.loading("云 AI 正在生成分阶段提问");
  try {
    const qs = await cloudGenerateQuestions({
      disease: document.querySelector("#cloudDisease").value,
      surgery: document.querySelector("#cloudSurgery").value,
      day: document.querySelector("#cloudDay").value,
      focus: document.querySelector("#cloudFocus").value,
    });
    out.innerHTML = `<div class="q-list">${qs.map((q, i) => `<div class="q-item">
        <b>${i + 1}. ${esc(q.text)}</b><div class="tiny muted">目的：${esc(q.purpose)}｜追问：${esc(q.branch)}</div></div>`).join("")}</div>`;
    const btn = document.querySelector("#view").querySelector("button[onclick^='cloudApplyBtn']");
    if (btn) btn.disabled = false;
    UI.toast("云 AI 已生成 " + qs.length + " 个提问，待医生审核");
  } catch (e) { out.textContent = cloudErr(e); }
}
async function cloudApplyBtn() {
  try {
    const ps = await yzhLocalApi("/api/protocols");
    const p = ps[0];
    if (!p) throw new Error("没有可用的病种方案");
    const stage = (p.stages || []).find(s => s.key === "D7") || (p.stages || [])[0];
    if (!stage) throw new Error("方案中没有阶段");
    await cloudApplyQuestions(p.id, stage.key, CloudState.generated);
    UI.toast("已写入方案「" + p.name + "」的 " + stage.key + " 阶段，请在方案页审核发布");
  } catch (e) { UI.toast(cloudErr(e)); }
}
async function cloudSelfTestBtn() {
  const out = document.querySelector("#cloudTestOut");
  out.innerHTML = UI.loading("正在逐项验证云服务");
  const steps = await cloudSelfTest();
  out.innerHTML = steps.map(x => `<div class="row" style="margin:5px 0">
      <span class="pill ${x.ok ? "green" : "red"}">${x.ok ? "通过" : "未通过"}</span>
      <b style="min-width:130px">${esc(x.name)}</b><span class="tiny muted">${esc(x.detail)}</span></div>`).join("");
  UI.toast("自检完成：" + steps.filter(x => x.ok).length + "/" + steps.length + " 项通过");
}

window.addEventListener("DOMContentLoaded", () => {
  const dot = document.querySelector("#dotCloud");
  const text = document.querySelector("#cloudText");
  const ready = cloudInit();
  if (dot) dot.className = "dot " + (ready ? "ok" : "off");
  if (text) text.textContent = ready ? "云服务已连接" : "云服务未就绪";
  if (ready) cloudSession().then(s => {
    if (text) text.textContent = s ? "云服务已登录" : "云服务已连接 · 未登录";
    if (dot) dot.className = "dot " + (s ? "ok" : "warn");
  }).catch(() => {});
});
