/* Fees Calling Desk – teacher UI. Plain JS, talks to the SchoolOps API on the same server. */
(() => {
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rupee = (v) => (v == null ? "–" : "₹" + Math.round(Number(v)).toLocaleString("en-IN"));
const fdate = (s) => (s ? new Date(s.length === 10 ? s + "T00:00:00" : s).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "");
const ftime = (s) => (s ? new Date(s).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) : "");
const todayISO = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};
const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== "" && v != null)).toString();

// Same eight one-tap results as the original Fees Calling Desk.
const ANSWERS = [["will_pay", "g"], ["gave_date", "b"], ["already_paid", ""], ["call_later", ""],
                 ["no_answer", ""], ["phone_off", ""], ["wrong_number", ""], ["complaint", ""]];
let META = { outcomes: {}, std: [], medium: [], section: [], languages: {} };
const label = (o) => META.outcomes[o] || o;

async function api(path, opt = {}) {
  const r = await fetch(path, { method: opt.method || "GET", headers: { "Content-Type": "application/json" },
                                 body: opt.body ? JSON.stringify(opt.body) : undefined });
  let d = null; try { d = await r.json(); } catch {}
  if (!r.ok) {
    const det = d && d.detail;
    throw new Error(typeof det === "string" ? det : (Array.isArray(det) && det[0] ? det[0].msg : `Something went wrong (${r.status})`));
  }
  return d;
}

let toastTimer;
function toast(text, { undo, bad } = {}) {
  clearTimeout(toastTimer);
  $("#toast").innerHTML = `<div class="toast ${bad ? "bad" : ""}" role="status"><span>${esc(text)}</span>${undo ? '<button id="undoBtn">Undo</button>' : ""}</div>`;
  if (undo) $("#undoBtn").onclick = async () => { $("#toast").innerHTML = ""; await undo(); };
  toastTimer = setTimeout(() => ($("#toast").innerHTML = ""), undo ? 7000 : 4000);
}
const fail = (e) => toast(e.message || String(e), { bad: true });

// ------------------------------------------------------------------ shared pieces
function selectHTML(name, labelText, options, value, allText = "All") {
  return `<label>${labelText}<select data-f="${name}"><option value="">${allText}</option>${options.map((o) => {
    const [v, t] = Array.isArray(o) ? o : [o, o];
    return `<option value="${esc(v)}" ${String(v) === String(value ?? "") ? "selected" : ""}>${esc(t)}</option>`; }).join("")}</select></label>`;
}
function classFilters(st, { search = false, fee = false } = {}) {
  return `<div class="filters">
    ${search ? `<label>Search<input type="search" data-f="search" value="${esc(st.search || "")}" placeholder="Name, phone or Sr. No."></label>` : ""}
    ${selectHTML("medium", "Medium", META.medium, st.medium)}
    ${selectHTML("std", "Class", META.std.map((s) => [s, /^\d+$/.test(s) ? `Std ${s}` : s]), st.std)}
    ${selectHTML("section", "Section", META.section, st.section)}
    ${fee ? selectHTML("fee_status", "Fees", [["due", "Fees due"], ["no_balance_in_excel", "No balance in Excel"], ["unknown", "Not in Excel yet"]], st.fee_status) : ""}
  </div>`;
}
function bindFilters(root, st, onChange) {
  let t;
  root.querySelectorAll("[data-f]").forEach((el) => {
    const ev = el.type === "search" ? "input" : "change";
    el.addEventListener(ev, () => {
      clearTimeout(t);
      t = setTimeout(() => { st[el.dataset.f] = el.value; st.page = 1; onChange(); }, el.type === "search" ? 300 : 0);
    });
  });
}
function pager(d, onPage) {
  const div = document.createElement("div");
  div.className = "pager";
  div.innerHTML = `<button class="btn small" data-p="${d.page - 1}" ${d.page <= 1 ? "disabled" : ""}>‹ Previous</button>
    <span>Page ${d.page} of ${d.pages} · ${d.total} total</span>
    <button class="btn small" data-p="${d.page + 1}" ${d.page >= d.pages ? "disabled" : ""}>Next ›</button>`;
  div.addEventListener("click", (e) => { const b = e.target.closest("[data-p]"); if (b && !b.disabled) onPage(+b.dataset.p); });
  return div;
}
const feePill = (s) => s.fee_status === "due" ? `<span class="pill due">Due</span>`
  : s.fee_status === "no_balance_in_excel" ? `<span class="pill zero">No balance in Excel</span>` : `<span class="pill unknown">Not in Excel</span>`;
