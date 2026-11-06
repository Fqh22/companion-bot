"use strict";

let data = null;
let assets = null;
let fields = [];
let values = {};
let personaDrafts = {};
let pending = null;

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.className = "toast hidden"; }, 4200);
}

function banner(msg, isError = false) {
  const b = $("#banner");
  if (!msg) { b.className = "banner hidden"; return; }
  b.textContent = msg;
  b.className = "banner" + (isError ? " error" : "");
}

function isChanged(f, v) {
  if (v === undefined) return false;
  if (typeof v === "number" && typeof f.default === "number") return Number(v) !== Number(f.default);
  if (Array.isArray(v) && Array.isArray(f.default)) return JSON.stringify(v) !== JSON.stringify(f.default);
  return v !== f.default;
}

function fmt(v) {
  if (Array.isArray(v)) return v.length ? v.join(", ") : "[]";
  if (v === true) return "true";
  if (v === false) return "false";
  return String(v);
}

function makeControl(f) {
  let c;
  if (f.type === "bool") {
    c = el("input"); c.type = "checkbox"; c.checked = !!values[f.id];
  } else if (f.options && f.options.length) {
    c = el("select");
    for (const o of f.options) {
      const op = el("option", "", o); op.value = o; c.appendChild(op);
    }
    c.value = values[f.id];
  } else if (f.type === "int" || f.type === "float") {
    c = el("input"); c.type = "number";
    if (f.min !== undefined) c.min = f.min;
    if (f.max !== undefined) c.max = f.max;
    c.step = f.step !== undefined ? f.step : (f.type === "int" ? "1" : "any");
    c.value = values[f.id];
  } else if (f.type === "list") {
    c = el("textarea"); c.rows = 3;
    c.value = (values[f.id] || []).join("\n");
  } else {
    c = el("input"); c.type = "text"; c.value = values[f.id] ?? "";
  }
  c.dataset.fid = f.id;
  c.addEventListener("input", () => onEdit(f, c));
  c.addEventListener("change", () => onEdit(f, c));
  return c;
}

function readControl(f, c) {
  if (f.type === "bool") return !!c.checked;
  if (f.options && f.options.length) return c.value;
  if (f.type === "int") return c.value === "" ? null : parseInt(c.value, 10);
  if (f.type === "float") return c.value === "" ? null : parseFloat(c.value);
  if (f.type === "list") {
    const lines = c.value.split("\n").map((x) => x.trim()).filter(Boolean);
    return f.list === "int" ? lines.map((x) => parseInt(x, 10)).filter((x) => !Number.isNaN(x)) : lines;
  }
  return c.value;
}

function onEdit(f, c) {
  values[f.id] = readControl(f, c);
  const box = c.closest(".field");
  const dirty = isChanged(f, values[f.id]);
  box.classList.toggle("dirty", dirty);
  const badge = box.querySelector(".badge");
  if (badge) {
    badge.textContent = dirty ? "已修改" : (f.source === "default" ? "用默认值" : "当前值");
    badge.classList.toggle("changed", dirty);
  }
  updateButtons();
  applyFilter();
}

function resetField(f, c) {
  values[f.id] = f.default;
  if (f.type === "bool") c.checked = !!f.default;
  else if (f.options && f.options.length) c.value = f.default;
  else if (f.type === "list") c.value = (f.default || []).join("\n");
  else c.value = f.default;
  onEdit(f, c);
}

function updateButtons() {
  const dirty = fields.some((f) => isChanged(f, values[f.id]));
  $("#save").disabled = !dirty;
}

function buildField(f) {
  const box = el("div", "field");
  box.dataset.id = f.id;
  const left = el("div", "field-label");
  const name = el("div", "name");
  name.appendChild(document.createTextNode(f.label));
  const badge = el("span", "badge", f.changed ? "当前值" : (f.source === "default" ? "用默认值" : "当前值"));
  if (isChanged(f, values[f.id])) badge.classList.add("changed");
  name.appendChild(badge);
  left.appendChild(name);
  left.appendChild(el("span", "id", f.id));
  const right = el("div", "field-control");
  const c = makeControl(f);
  right.appendChild(c);
  const foot = el("div", "field-foot");
  foot.appendChild(document.createTextNode("默认：" + fmt(f.default)));
  if (f.desc) foot.appendChild(document.createTextNode(" · " + f.desc));
  const reset = el("button", "reset", "恢复默认");
  reset.type = "button";
  reset.addEventListener("click", () => resetField(f, c));
  foot.appendChild(reset);
  right.appendChild(foot);
  box.appendChild(left); box.appendChild(right);
  return box;
}


