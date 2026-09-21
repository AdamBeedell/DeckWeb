// app.js — sign-in, lists, sidebar, menus and dialogs. Drawing lives in chart.js.
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  key: localStorage.getItem("deckweb.key"),
  user: null, lists: [], data: null, sel: null,
  opts: { mode: "circle", stack: true, dup: false, quickTag: null },
};

// ------------------------------------------------------------------ API
async function api(path, { method = "GET", body, raw = false } = {}) {
  const res = await fetch(path, {
    method, headers: { "X-API-Key": state.key || "", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && path !== "/api/login") { signOut(); throw new Error("Signed out"); }
  if (raw) { if (!res.ok) throw new Error((await res.json()).error); return res; }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(data.error || `Request failed (${res.status})`); e.data = data; throw e; }
  return data;
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg; t.classList.toggle("error", isError); t.classList.add("show");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove("show"), 3500);
}
const fail = e => e.message !== "Signed out" && toast(e.message, true);

// ------------------------------------------------------------------ sign in
async function start() {
  if (!state.key) return showLogin();
  try { state.user = await api("/api/me"); } catch { return showLogin(); }
  $("#login").hidden = true; $("#app").hidden = false;
  $("#userName").textContent = state.user.username;
  $("#adminBtn").hidden = !state.user.is_admin;
  await loadLists(Number(localStorage.getItem("deckweb.list")) || null);
}
function showLogin() { $("#app").hidden = true; $("#login").hidden = false; }
function signOut() { localStorage.removeItem("deckweb.key"); state.key = null; showLogin(); }

$("#loginForm").addEventListener("submit", async ev => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  try {
    const r = await api("/api/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
    state.key = r.key; localStorage.setItem("deckweb.key", r.key);
    $("#loginError").textContent = ""; ev.target.reset(); start();
  } catch (e) { $("#loginError").textContent = e.message; }
});
$("#logoutBtn").onclick = signOut;

// ------------------------------------------------------------------ lists
async function loadLists(selectId) {
  state.lists = await api("/api/lists");
  const sel = $("#listSelect");
  sel.innerHTML = state.lists.map(l => `<option value="${l.id}">${esc(l.name)} (${l.count})</option>`).join("");
  $("#emptyState").hidden = state.lists.length > 0;
  if (!state.lists.length) { state.data = null; d3.select("#chart").selectAll("g > g > *").remove(); renderSidebar(); return; }
  const id = state.lists.some(l => l.id === selectId) ? selectId : state.lists[0].id;
  sel.value = id;
  await loadList(id, true);
}

async function loadList(id, refit = false) {
  state.data = await api(`/api/lists/${id}`);
  localStorage.setItem("deckweb.list", id);
  const q = $("#quickTag"), cur = state.opts.quickTag;
  q.innerHTML = `<option value="">off</option>` + state.data.tags.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  state.opts.quickTag = state.data.tags.some(t => t.id === cur) ? cur : null;
  q.value = state.opts.quickTag ?? "";
  $("#queryBtn").hidden = state.data.mode !== "mtg";
  draw(refit);
}
const reload = () => loadList(state.data.id).catch(fail);
const draw = (refit = false) => state.data && Chart.render(state.data, state.opts, { refit });

$("#listSelect").onchange = e => { Chart.setPin(null); loadList(Number(e.target.value), true).catch(fail); };

// view options
function setMode(mode) {
  state.opts.mode = mode;
  $("#modeCircle").setAttribute("aria-pressed", mode === "circle");
  $("#modeCols").setAttribute("aria-pressed", mode === "cols");
  draw(true);
}
$("#modeCircle").onclick = () => setMode("circle");
$("#modeCols").onclick = () => setMode("cols");
$("#stackToggle").onchange = e => { state.opts.stack = e.target.checked; draw(); };
$("#dupToggle").onchange = e => { state.opts.dup = e.target.checked; draw(true); };
$("#quickTag").onchange = e => { state.opts.quickTag = e.target.value ? Number(e.target.value) : null; draw(); };

// ------------------------------------------------------------------ tagging
async function setTag(node, { tagId, tagName, on }) {
  await api(`/api/lists/${state.data.id}/item-tags`, {
    method: "POST", body: { item_ids: node.items.map(i => i.id), tag_id: tagId, tag_name: tagName, on } });
  await reload();
}