const klassTxt = (s) => s.klass || `${s.medium} ${s.std}${s.section ? "-" + s.section : ""}`;

/* One-tap call result. Saves a call, offers Undo, then calls onDone(). */
function callForm(student, onDone) {
  const box = document.createElement("div");
  box.className = "callcard";
  const draw = (askDate) => {
    box.innerHTML = `<div class="who"><div><div class="name">${esc(student.name)}</div>
        <div class="meta">${esc(klassTxt(student))}${student.sr_no ? " · Sr. " + esc(student.sr_no) : ""}</div></div>
        <div class="due"><span class="amt">${rupee(student.balance)}</span><small>fees due (from Excel)</small></div></div>
      <div>${student.contact ? `<a class="phone" href="tel:${esc(student.contact)}">${esc(student.contact)}</a>` : '<span class="pill amber">No phone number</span>'}
        ${student.promise_passed ? ' <span class="pill due">Promised date has passed</span>' : ""}</div>
      ${student.last_outcome ? `<div class="prev">Last call ${ftime(student.last_call_at)}: ${esc(label(student.last_outcome))}${student.follow_up_date ? ", said will pay by " + fdate(student.follow_up_date) : ""}</div>` : ""}
      <b>After the call, what did the parent say?</b>
      <input data-note placeholder="Note (optional), e.g. will pay after salary" aria-label="Note">
      ${askDate ? `<div class="datebox"><label>Which date will they pay?<input type="date" data-date min="${todayISO()}"></label>
          <button class="btn main" data-save-date>Save</button><button class="btn" data-back>Back</button></div>`
        : `<div class="answers">${ANSWERS.map(([o, c]) => `<button class="ans ${c}" data-o="${o}">${esc(label(o))}</button>`).join("")}</div>`}`;
  };
  const save = async (outcome, follow_up_date) => {
    try {
      const call = await api("/calls", { method: "POST", body: {
        student_id: student.id, outcome, follow_up_date: follow_up_date || null,
        note: box.querySelector("[data-note]").value.trim() || null, caller: store.get("fcd_caller", "") || null } });
      toast(`Saved: ${student.name} · ${label(outcome)}`, { undo: async () => {
        try { await api(`/calls/${call.id}`, { method: "DELETE" }); toast("Call removed"); onDone(); } catch (e) { fail(e); } } });
      onDone();
    } catch (e) { fail(e); }
  };
  box.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.o === "gave_date") { const n = box.querySelector("[data-note]").value; draw(true); box.querySelector("[data-note]").value = n; box.querySelector("[data-date]").focus(); }
    else if (b.dataset.o) save(b.dataset.o);
    else if ("back" in b.dataset) { const n = box.querySelector("[data-note]").value; draw(false); box.querySelector("[data-note]").value = n; }
    else if ("saveDate" in b.dataset) {
      const d = box.querySelector("[data-date]");
      if (!d.value) { d.style.borderColor = "var(--red)"; d.focus(); return; }
      save("gave_date", d.value);
    }
  });
  draw(false);
  return box;
}

