"use strict";
// ---------- Utilitaires ----------
const $ = (s) => document.querySelector(s);
const getCookie = (n) => document.cookie.split("; ").find((c) => c.startsWith(n + "="))?.split("=")[1] || "";

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null) el.append(k.nodeType ? k : document.createTextNode(k));
  return el;
}

let toastTimer;
function toast(msg, type = "ok") {
  const t = $("#toast");
  t.textContent = msg; t.className = `show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), 3200);
}

async function api(path, method = "GET", body) {
  const r = await fetch("/api" + path, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": decodeURIComponent(getCookie("csrf")) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401) { location.href = "/login"; throw new Error("unauthenticated"); }
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    const detail = Array.isArray(d.detail) ? d.detail.map((e) => e.msg).join("; ") : d.detail;
    throw new Error(detail || `Erreur ${r.status}`);
  }
  return r.status === 204 ? null : r.json();
}
const safe = (fn) => async (...a) => { try { await fn(...a); } catch (e) { if (e.message !== "unauthenticated") toast(e.message, "err"); } };

// ---------- Chaîne active ----------
let CH = null;                         // id de la chaîne sélectionnée
let channels = [];
const ch = (p) => `/channels/${CH}${p}`;

// ---------- Onglets ----------
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll("main > section").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + name));
}
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
$("#logout-btn").addEventListener("click", async () => { await fetch("/api/auth/logout", { method: "POST" }); location.href = "/login"; });

// ---------- Modules ----------
// Description des formulaires de configuration (chemins pointés dans la config JSON)
const F = (path, label, type, extra = {}) => ({ path, label, type, ...extra });
const SCHEMAS = {
  protect: [
    { title: "Liste noire", fields: [F("blacklist", "Mots / phrases (un par ligne, préfixe « re: » pour une regex)", "blacklist")] },
    { title: "Majuscules", fields: [F("caps.enabled", "Activer", "bool"), F("caps.min_length", "Longueur min. (lettres)", "int"), F("caps.max_percent", "% de majuscules max", "int")] },
    { title: "Répétitions", fields: [F("repeat.enabled", "Activer", "bool"), F("repeat.max_chars", "Répétitions max d'un caractère", "int")] },
    { title: "Émotes", fields: [F("emotes.enabled", "Activer", "bool"), F("emotes.max_emotes", "Émotes max par message", "int")] },
    { title: "Liens", fields: [F("links.enabled", "Bloquer les liens", "bool"), F("links.whitelist", "Domaines autorisés (un par ligne)", "lines")] },
    { title: "Exemptions", fields: [F("exempt_mods", "Modérateurs", "bool"), F("exempt_vips", "VIP", "bool"), F("exempt_subs", "Abonnés", "bool")] },
    { title: "Sanctions progressives", fields: [
      F("steps", "Une étape par ligne : warn | delete | timeout:SECONDES | ban", "steps"),
      F("reset_minutes", "Remise à zéro des infractions (min)", "int"),
      F("warn_message", "Message d'avertissement ({user}, {reason})", "text")] },
  ],
  commands: [{ title: "Général", fields: [F("prefix", "Préfixe des commandes", "text", { max: 3 })] }],
  timers: [{ title: "Général", fields: [F("only_when_live", "Annoncer uniquement quand le stream est en direct", "bool")] }],
  logs: [{ title: "Général", fields: [F("log_chat", "Journaliser tous les messages du chat", "bool"), F("retention_days", "Rétention (jours)", "int")] }],
};

const getPath = (o, p) => p.split(".").reduce((a, k) => a?.[k], o);
function setPath(o, p, v) {
  const ks = p.split("."); const last = ks.pop();
  ks.reduce((a, k) => a[k], o)[last] = v;
}

const CODECS = {
  lines: { enc: (v) => (v || []).join("\n"), dec: (s) => s.split("\n").map((x) => x.trim()).filter(Boolean) },
  blacklist: {
    enc: (v) => (v || []).map((e) => (e.regex ? "re:" : "") + e.pattern).join("\n"),
    dec: (s) => s.split("\n").map((x) => x.trim()).filter(Boolean)
      .map((l) => (l.startsWith("re:") ? { pattern: l.slice(3), regex: true } : { pattern: l, regex: false })),
  },
  steps: {
    enc: (v) => (v || []).map((s) => (s.action === "timeout" ? `timeout:${s.duration}` : s.action)).join("\n"),
    dec: (s) => s.split("\n").map((x) => x.trim().toLowerCase()).filter(Boolean).map((l) => {
      const [action, d] = l.split(":");
      return action === "timeout" ? { action, duration: parseInt(d || "60", 10) } : { action, duration: 60 };
    }),
  },
};

let uid = 0;
function buildField(f, value) {
  const id = `f${++uid}`;
  if (f.type === "bool") {
    const input = h("input", { type: "checkbox", id, class: "chk" });
    input.checked = !!value;
    return { el: h("div", { class: "row" }, h("label", { for: id }, f.label), input), read: () => input.checked };
  }
  if (f.type === "int" || f.type === "text") {
    const input = h("input", { type: f.type === "int" ? "number" : "text", id, maxlength: f.max });
    input.value = value ?? "";
    return { el: h("div", { class: "field" }, h("label", { for: id }, f.label), input),
             read: () => (f.type === "int" ? parseInt(input.value, 10) : input.value) };
  }
  const codec = CODECS[f.type];
  const ta = h("textarea", { id, rows: 4, spellcheck: "false" });
  ta.value = codec.enc(value);
  return { el: h("div", { class: "field" }, h("label", { for: id }, f.label), ta), read: () => codec.dec(ta.value) };
}

function renderModule(mod) {
  const readers = [];
  const groups = (SCHEMAS[mod.name] || []).map((g) => h("div", { class: "group" },
    h("div", { class: "gtitle" }, g.title),
    g.fields.map((f) => { const b = buildField(f, getPath(mod.config, f.path)); readers.push([f.path, b.read]); return b.el; })));

  const toggle = h("input", { type: "checkbox", id: `toggle-${mod.name}`, "aria-label": `Activer ${mod.label}` });
  toggle.checked = mod.enabled;
  toggle.addEventListener("change", safe(async () => {
    try { await api(ch(`/modules/${mod.name}/enabled`), "PUT", { enabled: toggle.checked }); toast(`${mod.label} ${toggle.checked ? "activé" : "désactivé"}`); }
    catch (e) { toggle.checked = !toggle.checked; throw e; }
  }));

  const save = h("button", { class: "btn", id: `save-${mod.name}`, onclick: safe(async () => {
    const cfg = structuredClone(mod.config);
    readers.forEach(([p, read]) => setPath(cfg, p, read()));
    const upd = await api(ch(`/modules/${mod.name}/config`), "PUT", cfg);
    mod.config = upd.config;
    toast("Configuration enregistrée");
  }) }, "Enregistrer");

  const card = h("article", { class: "card" + (mod.enabled ? "" : " off"), id: `module-${mod.name}` },
    h("div", { class: "card-head" },
      h("div", {}, h("h3", {}, mod.label), h("p", { class: "desc" }, mod.description)),
      h("label", { class: "switch" }, toggle, h("span", { class: "slider" }))),
    h("div", { class: "config" }, groups, h("div", {}, save)));
  card._toggle = toggle;
  return card;
}

const moduleCards = {};
async function loadModules() {
  const grid = $("#modules-grid");
  grid.replaceChildren();
  for (const k of Object.keys(moduleCards)) delete moduleCards[k];
  for (const mod of await api(ch("/modules"))) {
    const card = renderModule(mod);
    moduleCards[mod.name] = card;
    grid.append(card);
  }
}
function syncModule(mod) {  // changement reçu en temps réel
  if (mod.channel_id !== CH) return;
  const card = moduleCards[mod.name];
  if (!card) return;
  card._toggle.checked = mod.enabled;
  card.classList.toggle("off", !mod.enabled);
}

// ---------- Commandes ----------
const PERMS = { everyone: "Tous", vip: "VIP+", mod: "Mods", broadcaster: "Streamer" };
async function loadCommands() {
  const rows = await api(ch("/commands"));
  $("#commands-body").replaceChildren(...rows.map((c) => h("tr", {},
    h("td", {}, h("code", { class: "cmd" }, "!" + c.name)),
    h("td", {}, c.response.length > 60 ? c.response.slice(0, 60) + "…" : c.response),
    h("td", {}, PERMS[c.permission]), h("td", {}, `${c.cooldown_global}/${c.cooldown_user}s`), h("td", {}, c.uses),
    h("td", { class: "actions" },
      h("button", { class: "btn ghost sm", onclick: () => fillCommand(c) }, "Éditer"),
      h("button", { class: "btn danger sm", onclick: safe(async () => {
        if (!confirm(`Supprimer !${c.name} ?`)) return;
        await api(ch(`/commands/${c.id}`), "DELETE"); toast("Commande supprimée"); loadCommands();
      }) }, "Suppr.")))));
}
function fillCommand(c = {}) {
  $("#cmd-id").value = c.id || "";
  $("#cmd-name").value = c.name || ""; $("#cmd-response").value = c.response || "";
  $("#cmd-permission").value = c.permission || "everyone";
  $("#cmd-cd-global").value = c.cooldown_global ?? 5; $("#cmd-cd-user").value = c.cooldown_user ?? 15;
  $("#cmd-enabled").checked = c.enabled ?? true;
  $("#command-form-title").textContent = c.id ? `Modifier !${c.name}` : "Nouvelle commande";
}
$("#cmd-reset").addEventListener("click", () => fillCommand());
$("#command-form").addEventListener("submit", safe(async (e) => {
  e.preventDefault();
  const id = $("#cmd-id").value;
  await api(id ? ch(`/commands/${id}`) : ch("/commands"), id ? "PUT" : "POST", {
    name: $("#cmd-name").value.trim().replace(/^!/, ""), response: $("#cmd-response").value,
    permission: $("#cmd-permission").value, enabled: $("#cmd-enabled").checked,
    cooldown_global: parseInt($("#cmd-cd-global").value || "0", 10), cooldown_user: parseInt($("#cmd-cd-user").value || "0", 10),
  });
  toast("Commande enregistrée"); fillCommand(); loadCommands();
}));

// ---------- Timers ----------
async function loadTimers() {
  const rows = await api(ch("/timers"));
  $("#timers-body").replaceChildren(...rows.map((t) => h("tr", {},
    h("td", {}, t.name + (t.enabled ? "" : " (off)")),
    h("td", {}, t.message.length > 50 ? t.message.slice(0, 50) + "…" : t.message),
    h("td", {}, `${t.interval_minutes} min`), h("td", {}, t.min_messages),
    h("td", { class: "actions" },
      h("button", { class: "btn ghost sm", onclick: () => fillTimer(t) }, "Éditer"),
      h("button", { class: "btn danger sm", onclick: safe(async () => {
        if (!confirm(`Supprimer « ${t.name} » ?`)) return;
        await api(ch(`/timers/${t.id}`), "DELETE"); toast("Timer supprimé"); loadTimers();
      }) }, "Suppr.")))));
}
function fillTimer(t = {}) {
  $("#tm-id").value = t.id || ""; $("#tm-name").value = t.name || ""; $("#tm-message").value = t.message || "";
  $("#tm-interval").value = t.interval_minutes ?? 15; $("#tm-min").value = t.min_messages ?? 5;
  $("#tm-enabled").checked = t.enabled ?? true;
  $("#timer-form-title").textContent = t.id ? `Modifier « ${t.name} »` : "Nouveau timer";
}
$("#tm-reset").addEventListener("click", () => fillTimer());
$("#timer-form").addEventListener("submit", safe(async (e) => {
  e.preventDefault();
  const id = $("#tm-id").value;
  await api(id ? ch(`/timers/${id}`) : ch("/timers"), id ? "PUT" : "POST", {
    name: $("#tm-name").value, message: $("#tm-message").value, enabled: $("#tm-enabled").checked,
    interval_minutes: parseInt($("#tm-interval").value, 10), min_messages: parseInt($("#tm-min").value || "0", 10),
  });
  toast("Timer enregistré"); fillTimer(); loadTimers();
}));

// ---------- Logs & temps réel ----------
const MAX_LOGS = 300;
function logRow(l) {
  const time = new Date(l.ts).toLocaleTimeString("fr-FR");
  return h("div", { class: "log-item", "data-cat": l.category },
    h("span", { class: "ts" }, time), h("span", { class: `badge ${l.category}` }, l.category), h("span", {}, l.message));
}
function applyFilter() {
  const f = $("#log-filter").value;
  document.querySelectorAll(".log-item").forEach((r) => r.classList.toggle("hidden", !!f && r.dataset.cat !== f));
}
async function loadLogs() {
  const rows = await api(ch("/logs?limit=150"));
  $("#log-list").replaceChildren(...rows.map(logRow));
  applyFilter();
}
$("#log-filter").addEventListener("change", applyFilter);
$("#log-clear").addEventListener("click", () => $("#log-list").replaceChildren());

function setStatus(s) {
  const labels = { connected: "Twitch connecté", connecting: "Connexion…", error: "Erreur Twitch", not_configured: "Twitch non configuré" };
  const p = $("#status-pill");
  p.textContent = `${labels[s.twitch] || s.twitch}${s.bot ? " · " + s.bot : ""}`;
  p.className = `pill ${s.twitch}`;
}

// ---------- Chaînes ----------
function renderChannelSelect() {
  const sel = $("#channel-select");
  sel.replaceChildren(...channels.map((c) => h("option", { value: c.id }, "#" + c.login + (c.enabled ? "" : " (off)"))));
  sel.classList.toggle("hidden", channels.length === 0);
  document.querySelectorAll(".ch-tab").forEach((t) => t.classList.toggle("hidden", channels.length === 0));
  if (CH != null) sel.value = String(CH);
}

function renderChannelsTable() {
  $("#channels-empty").classList.toggle("hidden", channels.length > 0);
  $("#channels-body").replaceChildren(...channels.map((c) => {
    const toggle = h("input", { type: "checkbox", "aria-label": `Activer #${c.login}` });
    toggle.checked = c.enabled;
    toggle.addEventListener("change", safe(async () => {
      try { await api(`/channels/${c.id}/enabled`, "PUT", { enabled: toggle.checked }); }
      catch (e) { toggle.checked = !toggle.checked; throw e; }
      await loadChannels();
    }));
    const state = !c.enabled ? "désactivée" : c.joined ? "connectée" : "en attente";
    return h("tr", {},
      h("td", {}, h("code", { class: "cmd" }, "#" + c.login)),
      h("td", {}, h("span", { class: `pill ${c.joined ? "connected" : ""}` }, state)),
      h("td", {}, h("label", { class: "switch" }, toggle, h("span", { class: "slider" }))),
      h("td", { class: "actions" },
        h("button", { class: "btn ghost sm", onclick: () => { selectChannel(c.id); showTab("modules"); } }, "Configurer"),
        h("button", { class: "btn danger sm", onclick: safe(async () => {
          if (!confirm(`Retirer #${c.login} ? Sa configuration, ses commandes, timers et logs seront supprimés.`)) return;
          await api(`/channels/${c.id}`, "DELETE"); toast("Chaîne retirée"); await loadChannels();
        }) }, "Retirer")));
  }));
}