function tagCheckboxes(node) {
  const has = new Set([...node.tags].map(t => t.id));
  return state.data.tags.map(t => `<label class="check"><input type="checkbox" data-tag="${t.id}" ${has.has(t.id) ? "checked" : ""}> ${esc(t.name)}</label>`).join("")
    + `<input class="newtag" placeholder="New tag, then Enter" maxlength="60">`;
}
function wireTagBoxes(container, node) {
  container.querySelectorAll("input[data-tag]").forEach(cb => cb.onchange = () =>
    setTag(node, { tagId: Number(cb.dataset.tag), on: cb.checked }).catch(fail));
  const nt = container.querySelector(".newtag");
  nt.onkeydown = ev => { if (ev.key === "Enter" && nt.value.trim()) { ev.preventDefault(); setTag(node, { tagName: nt.value, on: true }).catch(fail); } };
}

// small menu from the "#" icon on each node
function openTagMenu(node, ev) {
  const p = $("#popover");
  p.innerHTML = `<strong>${esc(node.name)}${node.items.length > 1 ? ` ×${node.items.length}` : ""}</strong><div class="tag-list">${tagCheckboxes(node)}</div>`;
  wireTagBoxes(p, node);
  p.hidden = false;
  const x = Math.min(ev.clientX + 8, innerWidth - p.offsetWidth - 8), y = Math.min(ev.clientY + 8, innerHeight - p.offsetHeight - 8);
  p.style.left = `${x}px`; p.style.top = `${y}px`;
  p.querySelector(".newtag").focus();
}
document.addEventListener("click", ev => { if (!$("#popover").contains(ev.target)) $("#popover").hidden = true; });
document.addEventListener("keydown", ev => { if (ev.key === "Escape") $("#popover").hidden = true; });

function quickTag(node) {
  const id = state.opts.quickTag;
  setTag(node, { tagId: id, on: ![...node.tags].some(t => t.id === id) }).catch(fail);
}

// ------------------------------------------------------------------ hover image
function hoverImage(node, ev) {
  const img = $("#hoverImg");
  if (!node || !node.print_id) { img.hidden = true; return; }
  const src = `/images/${node.print_id}.jpg`;
  if (!img.src.endsWith(src)) img.src = src;
  img.hidden = false;
  img.onerror = () => { img.hidden = true; };
  const w = 200, h = 280;
  img.style.left = `${ev.clientX + 18 + w > innerWidth ? ev.clientX - w - 18 : ev.clientX + 18}px`;
  img.style.top = `${Math.min(ev.clientY - 40, innerHeight - h - 8)}px`;
}

// ------------------------------------------------------------------ sidebar
const thumb = n => n.print_id ? `<img class="thumb" src="/images/${n.print_id}.jpg" alt="" loading="lazy" onerror="this.style.visibility='hidden'">` : `<span class="thumb blank"></span>`;
const nodeRow = n => `<li><button class="link row-btn" data-item="${n.items[0].id}">${thumb(n)}<span>${esc(n.name)}${n.items.length > 1 ? ` ×${n.items.length}` : ""}</span></button></li>`;