// ------------------------------------------------------------------ student drawer
async function openStudent(id) {
  const dr = $("#drawer");
  dr.hidden = false; $("#scrim").hidden = false;
  dr.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const s = await api(`/students/${id}`);
    const last = s.calls[0];
    const full = { ...s, last_outcome: last && last.outcome, last_call_at: last && last.called_at, follow_up_date: last && last.follow_up_date,
                   promise_passed: !!(last && last.follow_up_date && last.follow_up_date < todayISO()) };
    dr.innerHTML = `<div class="panel-head"><h2>${esc(s.name)}</h2><button class="btn small" data-close>Close</button></div>
      ${s.missing_since ? `<div class="note warn">Not in the latest Excel file (since ${fdate(s.missing_since)}). Kept with all history.</div>` : ""}
      <dl class="kv"><dt>Class</dt><dd>${esc(s.klass)}</dd><dt>Sr. No.</dt><dd>${esc(s.sr_no || "–")}</dd>
        <dt>Phone</dt><dd>${esc(s.contact || "–")}</dd><dt>Total fee</dt><dd>${rupee(s.total_fee)}</dd>
        <dt>Paid</dt><dd>${rupee(s.paid)}</dd><dt>Balance</dt><dd>${rupee(s.balance)} <span class="muted">(Excel, ${fdate(s.fee_as_of)})</span></dd></dl>
      <div id="drawerCall"></div>
      <a class="btn" href="#messages?student=${s.id}" data-close>Message previews</a>
      <h3>Call history</h3>
      <div class="list">${s.calls.length ? s.calls.map((c) => `<div class="row"><span><b>${esc(label(c.outcome))}</b>${c.follow_up_date ? " · pays by " + fdate(c.follow_up_date) : ""}${c.note ? " · “" + esc(c.note) + "”" : ""}</span>
        <span class="muted">${ftime(c.called_at)}${c.caller ? " · " + esc(c.caller) : ""}</span></div>`).join("") : '<p class="muted">No calls yet.</p>'}</div>
      <h3>Fee history (from Excel)</h3>
      <div class="tablewrap"><table style="min-width:420px"><thead><tr><th>Date</th><th class="num">Total</th><th class="num">Paid</th><th class="num">Balance</th></tr></thead>
        <tbody>${s.fee_history.map((f) => `<tr><td>${fdate(f.captured_at)}${f.source !== "excel" ? ' <span class="pill grey">reviewed</span>' : ""}</td><td class="num">${rupee(f.total_fee)}</td><td class="num">${rupee(f.paid)}</td><td class="num">${rupee(f.balance)}</td></tr>`).join("")}</tbody></table></div>`;
    if (Number(s.balance) > 0) $("#drawerCall", dr).append(callForm(full, () => { openStudent(id); route(true); }));
    dr.focus?.();
  } catch (e) { dr.innerHTML = `<button class="btn small" data-close>Close</button><p>${esc(e.message)}</p>`; }
}
function closeDrawer() { $("#drawer").hidden = true; $("#scrim").hidden = true; }

// ------------------------------------------------------------------ pages
const dashState = store.get("fcd_dash", { medium: "", std: "", section: "" });
let skip = 0, fuWhen = "today";

async function pageDashboard(main) {
  const [d, classes] = await Promise.all([api("/dashboard"), api("/classes")]);
  const targets = store.get("fcd_targets", {});
  main.innerHTML = `
    <section class="cards">
      <div class="card"><span class="v">${rupee(d.pending_amount)}</span><span class="k">Fees pending</span></div>
      <div class="card"><span class="v">${d.students_due}</span><span class="k">Students with fees due</span></div>
      <div class="card"><span class="v">${d.calls_today}</span><span class="k">Calls done today</span></div>
      <div class="card"><span class="v">${d.promised_today}</span><span class="k">Parents who promised today</span></div>
      <div class="card ${d.follow_ups_overdue ? "alert" : ""}"><span class="v">${d.follow_ups_today} / ${d.follow_ups_overdue}</span><span class="k">Follow-ups today / overdue</span></div>
      ${d.pending_reviews ? `<a class="card warn" href="#sync" style="text-decoration:none;color:inherit"><span class="v">${d.pending_reviews}</span><span class="k">Excel changes to check</span></a>` : ""}
    </section>
    <div class="grid2">
      <section class="panel" id="nextPanel">
        <div class="panel-head"><h2>Call next parent</h2><button class="btn small" id="skipBtn">Skip this parent</button></div>
        ${classFilters(dashState)}
        <div id="nextBody"><p class="muted">Loading…</p></div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>Follow-ups</h2>
          <div><button class="btn small ${fuWhen === "today" ? "main" : ""}" data-fu="today">Today</button>
               <button class="btn small ${fuWhen === "overdue" ? "main" : ""}" data-fu="overdue">Overdue</button></div></div>
        <div class="list" id="fuList"><p class="muted">Loading…</p></div>
      </section>
    </div>
    <section class="panel">
      <div class="panel-head"><h2>Each class today</h2><span class="muted">Type a daily calling target for each class</span></div>
      <div class="tablewrap"><table><thead><tr><th>Class</th><th class="num">Fees due</th><th class="num">Pending</th><th class="num">Called today</th><th>Target</th><th>Progress</th></tr></thead>
      <tbody>${classes.map((c) => { const t = +targets[c.klass] || 0; const pct = t ? Math.min(100, c.called_today / t * 100) : 0;
        return `<tr><td><b>${esc(c.klass)}</b></td><td class="num">${c.students_due}</td><td class="num">${rupee(c.pending_amount)}</td>
          <td class="num">${c.called_today}${t ? " / " + t : ""}</td>
          <td><input class="target" type="number" min="0" inputmode="numeric" data-t="${esc(c.klass)}" value="${t || ""}" placeholder="0" aria-label="Target for ${esc(c.klass)}"></td>
          <td><div class="track"><i style="width:${pct}%"></i></div></td></tr>`; }).join("") || '<tr><td colspan="6" class="empty">No students yet. Check Excel Sync.</td></tr>'}</tbody></table></div>
    </section>`;

  const loadNext = async () => {
    const body = $("#nextBody");
    try {
      const r = await api("/call-next?" + qs({ ...dashState, skip }));
      body.innerHTML = "";
      if (!r.student) { body.innerHTML = `<div class="empty">${skip ? "No more parents after the ones you skipped. " : ""}Nobody left to call${dashState.std ? " in this class" : ""} today. Parents who gave a date come back on that date.</div>`; return; }
      body.append(callForm(r.student, () => { skip = 0; pageDashboard(main).catch(fail); }));
    } catch (e) { fail(e); }
  };
  bindFilters($("#nextPanel"), dashState, () => { skip = 0; store.set("fcd_dash", dashState); loadNext(); });
  $("#skipBtn").onclick = () => { skip += 1; loadNext(); };
  main.querySelectorAll("[data-fu]").forEach((b) => b.onclick = () => { fuWhen = b.dataset.fu; pageDashboard(main).catch(fail); });
  main.querySelectorAll("[data-t]").forEach((i) => i.addEventListener("change", () => { targets[i.dataset.t] = i.value; store.set("fcd_targets", targets); pageDashboard(main).catch(fail); }));
  loadNext();
  const fu = await api("/follow-ups?when=" + fuWhen);
  $("#fuList").innerHTML = fu.length ? fu.map((f) => `<div class="row" data-open="${f.id}" style="cursor:pointer"><span><b>${esc(f.name)}</b> · ${esc(f.klass)}<br><span class="muted">said will pay by ${fdate(f.follow_up_date)}${f.note ? " · “" + esc(f.note) + "”" : ""}</span></span><span class="amt">${rupee(f.balance)}</span></div>`).join("")
    : `<p class="muted">${fuWhen === "today" ? "No parents promised to pay today." : "No overdue promises."}</p>`;
}