function dialogsFrom(text) {
  const lines = (text || "").split("\n").map((x) => x.trim());
  while (lines.length && !lines[lines.length - 1]) lines.pop();
  return lines.filter((x) => x.length > 0);
}

function personaCard(p) {
  const card = el("div", "asset-card");
  const head = el("div", "asset-head");
  const title = el("h3", "", p.meta && p.meta.title ? p.label + " · " + p.meta.title : p.label + "人格");
  head.appendChild(title);
  const meta = el("div", "asset-meta", p.path + " · " + (p.exists ? "修改于 " + p.mtime_text : "文件不存在"));
  head.appendChild(meta);
  card.appendChild(head);
  if (p.error) {
    card.appendChild(el("div", "banner error", "读取失败：" + p.error));
    return card;
  }
  if (!personaDrafts[p.which]) {
    personaDrafts[p.which] = {
      system_prompt: p.system_prompt || "",
      begin_dialogs: Array.isArray(p.begin_dialogs) ? p.begin_dialogs.slice() : [],
    };
  }
  const draft = personaDrafts[p.which];

  const l1 = el("label", "asset-label", "system_prompt（人格正文）");
  const ta = el("textarea"); ta.rows = 12; ta.value = draft.system_prompt;
  ta.spellcheck = false;
  const counter = el("div", "asset-meta", draft.system_prompt.length + " 字");
  ta.addEventListener("input", () => {
    draft.system_prompt = ta.value;
    counter.textContent = ta.value.length + " 字";
  });
  card.appendChild(l1); card.appendChild(ta); card.appendChild(counter);

  const l2 = el("label", "asset-label", "begin_dialogs（示例对话，一行一条，user / assistant 交替）");
  const td = el("textarea"); td.rows = 6; td.value = (draft.begin_dialogs || []).join("\n");
  td.spellcheck = false;
  const parity = el("div", "asset-meta");
  const refresh = () => {
    draft.begin_dialogs = dialogsFrom(td.value);
    const n = draft.begin_dialogs.length;
    parity.textContent = n + " 条" + (n % 2 ? "（奇数会拒绝保存）" : "（成对）");
    parity.className = "asset-meta" + (n % 2 ? " warn" : "");
  };
  td.addEventListener("input", refresh); refresh();
  card.appendChild(l2); card.appendChild(td); card.appendChild(parity);

  const actions = el("div", "asset-actions");
  const btn = el("button", "primary", "预览并保存");
  btn.type = "button";
  btn.addEventListener("click", () => previewPersona(p.which));
  actions.appendChild(btn);
  actions.appendChild(el("span", "asset-meta",
    p.which === "owner" || p.which === "stranger" ? "保存后需在 QQ 发 /reload_persona" : ""));
  card.appendChild(actions);
  return card;
}

function renderPersonas() {
  const box = $("#personas");
  box.textContent = "";
  if (!assets) { box.appendChild(el("p", "loading", "读取中…")); return; }
  for (const p of assets.personas || []) box.appendChild(personaCard(p));
}

