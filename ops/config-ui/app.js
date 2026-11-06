"use strict";

let data = null;
let fields = [];
let values = {};
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
  const r = await fetch("/api/config", { credentials: "same-origin" });
  if (r.status === 401) { banner("会话失效：请用启动日志里带 ?token= 的 URL 重新打开。", true); return; }
  const d = await r.json();
  if (!d.ok) { banner(d.error || "读取失败", true); return; }
  data = d;
  render();
}

function collect() {
  const out = {};
  for (const f of fields) out[f.id] = values[f.id];
  return out;
}

async function post(payload) {
  const r = await fetch("/api/config", {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json().catch(() => ({ ok: false, error: "响应不是 JSON" }));
  if (r.status === 401) { banner("会话失效：请用带 token 的 URL 重新打开。", true); }
  return d;
}

async function preview() {
  const d = await post({ values: collect(), dry_run: true });
  if (!d.ok) {
    banner((d.errors || [d.error || "校验失败"]).join("；"), true);
    toast("校验失败，未保存", true);
    return;
  }
  if (!d.changed) { toast("没有改动"); return; }
  pending = collect();
  $("#diff").textContent = d.diff;
  $("#diff-note").textContent = "保存前会备份当前 config.yaml；保存后需要重启 nonebot 才生效。";
  const dlg = $("#diff-dialog");
  if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
}

async function confirmSave() {
  if (!pending) return;
  const d = await post({ values: pending });
  const dlg = $("#diff-dialog");
  if (dlg.open) dlg.close();
  if (!d.ok) {
    banner((d.errors || [d.error || "保存失败"]).join("；"), true);
    toast("保存失败", true);
    return;
  }
  if (d.unchanged) { toast("没有改动"); pending = null; return; }
  banner("已保存。备份：" + (d.backup || "无旧文件") + "。生效需要：" + d.restart_cmd);
  toast("已保存，重启 nonebot 后生效");
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

$("#save").addEventListener("click", preview);
$("#reload").addEventListener("click", load);
$("#search").addEventListener("input", applyFilter);
$("#only-changed").addEventListener("change", applyFilter);
$("#confirm-save").addEventListener("click", (ev) => { ev.preventDefault(); confirmSave(); });
$("#diff-dialog").addEventListener("close", () => { pending = null; });

load().catch((e) => banner("读取失败：" + e, true));