const stuState = { search: "", medium: "", std: "", section: "", fee_status: "", page: 1, page_size: 25 };
async function pageStudents(main) {
  main.innerHTML = `<section class="panel" id="stuPanel">${classFilters(stuState, { search: true, fee: true })}
    <div id="stuTable"></div></section>`;
  const load = async () => {
    const d = await api("/students?" + qs(stuState));
    const box = $("#stuTable");
    box.innerHTML = `<div class="tablewrap"><table><thead><tr><th class="hide-sm">Sr.</th><th>Name</th><th>Class</th><th class="hide-sm">Phone</th><th class="num">Balance</th><th>Fees</th><th class="hide-sm">Last call</th></tr></thead>
      <tbody>${d.items.map((s) => `<tr class="click" data-open="${s.id}" tabindex="0"><td class="hide-sm">${esc(s.sr_no || "")}</td><td><b>${esc(s.name)}</b></td><td>${esc(s.klass)}</td>
        <td class="hide-sm">${esc(s.contact || "–")}</td><td class="num amt">${rupee(s.balance)}</td><td>${feePill(s)}</td>
        <td class="hide-sm">${s.last_outcome ? esc(label(s.last_outcome)) + `<br><span class="muted">${fdate(s.last_call_at)}</span>` : '<span class="muted">Not called</span>'}</td></tr>`).join("")
        || `<tr><td colspan="7" class="empty">No students match. Try clearing the filters.</td></tr>`}</tbody></table></div>`;
    box.append(pager(d, (p) => { stuState.page = p; load().catch(fail); }));
  };
  bindFilters($("#stuPanel"), stuState, () => load().catch(fail));
  await load();
}