function renderSidebar(sel = state.sel, model = Chart.getModel()) {
  state.sel = sel;
  const sb = $("#sidebar");
  if (!state.data) { sb.innerHTML = `<p class="muted">Paste a decklist or any list to get started.</p>`; return; }
  if (!sel) return overview(sb, model);
  if (sel.type === "tag") {
    const t = sel.tag, members = [...t.members].sort((a, b) => a.name.localeCompare(b.name));
    sb.innerHTML = `
      <p class="kicker">${t.target ? "Points at a card" : "Tag"}</p>
      <h2>#${esc(t.name)}</h2>
      ${t.target ? `<button class="card-link" data-item="${t.target.items[0].id}">${thumb(t.target)} ${esc(t.target.name)}</button>` : ""}
      <p class="muted">${members.reduce((s, n) => s + n.items.length, 0)} items</p>
      <ul class="vlist">${members.map(nodeRow).join("")}</ul>
      <div class="row"><button id="tagRename">Rename</button><button id="tagDelete" class="danger">Delete tag</button></div>`;
    $("#tagRename").onclick = async () => {
      const name = prompt("New tag name", t.name);
      if (name) await api(`/api/tags/${t.id}`, { method: "PATCH", body: { name } }).then(reload).catch(fail);
    };
    $("#tagDelete").onclick = async () => {
      if (confirm(`Delete the tag "${t.name}"? Items stay in the list.`))
        await api(`/api/tags/${t.id}`, { method: "DELETE" }).then(() => { Chart.setPin(null); reload(); }).catch(fail);
    };
  } else {
    const n = sel.node;
    const pointsTo = [...n.tags].filter(t => t.target && t.target !== n).map(t => t.target);
    const pointedBy = model.edges.filter(e => e.to === n).map(e => e.from);
    const card = n.item.type_line ? `<p class="muted">${esc(n.item.mana_cost || "")} ${esc(n.item.type_line)}</p>` : "";
    sb.innerHTML = `
      ${n.print_id ? `<img class="card-img" src="/images/${n.print_id}.jpg" alt="" onerror="this.hidden=true">` : ""}
      <h2>${esc(n.name)}${n.items.length > 1 ? ` <span class="muted">×${n.items.length}</span>` : ""}</h2>
      ${card}
      ${n.item.oracle_id ? `<button id="artBtn">Change art</button>` : ""}
      <h3>Tags</h3><div class="tag-list" id="sbTags">${tagCheckboxes(n)}</div>
      <h3>Points to</h3>${pointsTo.length ? `<ul class="vlist">${pointsTo.map(nodeRow).join("")}</ul>` : `<p class="muted">Nothing yet. Tag this card with another card's name to connect them.</p>`}
      <h3>Pointed to by</h3>${pointedBy.length ? `<ul class="vlist">${[...new Set(pointedBy)].map(nodeRow).join("")}</ul>` : `<p class="muted">No cards point here.</p>`}`;
    wireTagBoxes($("#sbTags"), n);
    if ($("#artBtn")) $("#artBtn").onclick = () => openArt(n);
  }
  sb.querySelectorAll("[data-item]").forEach(b => b.onclick = ev => { ev.stopPropagation(); Chart.setPin({ type: "node", itemId: Number(b.dataset.item) }); });
}

function overview(sb, model) {
  const d = state.data;
  const count = t => model?.tags.get(t.id)?.members.size ?? 0;
  const isCard = t => !!model?.tags.get(t.id)?.target;
  sb.innerHTML = `
    <p class="kicker">${d.mode === "mtg" ? "Magic decklist" : "Plain list"}</p>
    <h2>${esc(d.name)}</h2>
    <p class="muted">${d.items.length} items, ${d.tags.length} of 100 tags. Hover to trace connections; click to keep them lit.</p>
    <h3>Tags</h3>
    <ul class="vlist tags">${d.tags.map(t => `<li><button class="link" data-tag="${t.id}">${isCard(t) ? "→ " : "#"}${esc(t.name)}</button><span class="muted">${count(t)}</span></li>`).join("") || `<li class="muted">No tags yet. Use the # beside any item.</li>`}</ul>
    <input id="sbNewTag" placeholder="New tag, then Enter" maxlength="60">
    <p class="muted small">A tag named after a card in this list draws a line to that card.</p>
    <div class="row"><button id="listRename">Rename list</button><button id="listDelete" class="danger">Delete list</button></div>`;
  sb.querySelectorAll("[data-tag]").forEach(b => b.onclick = ev => { ev.stopPropagation(); Chart.setPin({ type: "tag", id: Number(b.dataset.tag) }); });
  $("#sbNewTag").onkeydown = ev => {
    if (ev.key === "Enter" && ev.target.value.trim())
      api(`/api/lists/${d.id}/tags`, { method: "POST", body: { name: ev.target.value } }).then(reload).catch(fail);
  };
  $("#listRename").onclick = () => {
    const name = prompt("List name", d.name);
    if (name) api(`/api/lists/${d.id}`, { method: "PATCH", body: { name } }).then(() => loadLists(d.id)).catch(fail);
  };
  $("#listDelete").onclick = () => {
    if (confirm(`Delete "${d.name}" and all its tags? This can't be undone.`))
      api(`/api/lists/${d.id}`, { method: "DELETE" }).then(() => loadLists()).catch(fail);
  };
}