function renderWatch() {
  const box = $("#watch-edit");
  const base = $("#watch-baseline");
  box.textContent = ""; base.textContent = "";
  if (!assets) return;
  const w = assets.watchlist || {};
  if (w.error) {
    box.appendChild(el("div", "banner error", "读取失败：" + w.error));
    return;
  }
  const topics = (w.topics || []).map((x) => x.topic);
  const ta = el("textarea"); ta.rows = 6; ta.value = topics.join("\n");
  ta.spellcheck = false;
  const note = el("div", "asset-meta", topics.length + " / " + (w.max || 8) + " 个话题 · " + w.path +
    " · " + (w.exists ? "修改于 " + w.mtime_text : "文件不存在（保存时创建）"));
  const upd = () => {
    const n = ta.value.split("\n").map((x) => x.trim()).filter(Boolean).length;
    note.textContent = n + " / " + (w.max || 8) + " 个话题 · " + w.path +
      " · " + (w.exists ? "修改于 " + w.mtime_text : "文件不存在（保存时创建）");
    note.className = "asset-meta" + (n > (w.max || 8) ? " warn" : "");
  };
  ta.addEventListener("input", upd);
  box.appendChild(ta); box.appendChild(note);
  const actions = el("div", "asset-actions");
  const btn = el("button", "primary", "预览并保存");
  btn.type = "button";
  btn.addEventListener("click", () => previewWatch(ta.value));
  actions.appendChild(btn);
  actions.appendChild(el("span", "asset-meta", "免重启，下一次巡检生效；同名话题保留 last 基线"));
  box.appendChild(actions);

  if ((w.topics || []).length) {
    const table = el("table");
    for (const it of w.topics) {
      const tr = el("tr");
      tr.appendChild(el("td", "", it.topic));
      tr.appendChild(el("td", "", it.last ? it.last : "（还没有基线）"));
      table.appendChild(tr);
    }
    base.appendChild(el("p", "desc", "当前基线（保存时同名话题会保留）："));
    base.appendChild(table);
  }
}

function renderAssets() {
  renderPersonas();
  renderWatch();
}

function render() {
  fields = [];
  values = {};
  const nav = $("#nav");
  const groups = $("#groups");
  nav.textContent = ""; groups.textContent = "";
  for (const g of data.groups) {
    const a = el("a", "", g.label); a.href = "#group-" + g.id; nav.appendChild(a);
    const sec = el("section", "group"); sec.id = "group-" + g.id;
    sec.appendChild(el("h2", "", g.label));
    if (g.desc) sec.appendChild(el("p", "desc", g.desc));
    for (const f of g.fields) {
      fields.push(f);
      values[f.id] = f.value;
      sec.appendChild(buildField(f));
    }
    groups.appendChild(sec);
  }
  const ap = el("a", "", "人格"); ap.href = "#persona-wrap"; nav.appendChild(ap);
  const aw = el("a", "", "巡检话题"); aw.href = "#watch-wrap"; nav.appendChild(aw);
  renderAssets();
  const unknown = data.unknown || [];
  const wrap = $("#unknown-wrap");
  if (unknown.length) {
    wrap.classList.remove("hidden");
    const box = $("#unknown"); box.textContent = "";
    const table = el("table");
    for (const u of unknown) {
      const tr = el("tr");
      tr.appendChild(el("td", "", u.id));
      tr.appendChild(el("td", "", typeof u.value === "object" ? JSON.stringify(u.value) : String(u.value)));
      table.appendChild(tr);
    }
    box.appendChild(table);
  } else {
    wrap.classList.add("hidden");
  }
  const s = data.service;
  let status = data.path + " · " + (data.exists ? ("修改于 " + data.mtime_text) : "文件不存在（保存时创建）");
  if (s) {
    status += " · 服务 " + s.state + (s.since ? "（" + s.since + "）" : "");
    if (s.stale) status += " · ⚠ 配置比服务新，需要重启";
  }
  $("#status").innerHTML = "";
  const span = el("span", "", status);
  if (s && s.stale) span.className = "warn";
  $("#status").appendChild(span);
  $("#loading").classList.add("hidden");
  updateButtons();
  applyFilter();
}

async function load() {
  banner("");
  const [rc, ra] = await Promise.all([
    fetch("/api/config", { credentials: "same-origin" }),
    fetch("/api/assets", { credentials: "same-origin" }),
  ]);
  if (rc.status === 401 || ra.status === 401) {
    banner("会话失效：请用启动日志里带 ?token= 的 URL 重新打开。", true);
    return;
  }
  const d = await rc.json();
  const a = await ra.json().catch(() => ({ ok: false, personas: [], watchlist: {} }));
  if (!d.ok) { banner(d.error || "读取失败", true); return; }
  data = d;
  assets = a.ok ? a : { personas: [], watchlist: {} };
  personaDrafts = {};
  render();
}