const feeState = { medium: "", std: "", section: "", page: 1, page_size: 25 };
async function pageFees(main) {
  const classes = await api("/classes");
  const total = classes.reduce((a, c) => a + Number(c.pending_amount), 0);
  const top = [...classes].sort((a, b) => b.pending_amount - a.pending_amount).slice(0, 8);
  main.innerHTML = `<div class="note info">Amounts come from the school's Excel files. This app never marks fees as paid: when the office records a payment in Excel, it shows here after the next sync.</div>
    <section class="cards">
      <div class="card"><span class="v">${rupee(total)}</span><span class="k">Total pending</span></div>
      ${top.slice(0, 3).map((c) => `<div class="card"><span class="v">${rupee(c.pending_amount)}</span><span class="k">${esc(c.klass)} · ${c.students_due} students</span></div>`).join("")}
    </section>
    <section class="panel" id="feePanel"><div class="panel-head"><h2>Students with fees due</h2><span class="muted">Highest balance first</span></div>
      ${classFilters(feeState)}<div id="feeTable"></div></section>`;
  const load = async () => {
    const d = await api("/students?" + qs({ ...feeState, fee_status: "due", sort: "balance" }));
    const box = $("#feeTable");
    box.innerHTML = `<div class="tablewrap"><table><thead><tr><th>Name</th><th>Class</th><th class="num hide-sm">Total</th><th class="num hide-sm">Paid</th><th class="num">Balance</th><th class="hide-sm">As of</th><th class="hide-sm">Last call</th></tr></thead>
      <tbody>${d.items.map((s) => `<tr class="click" data-open="${s.id}" tabindex="0"><td><b>${esc(s.name)}</b></td><td>${esc(s.klass)}</td>
        <td class="num hide-sm">${rupee(s.total_fee)}</td><td class="num hide-sm">${rupee(s.paid)}</td><td class="num amt">${rupee(s.balance)}</td>
        <td class="hide-sm">${fdate(s.fee_as_of)}</td><td class="hide-sm">${s.last_outcome ? esc(label(s.last_outcome)) : '<span class="muted">Not called</span>'}</td></tr>`).join("")
        || `<tr><td colspan="7" class="empty">No fees due with these filters.</td></tr>`}</tbody></table></div>`;
    box.append(pager(d, (p) => { feeState.page = p; load().catch(fail); }));
  };
  bindFilters($("#feePanel"), feeState, () => load().catch(fail));
  await load();
}

const callState = { search: "", medium: "", std: "", section: "", page: 1, page_size: 25 };
async function pageCalls(main) {
  main.innerHTML = `<section class="panel" id="callPanel"><div class="panel-head"><h2>All calls</h2><button class="btn small" id="csvBtn">Copy for Excel</button></div>
    ${classFilters(callState, { search: true })}<div id="callTable"></div><textarea id="csvBox" hidden readonly style="width:100%;min-height:120px"></textarea></section>`;
  const load = async () => {
    const d = await api("/calls?" + qs(callState));
    const box = $("#callTable");
    box.innerHTML = `<div class="tablewrap"><table><thead><tr><th>When</th><th>Student</th><th>Class</th><th>Result</th><th>Pays by</th><th class="hide-sm">Note</th><th class="hide-sm">Called by</th></tr></thead>
      <tbody>${d.items.map((c) => `<tr class="click" data-open="${c.student_id}" tabindex="0"><td>${ftime(c.called_at)}</td><td><b>${esc(c.name)}</b></td><td>${esc(c.klass)}</td>
        <td>${esc(c.outcome_label)}</td><td>${fdate(c.follow_up_date)}</td><td class="hide-sm">${esc(c.note || "")}</td><td class="hide-sm">${esc(c.caller || "")}</td></tr>`).join("")
        || `<tr><td colspan="7" class="empty">No calls saved yet.</td></tr>`}</tbody></table></div>`;
    box.append(pager(d, (p) => { callState.page = p; load().catch(fail); }));
  };
  $("#csvBtn").onclick = async () => {
    try {
      const rows = [["Date", "Student", "Class", "Result", "Pays by", "Note", "Called by"]];
      for (let p = 1; p <= 50; p++) {
        const d = await api("/calls?" + qs({ ...callState, page: p, page_size: 100 }));
        d.items.forEach((c) => rows.push([ftime(c.called_at), c.name, c.klass, c.outcome_label, c.follow_up_date || "", c.note || "", c.caller || ""]));
        if (p >= d.pages) break;
      }
      const csv = rows.map((r) => r.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(",")).join("\n");
      const box = $("#csvBox"); box.value = csv;
      try { await navigator.clipboard.writeText(csv); toast(`Copied ${rows.length - 1} calls. Paste them in Excel.`); }
      catch { box.hidden = false; box.select(); toast("Press Ctrl+C / Cmd+C to copy"); }
    } catch (e) { fail(e); }
  };
  bindFilters($("#callPanel"), callState, () => load().catch(fail));
  await load();
}