// ------------------------------------------------------------------ new list dialog
const impBody = () => ({
  name: $("#impName").value.trim(), text: $("#impText").value,
  mode: document.querySelector("input[name=impMode]:checked").value, import_tags: $("#impTags").checked,
});
function report({ count, errors = [], warnings = [], tags = [] }) {
  const line = (e, cls) => `<li class="${cls}"><button type="button" class="link" data-line="${e.line}">${e.line ? `Line ${e.line}` : "List"}</button> ${esc(e.reason)}</li>`;
  $("#impReport").innerHTML = (count != null ? `<p>${count} items${tags.length ? `, tags: ${tags.map(esc).join(", ")}` : ""}.</p>` : "")
    + `<ul>${errors.map(e => line(e, "error")).join("")}${warnings.map(e => line(e, "warn")).join("")}</ul>`;
  $("#impDrop").hidden = !errors.some(e => e.line > 0);
  $("#impReport").querySelectorAll("[data-line]").forEach(b => b.onclick = () => selectLine(Number(b.dataset.line)));
}
function selectLine(n) {
  if (!n) return;
  const ta = $("#impText"), lines = ta.value.split("\n");
  const start = lines.slice(0, n - 1).reduce((s, l) => s + l.length + 1, 0);
  ta.focus(); ta.setSelectionRange(start, start + lines[n - 1].length);
  ta.scrollTop = (n - 3) * parseFloat(getComputedStyle(ta).lineHeight);
}
function openImport() { $("#impReport").innerHTML = ""; $("#impDrop").hidden = true; $("#importDlg").showModal(); }
$("#newListBtn").onclick = openImport;
$("#emptyNewBtn").onclick = openImport;
$("#impCheck").onclick = () => api("/api/lists/preview", { method: "POST", body: impBody() }).then(report).catch(fail);
async function saveList(drop) {
  const b = impBody();
  if (!b.name) return toast("Give the list a name.", true);
  try {
    const r = await api("/api/lists", { method: "POST", body: { ...b, drop_errors: drop } });
    $("#importDlg").close(); $("#impText").value = ""; $("#impName").value = "";
    if (r.warnings?.length) toast(`Saved with ${r.warnings.length} note(s).`); else toast("List saved.");
    Chart.setPin(null); await loadLists(r.id);
  } catch (e) { if (e.data?.errors) report(e.data); else fail(e); }
}
$("#impSave").onclick = () => saveList(false);
$("#impDrop").onclick = () => saveList(true);

// ------------------------------------------------------------------ Scryfall query tags
$("#queryBtn").onclick = () => { $("#qResult").textContent = ""; $("#queryDlg").showModal(); };
$("#qQuery").oninput = e => { const m = e.target.value.match(/otag:([\w-]+)/); if (m && !$("#qTag").dataset.touched) $("#qTag").value = m[1]; };
$("#qTag").oninput = e => { e.target.dataset.touched = "1"; };
$("#qRun").onclick = async () => {
  $("#qResult").textContent = "Searching Scryfall…";
  try {
    const r = await api(`/api/lists/${state.data.id}/query-tag`, { method: "POST",
      body: { query: $("#qQuery").value, tag: $("#qTag").value, use_identity: $("#qIdentity").checked } });
    $("#qResult").textContent = `Tagged ${r.matched} items. Search used: ${r.query}`;
    await reload();
  } catch (e) { $("#qResult").textContent = e.message; }
};

// ------------------------------------------------------------------ art picker
async function openArt(node) {
  $("#artTitle").textContent = `Choose art for ${node.name}`;
  $("#artGrid").innerHTML = `<p class="muted">Loading printings…</p>`;
  $("#artDlg").showModal();
  try {
    const prints = await api(`/api/lists/${state.data.id}/items/${node.items[0].id}/prints`);
    $("#artGrid").innerHTML = prints.map(p => `<button type="button" class="art ${p.print_id === node.print_id ? "current" : ""}" data-print="${p.print_id}">
      <img src="${esc(p.thumb)}" alt="" loading="lazy"><span>${esc(p.set)} ${esc(p.number)}</span></button>`).join("");
    $("#artGrid").querySelectorAll("[data-print]").forEach(b => b.onclick = async () => {
      await api(`/api/lists/${state.data.id}/art`, { method: "POST", body: { item_ids: node.items.map(i => i.id), print_id: b.dataset.print } }).catch(fail);
      $("#artDlg").close(); reload();
    });
  } catch (e) { $("#artGrid").innerHTML = `<p class="error">${esc(e.message)}</p>`; }
}