function collect() {
  const out = {};
  for (const f of fields) out[f.id] = values[f.id];
  return out;
}

async function postTo(url, payload) {
  const r = await fetch(url, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json().catch(() => ({ ok: false, error: "响应不是 JSON" }));
  if (r.status === 401) { banner("会话失效：请用带 token 的 URL 重新打开。", true); }
  return d;
}

function showDiff(d, url, body, note) {
  if (!d.ok) {
    banner((d.errors || [d.error || "校验失败"]).join("；"), true);
    toast("校验失败，未保存", true);
    return;
  }
  if (!d.changed) { toast("没有改动"); return; }
  pending = { url, body, note };
  $("#diff").textContent = d.diff;
  $("#diff-note").textContent = note;
  const dlg = $("#diff-dialog");
  if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
}

async function previewConfig() {
  const body = { values: collect() };
  const d = await postTo("/api/config", { ...body, dry_run: true });
  showDiff(d, "/api/config", body, "保存前备份 config.yaml；保存后需要重启 nonebot 才生效。");
}

async function previewPersona(which) {
  const draft = personaDrafts[which];
  if (!draft) return;
  if (draft.begin_dialogs.length % 2) {
    toast("示例对话必须是偶数条（user / assistant 成对）", true);
    return;
  }
  const body = {
    which,
    system_prompt: draft.system_prompt,
    begin_dialogs: draft.begin_dialogs,
  };
  const d = await postTo("/api/persona", { ...body, dry_run: true });
  showDiff(d, "/api/persona", body,
    "保存前备份人格文件；保存后在 QQ 发 /reload_persona（或重启 nonebot）才生效。");
}

async function previewWatch(text) {
  const topics = (text || "").split("\n").map((x) => x.trim()).filter(Boolean);
  const d = await postTo("/api/watchlist", { topics, dry_run: true });
  showDiff(d, "/api/watchlist", { topics },
    "保存前备份 watchlist.json；免重启，下一次巡检读取新文件。同名话题保留 last 基线。");
}

async function confirmSave() {
  if (!pending) return;
  const d = await postTo(pending.url, pending.body);
  const dlg = $("#diff-dialog");
  if (dlg.open) dlg.close();
  if (!d.ok) {
    banner((d.errors || [d.error || "保存失败"]).join("；"), true);
    toast("保存失败", true);
    return;
  }
  if (d.unchanged) { toast("没有改动"); pending = null; return; }
  const backup = d.backup || "无旧文件";
  if (pending.url === "/api/config") {
    banner("已保存。备份：" + backup + "。生效需要：" + d.restart_cmd);
    toast("已保存，重启 nonebot 后生效");
  } else if (pending.url === "/api/persona") {
    banner("已保存。备份：" + backup + "。生效：" + d.reload_cmd);
    toast("已保存，/reload_persona 后生效");
  } else {
    banner("已保存。备份：" + backup + "。" + (d.note || "免重启"));
    toast("已保存，下一次巡检生效");
  }
  pending = null;
  await load();
}

function applyFilter() {
  const q = ($("#search").value || "").trim().toLowerCase();
  const only = $("#only-changed").checked;
  for (const f of fields) {
    const box = document.querySelector('.field[data-id="' + CSS.escape(f.id) + '"]');
    if (!box) continue;
    const text = (f.label + " " + f.id + " " + (f.desc || "")).toLowerCase();
    let show = !q || text.includes(q);
    if (only) show = show && isChanged(f, values[f.id]);
    box.classList.toggle("hidden", !show);
  }
  for (const g of data.groups) {
    const sec = document.getElementById("group-" + g.id);
    if (!sec) continue;
    const any = [...sec.querySelectorAll(".field")].some((x) => !x.classList.contains("hidden"));
    sec.classList.toggle("hidden", !any);
  }
}

$("#save").addEventListener("click", previewConfig);
$("#reload").addEventListener("click", load);
$("#search").addEventListener("input", applyFilter);
$("#only-changed").addEventListener("change", applyFilter);
$("#confirm-save").addEventListener("click", (ev) => { ev.preventDefault(); confirmSave(); });
$("#diff-dialog").addEventListener("close", () => { pending = null; });

load().catch((e) => banner("读取失败：" + e, true));