let msgPage = 1;
async function pageMessages(main, params) {
  main.innerHTML = `<div class="note warn">Real sending is turned off. These are previews only. "Save as test message" keeps a copy in the log below; no parent receives anything.</div>
    <section class="panel"><h2>Choose a student</h2>
      <div class="filters"><label style="flex:1">Search<input type="search" id="msgSearch" placeholder="Type a name or phone number"></label></div>
      <div class="list" id="msgResults"></div></section>
    <div id="msgPreview"></div>
    <section class="panel"><h2>Test message log</h2><div id="msgLog"></div></section>`;
  let t;
  $("#msgSearch").addEventListener("input", (e) => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const q = e.target.value.trim();
      if (!q) { $("#msgResults").innerHTML = ""; return; }
      const d = await api("/students?" + qs({ search: q, page_size: 6 }));
      $("#msgResults").innerHTML = d.items.map((s) => `<a class="row" href="#messages?student=${s.id}" style="text-decoration:none;color:inherit"><span><b>${esc(s.name)}</b> · ${esc(s.klass)}</span><span class="amt">${rupee(s.balance)}</span></a>`).join("") || '<p class="muted">No match.</p>';
    }, 300);
  });
  if (params.student) {
    const [s, p] = await Promise.all([api(`/students/${params.student}`), api(`/messages/preview/${params.student}`)]);
    $("#msgPreview").innerHTML = `<section class="panel"><div class="panel-head"><h2>${esc(s.name)} · ${esc(s.klass)}</h2><span class="amt">${rupee(s.balance)} due</span></div>
      <div class="msggrid">${Object.entries(p).map(([lang, body]) => `<div class="panel"><h3>${esc(META.languages[lang] || lang)}</h3><div class="msg" lang="${lang}">${esc(body)}</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn small" data-copy="${lang}">Copy</button><button class="btn small" data-mock="${lang}">Save as test message</button></div></div>`).join("")}</div></section>`;
    $("#msgPreview").addEventListener("click", async (e) => {
      const b = e.target.closest("button"); if (!b) return;
      if (b.dataset.copy) { try { await navigator.clipboard.writeText(p[b.dataset.copy]); toast("Copied"); } catch { toast("Select the text and copy it", { bad: true }); } }
      if (b.dataset.mock) { try { await api("/messages/mock", { method: "POST", body: { student_id: s.id, lang: b.dataset.mock } }); toast("Saved as a test message. Nothing was sent."); loadLog(); } catch (er) { fail(er); } }
    });
  }
  const loadLog = async () => {
    const d = await api("/messages?" + qs({ page: msgPage, page_size: 10 }));
    const box = $("#msgLog");
    box.innerHTML = `<div class="tablewrap"><table><thead><tr><th>When</th><th>Student</th><th>Language</th><th>Status</th></tr></thead><tbody>${d.items.map((m) => `<tr><td>${ftime(m.created_at)}</td><td>${esc(m.name)} · ${esc(m.klass)}</td><td>${esc(META.languages[m.lang] || m.lang)}</td><td><span class="pill grey">Test only, not sent</span></td></tr>`).join("") || '<tr><td colspan="4" class="empty">No test messages yet.</td></tr>'}</tbody></table></div>`;
    box.append(pager(d, (pg) => { msgPage = pg; loadLog().catch(fail); }));
  };
  await loadLog();
}