// ------------------------------------------------------------------ export
$("#exportMenu").querySelectorAll("[data-format]").forEach(b => b.onclick = async () => {
  $("#exportMenu").open = false;
  if (!state.data) return;
  toast("Preparing export…");
  try {
    const res = await api(`/api/lists/${state.data.id}/export?format=${b.dataset.format}`, { raw: true });
    const url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: `${state.data.name.replace(/[^\w]+/g, "_")}.zip` });
    a.click(); URL.revokeObjectURL(url);
  } catch (e) { fail(e); }
});

// ------------------------------------------------------------------ account
$("#accountBtn").onclick = () => { $("#userMenu").open = false; $("#accKey").value = state.key; $("#accountDlg").showModal(); };
$("#accCopy").onclick = () => navigator.clipboard.writeText(state.key).then(() => toast("Key copied."));
$("#accRegen").onclick = async () => {
  if (!confirm("Make a new key? Other devices and scripts using the old key will stop working.")) return;
  const r = await api("/api/me/key", { method: "POST" }).catch(fail);
  if (r) { state.key = r.key; localStorage.setItem("deckweb.key", r.key); $("#accKey").value = r.key; toast("New key made."); }
};
$("#accPwSave").onclick = () => api("/api/me/password", { method: "POST", body: { password: $("#accPw").value } })
  .then(() => { $("#accPw").value = ""; toast("Password changed."); }).catch(fail);

// ------------------------------------------------------------------ admin
async function openAdmin() {
  $("#userMenu").open = false;
  const [st, users] = await Promise.all([api("/api/status"), api("/api/admin/users")]);
  $("#admCards").textContent = `${st.cards} cards in the database. ${st.message || ""}`;
  $("#admUsers").innerHTML = users.map(u => `
    <div class="adm-user">
      <div class="row"><strong>${esc(u.username)}</strong>${u.is_admin ? `<span class="muted">admin</span>` : ""}
        <span class="muted">${u.lists.length} of 10 lists</span><span class="grow"></span>
        ${u.username === state.user.username ? "" : `<button type="button" class="danger" data-deluser="${u.id}">Delete user</button>`}</div>
      <ul class="vlist">${u.lists.map(l => `<li><span>${esc(l.name)} <span class="muted">(${l.count})</span></span>
        <button type="button" class="danger small" data-dellist="${l.id}">Delete</button></li>`).join("")}</ul>
    </div>`).join("");
  $("#admUsers").querySelectorAll("[data-dellist]").forEach(b => b.onclick = () =>
    confirm("Delete this list?") && api(`/api/lists/${b.dataset.dellist}`, { method: "DELETE" }).then(openAdmin).then(() => loadLists(state.data?.id)).catch(fail));
  $("#admUsers").querySelectorAll("[data-deluser]").forEach(b => b.onclick = () =>
    confirm("Delete this user and all their lists?") && api(`/api/admin/users/${b.dataset.deluser}`, { method: "DELETE" }).then(openAdmin).catch(fail));
  if (!$("#adminDlg").open) $("#adminDlg").showModal();
}
$("#adminBtn").onclick = () => openAdmin().catch(fail);
$("#admRefresh").onclick = () => api("/api/admin/cards/refresh", { method: "POST" }).then(() => toast("Refreshing card data in the background.")).catch(fail);
$("#admAdd").onclick = () => api("/api/admin/users", { method: "POST",
  body: { username: $("#admName").value, password: $("#admPw").value, is_admin: $("#admIsAdmin").checked } })
  .then(() => { $("#admName").value = $("#admPw").value = ""; toast("User added."); openAdmin(); }).catch(fail);

// ------------------------------------------------------------------ boot
Chart.init($("#chart"), {
  onPin: (sel, model) => renderSidebar(sel, model),
  onTagMenu: openTagMenu,
  onQuickTag: quickTag,
  onHoverImage: hoverImage,
});
window.addEventListener("resize", () => draw(true));
start();