async function loadChannels() {
  channels = await api("/channels");
  if (!channels.some((c) => c.id === CH)) CH = channels[0]?.id ?? null;
  renderChannelSelect();
  renderChannelsTable();
  if (CH != null) await loadChannelData();
}

async function loadChannelData() {
  await Promise.all([loadModules(), loadCommands(), loadTimers(), loadLogs()]);
  fillCommand(); fillTimer();
}

const selectChannel = safe(async (id) => {
  CH = Number(id);
  $("#channel-select").value = String(CH);
  await loadChannelData();
});
$("#channel-select").addEventListener("change", (e) => selectChannel(e.target.value));

$("#channel-form").addEventListener("submit", safe(async (e) => {
  e.preventDefault();
  const c = await api("/channels", "POST", { login: $("#ch-login").value.trim() });
  $("#ch-login").value = "";
  CH = c.id;
  toast(`#${c.login} ajoutée`);
  await loadChannels();
}));

function connectSSE() {
  const es = new EventSource("/api/events");
  const live = $("#live-pill");
  es.onopen = () => { live.className = "pill connected"; };
  es.onerror = () => { live.className = "pill error"; };
  es.addEventListener("log", (e) => {
    const l = JSON.parse(e.data);
    if (l.channel_id != null && l.channel_id !== CH) return;   // autre chaîne
    const row = logRow(l);
    const list = $("#log-list");
    list.prepend(row);
    while (list.children.length > MAX_LOGS) list.lastChild.remove();
    applyFilter();
    if (l.category === "command") loadCommands();
  });
  es.addEventListener("module", (e) => syncModule(JSON.parse(e.data)));
  es.addEventListener("status", (e) => { setStatus(JSON.parse(e.data)); loadChannelsList(); });
  es.addEventListener("channels", () => loadChannelsList());
}

// Rafraîchit uniquement la liste (pas les données de la chaîne) : état "connectée" etc.
const loadChannelsList = safe(async () => {
  channels = await api("/channels");
  renderChannelSelect();
  renderChannelsTable();
});

// ---------- Démarrage ----------
(safe(async () => {
  await api("/auth/me");
  setStatus(await api("/status"));
  await loadChannels();
  connectSSE();
}))();