const REASON = { balance_increased: "the balance went up", paid_decreased: "the paid amount went down" };
async function pageSync(main) {
  const [st, reviews, runs] = await Promise.all([api("/sync/status"), api("/review"), api("/sync/runs?page_size=15")]);
  const yes = (b) => (b ? "Yes" : "No");
  main.innerHTML = `
    ${st.test_mode ? '<div class="note info">Test mode: only the synthetic test folder is used. No real school data.</div>'
      : '<div class="note warn">Real school folder. From this page you can only run checks (dry runs). Nothing is written to the database from here.</div>'}
    <div class="grid2">
      <section class="panel"><h2>Status</h2>
        <dl class="kv"><dt>Folder</dt><dd style="word-break:break-all">${esc(st.watch_folder)}</dd>
          <dt>Watching for saved files</dt><dd>${yes(st.watching)}${st.watcher_error ? ` <span class="muted">(${esc(st.watcher_error)})</span>` : ""}</dd>
          <dt>Default mode</dt><dd>${st.dry_run_default ? "Dry run (check only)" : "Apply changes"}</dd>
          <dt>Column mapping confirmed</dt><dd>${yes(st.mapping_confirmed)}</dd>
          <dt>Payment rules confirmed</dt><dd>${yes(st.payment_rules_confirmed)}</dd></dl>
        <div><button class="btn main" id="checkBtn">Check the folder now (dry run)</button></div>
        <div id="checkResult"></div>
      </section>
      <section class="panel"><div class="panel-head"><h2>Changes to check</h2><span class="pill ${reviews.length ? "amber" : "zero"}">${reviews.length} waiting</span></div>
        <div class="list">${reviews.map(reviewHTML).join("") || '<p class="muted">Nothing to check. Every Excel change matched the rules.</p>'}</div>
      </section>
    </div>
    <section class="panel"><h2>Recent syncs</h2>
      <div class="tablewrap"><table><thead><tr><th>When</th><th>File</th><th>How</th><th>Result</th><th class="num">New</th><th class="num">Fee updates</th><th class="num">To check</th><th class="num">Errors</th></tr></thead>
      <tbody>${runs.items.map((r) => { const s = r.summary || {}; return `<tr class="click" data-run="${r.id}" tabindex="0"><td>${ftime(r.started_at)}</td><td>${esc(r.file_path.split(/[\\/]/).pop())}</td>
        <td>${esc({ watcher: "File saved", reconcile: "Regular re-check", api: "From this page", manual: "Command line" }[r.trigger] || r.trigger)}</td>
        <td>${runPill(r.status)}</td><td class="num">${s.new_students ?? "–"}</td><td class="num">${s.fee_updates ?? "–"}</td><td class="num">${s.needs_review ?? "–"}</td><td class="num">${s.errors ?? "–"}</td></tr>`; }).join("")
        || '<tr><td colspan="8" class="empty">No syncs yet.</td></tr>'}</tbody></table></div></section>`;

  $("#checkBtn").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Checking…";
    try {
      const r = await api("/sync/run", { method: "POST", body: { dry_run: true, force: true } });
      $("#checkResult").innerHTML = `<div class="tablewrap"><table style="min-width:480px"><thead><tr><th>File</th><th class="num">New</th><th class="num">Fee updates</th><th class="num">To check</th><th class="num">Errors</th></tr></thead><tbody>${
        r.files.map((f) => `<tr><td>${esc(f.file)}</td><td class="num">${f.new_students ?? "–"}</td><td class="num">${f.fee_updates ?? "–"}</td><td class="num">${f.needs_review ?? "–"}</td><td class="num">${f.errors ?? "–"}</td></tr>`).join("")}</tbody></table></div>
        <p class="muted">Dry run: nothing was changed.</p>`;
    } catch (er) { fail(er); }
    e.target.disabled = false; e.target.textContent = "Check the folder now (dry run)";
  };
  main.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-review]");
    if (b) {
      try { await api(`/review/${b.dataset.review}/${b.dataset.act}?` + qs({ by: store.get("fcd_caller", "") }), { method: "POST" }); toast("Saved"); route(true); } catch (er) { fail(er); }
      return;
    }
    const tr = e.target.closest("[data-run]");
    if (tr) showRun(tr.dataset.run);
  });
}
function runPill(s) {
  return { applied: '<span class="pill zero">Synced</span>', dry_run: '<span class="pill grey">Check only</span>',
           failed: '<span class="pill due">Failed</span>', running: '<span class="pill amber">Running</span>' }[s] || esc(s);
}
function reviewHTML(r) {
  const d = r.details;
  if (r.kind === "fee_change") {
    return `<div class="row" style="display:grid;gap:8px"><div><b>${esc(d.name)}</b> · ${esc(d.class)}<br>
      <span class="muted">In Excel, ${(d.reasons || []).map((x) => REASON[x] || x).join(" and ")}.</span><br>
      Balance ${rupee(d.from.balance)} → <b>${rupee(d.to.balance)}</b> · Paid ${rupee(d.from.paid)} → <b>${rupee(d.to.paid)}</b>
      <span class="muted">(${esc(d.file)}, ${esc(d.sheet)} row ${d.row})</span></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn small good" data-review="${r.id}" data-act="approve">Excel is correct, use new amounts</button>
      <button class="btn small" data-review="${r.id}" data-act="reject">Keep old amounts</button></div></div>`;
  }
  const c = d.candidates[0] || {};
  return `<div class="row" style="display:grid;gap:8px"><div><b>Is this the same student?</b><br>
    Excel now has <b>${esc(d.row.name)}</b> (Sr. ${esc(d.row.sr_no || "–")}) in ${esc(d.row.medium)} ${esc(d.row.std)}-${esc(d.row.section || "")},
    and we already have <b>${esc(c.name)}</b> (Sr. ${esc(c.sr_no || "–")}). <span class="muted">(${esc(d.file)}, ${esc(d.sheet)} row ${d.row_number})</span></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn small good" data-review="${r.id}" data-act="approve">Same student (keep call history)</button>
    <button class="btn small" data-review="${r.id}" data-act="reject">Different student (add new)</button></div></div>`;
}
async function showRun(id) {
  const dr = $("#drawer"); dr.hidden = false; $("#scrim").hidden = false;
  const r = await api(`/sync/runs/${id}`);
  const s = r.summary || {};
  dr.innerHTML = `<div class="panel-head"><h2>${esc(r.file_path.split(/[\\/]/).pop())}</h2><button class="btn small" data-close>Close</button></div>
    <p>${runPill(r.status)} ${ftime(r.started_at)}${r.dry_run ? " · check only" : ""}</p>
    <dl class="kv"><dt>Sheets read</dt><dd>${esc((s.sheets_read || []).join(", ") || "–")}</dd>
      <dt>Sheets with problems</dt><dd>${esc((s.sheets_failed || []).join(", ") || "None")}</dd>
      <dt>New students</dt><dd>${s.new_students ?? "–"}</dd><dt>Fee updates</dt><dd>${s.fee_updates ?? "–"}</dd>
      <dt>To check</dt><dd>${s.needs_review ?? "–"}</dd><dt>Not in file any more</dt><dd>${s.missing_from_file ?? "–"}</dd></dl>
    <h3>Problems found</h3>
    <div class="list">${r.issues.filter((i) => i.level !== "info").map((i) => `<div class="row"><span><span class="pill ${i.level === "error" ? "due" : "amber"}">${i.level === "error" ? "Not synced" : "Check"}</span> ${esc(i.message)}</span>
      <span class="muted">${esc(i.sheet || "")}${i.row_number ? " row " + i.row_number : ""}</span></div>`).join("") || '<p class="muted">No problems.</p>'}</div>`;
}

// ------------------------------------------------------------------ router
const PAGES = {
  dashboard: ["Dashboard", pageDashboard], students: ["Students", pageStudents], fees: ["Fee Collection", pageFees],
  calls: ["Call History", pageCalls], messages: ["Messages", pageMessages], sync: ["Excel Sync", pageSync],
};
let current = "";
async function route(keepScroll = false) {
  const [name, query] = (location.hash.slice(1) || "dashboard").split("?");
  const key = PAGES[name] ? name : "dashboard";
  const params = Object.fromEntries(new URLSearchParams(query || ""));
  const [title, fn] = PAGES[key];
  $("#pageTitle").textContent = title;
  document.title = `${title} · Fees Calling Desk`;
  document.querySelectorAll("#nav a").forEach((a) => (a.dataset.page === key ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  $("#side").classList.remove("open"); $("#menuBtn").setAttribute("aria-expanded", "false");
  const main = $("#main");
  const fresh = main.cloneNode(false); main.replaceWith(fresh);  // drop old listeners
  fresh.innerHTML = `<p class="muted">Loading…</p>`;
  if (!keepScroll || current !== key) window.scrollTo(0, 0);
  current = key;
  try { await fn(fresh, params); } catch (e) { fresh.innerHTML = `<div class="note bad">${esc(e.message)}. Is the SchoolOps service running?</div>`; }
  refreshBadge();
}
async function refreshBadge() {
  try {
    const st = await api("/sync/status");
    const b = $("#reviewBadge"); b.hidden = !st.pending_reviews; b.textContent = st.pending_reviews;
    $("#modeNote").textContent = st.test_mode ? "Test mode: synthetic students only. Messages are previews; nothing is sent."
      : "Messages are previews; nothing is sent.";
  } catch {}
}

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]") || e.target.id === "scrim") { closeDrawer(); return; }
  const o = e.target.closest("[data-open]");
  if (o && !e.target.closest("a,button,input")) openStudent(o.dataset.open);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDrawer();
  if (e.key === "Enter" && e.target.matches("tr[data-open]")) openStudent(e.target.dataset.open);
  if (e.key === "Enter" && e.target.matches("tr[data-run]")) showRun(e.target.dataset.run);
});
$("#menuBtn").onclick = () => { const s = $("#side"); s.classList.toggle("open"); $("#menuBtn").setAttribute("aria-expanded", s.classList.contains("open")); };
$("#caller").value = store.get("fcd_caller", "");
$("#caller").addEventListener("input", (e) => store.set("fcd_caller", e.target.value));
window.addEventListener("hashchange", () => { closeDrawer(); route(); });

api("/filters").then((m) => { META = m; route(); }).catch((e) => { $("#main").innerHTML = `<div class="note bad">Cannot reach the SchoolOps service (${esc(e.message)}).</div>`; });
})();
