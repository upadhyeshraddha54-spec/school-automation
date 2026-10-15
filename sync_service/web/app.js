/* Fees Calling Desk – single-staff UI. Plain JS, talks to the SchoolOps API on the same server. */
(() => {
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rupee = (v) => (v == null ? "–" : "₹" + Math.round(Number(v)).toLocaleString("en-IN"));
const fdate = (s) => (s ? new Date(s.length === 10 ? s + "T00:00:00" : s).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "");
const fshort = (s) => (s ? new Date(s.length === 10 ? s + "T00:00:00" : s).toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : "");
const ftime = (s) => (s ? new Date(s).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) : "");
const todayISO = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};
const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== "" && v != null)).toString();
const caller = () => store.get("fcd_caller", "") || null;

// Call results (ids are stable; labels come from the server)
const ANSWERS = [["will_pay", "g"], ["gave_date", "b"], ["part_payment", "b"], ["call_later", ""],
                 ["no_answer", ""], ["phone_off", ""], ["wrong_number", ""], ["already_paid", ""], ["complaint", ""]];
const NEEDS_DETAIL = new Set(["gave_date", "part_payment", "call_later"]);
const REASON_CLASS = { missed_promise: "due", due_today: "amber", not_called: "blue", recontact: "grey" };
let META = { outcomes: {}, outcome_rules: {}, std: [], medium: [], section: [], languages: {}, follow_up_filters: {} };
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
  toastTimer = setTimeout(() => ($("#toast").innerHTML = ""), undo ? 8000 : 4000);
}
const fail = (e) => toast(e.message || String(e), { bad: true });

// ------------------------------------------------------------------ shared pieces
function selectHTML(name, labelText, options, value, allText = "All") {
  return `<label>${labelText}<select data-f="${name}"><option value="">${allText}</option>${options.map((o) => {
    const [v, t] = Array.isArray(o) ? o : [o, o];
    return `<option value="${esc(v)}" ${String(v) === String(value ?? "") ? "selected" : ""}>${esc(t)}</option>`; }).join("")}</select></label>`;
}
const stdName = (s) => (/^\d+$/.test(s) ? `Std ${s}` : s);
function classFilters(st, { search = false, fee = false, followUp = false, outcome = false, placeholder } = {}) {
  return `<div class="filters">
    ${search ? `<label>Search<input type="search" data-f="search" value="${esc(st.search || "")}" placeholder="${esc(placeholder || "Name, phone or Sr. No.")}"></label>` : ""}
    ${selectHTML("medium", "Medium", META.medium, st.medium)}
    ${selectHTML("std", "Class", META.std.map((s) => [s, stdName(s)]), st.std)}
    ${selectHTML("section", "Section", META.section, st.section)}
    ${fee ? selectHTML("fee_status", "Fees", [["due", "Fees due"], ["no_balance_in_excel", "No balance in Excel"], ["unknown", "Not in Excel yet"]], st.fee_status) : ""}
    ${followUp ? selectHTML("follow_up", "Follow-up", Object.entries(META.follow_up_filters), st.follow_up) : ""}
    ${outcome ? selectHTML("outcome", "Result", Object.entries(META.outcomes), st.outcome) : ""}
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
function followUpText(k) {
  if (!k.fu_kind) return '<span class="muted">–</span>';
  const missed = ["promise", "part_payment"].includes(k.fu_kind) && k.fu_due && k.fu_due < todayISO();
  return `<span class="pill ${missed ? "due" : ["dispute", "verify_payment", "fix_number"].includes(k.fu_kind) ? "amber" : "blue"}">${esc(k.follow_up_label || META.follow_up_labels?.[k.fu_kind] || k.fu_kind)}</span>${k.fu_due ? `<br><span class="muted">${missed ? "was due " : ""}${fshort(k.fu_due)}</span>` : ""}`;
}

/* Record one call for one or more children of the same parent. */
function callForm(kids, onDone, { preselect } = {}) {
  const box = document.createElement("div");
  box.className = "callcard";
  const due = kids.filter((k) => Number(k.balance) > 0);
  const chosen = new Set(preselect || due.map((k) => k.id));
  let pending = null;
  const draw = () => {
    const rule = pending && META.outcome_rules[pending];
    box.innerHTML = `${kids.length > 1 ? `<div class="kids"><b>Save this call for:</b>${kids.map((k) => `<label class="kid"><input type="checkbox" data-kid="${k.id}" ${chosen.has(k.id) ? "checked" : ""} ${Number(k.balance) > 0 ? "" : "disabled"}>
        <span><b>${esc(k.name)}</b> · ${esc(k.klass)}</span><span class="amt">${rupee(k.balance)}</span></label>`).join("")}
        <p class="muted small-note">This call is saved for every ticked child. Each child keeps their own fee and follow-up.</p></div>` : ""}
      <b>After the call, what did the parent say?</b>
      <input data-note placeholder="Note (optional), e.g. will pay after salary" aria-label="Note" maxlength="500">
      ${pending ? `<div class="detail"><b>${esc(label(pending))}</b>
          <div class="filters">
            <label>${pending === "gave_date" ? "Which date will they pay?" : pending === "part_payment" ? "Pay by (optional)" : "Call again on (optional)"}
              <input type="date" data-date min="${todayISO()}"></label>
            ${pending === "part_payment" ? '<label>Amount promised (optional)<input type="number" min="1" step="1" inputmode="numeric" data-amount placeholder="₹"></label>' : ""}
          </div>
          ${rule && rule.default_days != null ? `<p class="muted small-note">If you leave the date empty, the follow-up is in ${rule.default_days} day${rule.default_days === 1 ? "" : "s"}.</p>` : ""}
          <div class="row-btns"><button class="btn main" data-save>Save</button><button class="btn" data-back>Back</button></div></div>`
        : `<div class="answers">${ANSWERS.map(([o, c]) => `<button class="ans ${c}" data-o="${o}">${esc(label(o))}</button>`).join("")}</div>`}`;
  };
  const save = async (outcome, follow_up_date, amount) => {
    const ids = kids.length > 1 ? [...chosen] : [kids[0].id];
    if (!ids.length) { toast("Tick at least one child", { bad: true }); return; }
    try {
      const r = await api("/calls", { method: "POST", body: {
        student_ids: ids, outcome, follow_up_date: follow_up_date || null, promised_amount: amount || null,
        note: box.querySelector("[data-note]").value.trim() || null, caller: caller() } });
      toast(`Saved: ${label(outcome)}${ids.length > 1 ? ` for ${ids.length} children` : ""}`, { undo: async () => {
        try { await api(`/calls/${r.id}`, { method: "DELETE" }); toast("Call removed"); onDone(); } catch (e) { fail(e); } } });
      onDone();
    } catch (e) { fail(e); }
  };
  box.addEventListener("change", (e) => {
    const k = e.target.dataset.kid; if (!k) return;
    e.target.checked ? chosen.add(+k) : chosen.delete(+k);
  });
  box.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    const note = box.querySelector("[data-note]").value;
    if (b.dataset.o) {
      if (NEEDS_DETAIL.has(b.dataset.o)) { pending = b.dataset.o; draw(); box.querySelector("[data-note]").value = note; box.querySelector("[data-date]").focus(); }
      else save(b.dataset.o);
    } else if ("back" in b.dataset) { pending = null; draw(); box.querySelector("[data-note]").value = note; }
    else if ("save" in b.dataset) {
      const d = box.querySelector("[data-date]"), a = box.querySelector("[data-amount]");
      if (pending === "gave_date" && !d.value) { d.style.borderColor = "var(--red)"; d.focus(); toast("Please choose the date", { bad: true }); return; }
      save(pending, d.value, a && a.value ? Number(a.value) : null);
    }
  });
  draw();
  return box;
}

function childLine(k) {
  return `<div class="child"><div><b>${esc(k.name)}</b> <span class="muted">${esc(k.klass)}${k.sr_no ? " · Sr. " + esc(k.sr_no) : ""}</span>
      ${k.follow_up_label ? `<div class="muted">${esc(k.follow_up_label)}${k.fu_due ? " · " + fshort(k.fu_due) : ""}${k.state === "missed_promise" || k.promise_missed ? ' <span class="pill due">Missed</span>' : ""}</div>` : ""}
      ${k.paid_since_follow_up ? `<div class="pill zero">Part paid ${rupee(k.paid_since_follow_up)} (Excel)</div>` : ""}
      ${k.last_outcome ? `<div class="muted">Last call ${fshort(k.last_call_at)}: ${esc(k.last_outcome_label || label(k.last_outcome))}</div>` : ""}</div>
    <div class="due"><span class="amt">${rupee(k.balance)}</span><small>${Number(k.balance) > 0 ? "due" : "nothing due"}</small></div></div>`;
}

// ------------------------------------------------------------------ drawers: student and family
async function openStudent(id) {
  const dr = $("#drawer");
  dr.hidden = false; $("#scrim").hidden = false;
  dr.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const s = await api(`/students/${id}`);
    const fu = s.follow_ups.find((f) => f.status === "open");
    const kid = { ...s, fu_kind: fu && fu.kind, fu_due: fu && fu.due_date, follow_up_label: fu && fu.label };
    dr.innerHTML = `<div class="panel-head"><h2>${esc(s.name)}</h2><button class="btn small" data-close>Close</button></div>
      ${s.missing_since ? `<div class="note warn">Not in the latest Excel file (since ${fdate(s.missing_since)}). Kept with all history.</div>` : ""}
      <dl class="kv"><dt>Class</dt><dd>${esc(s.klass)}</dd><dt>Sr. No.</dt><dd>${esc(s.sr_no || "–")}</dd>
        ${s.parent_name ? `<dt>Parent</dt><dd>${esc(s.parent_name)}</dd>` : ""}
        <dt>Phone</dt><dd>${s.contact ? `<a class="phone" href="tel:${esc(s.contact)}">${esc(s.contact)}</a>` : "–"}</dd>
        <dt>Total fee</dt><dd>${rupee(s.total_fee)}</dd><dt>Paid</dt><dd>${rupee(s.paid)}</dd>
        <dt>Balance</dt><dd>${rupee(s.balance)} <span class="muted">(Excel, ${fdate(s.fee_as_of)})</span></dd>
        <dt>Next step</dt><dd>${followUpText(kid)}</dd></dl>
      <section class="panel"><div class="panel-head"><h3>Family</h3><button class="btn small" data-family="${s.family_id}">Open parent view</button></div>
        ${s.siblings.length ? s.siblings.map((x) => `<div class="row"><span><b>${esc(x.name)}</b> · ${esc(x.klass)}</span><span class="amt">${rupee(x.balance)}</span></div>`).join("")
          : '<p class="muted" style="margin:0">No brothers or sisters linked. Open the parent view to link one.</p>'}</section>
      <div id="drawerCall"></div>
      <a class="btn" href="#messages?student=${s.id}" data-close>Prepare a message</a>
      <h3>Call history</h3>
      <div class="list">${s.calls.length ? s.calls.map((c) => `<div class="row"><span><b>${esc(c.outcome_label)}</b>${c.follow_up_date ? " · pays by " + fdate(c.follow_up_date) : ""}${c.promised_amount ? " · " + rupee(c.promised_amount) : ""}${c.note ? " · “" + esc(c.note) + "”" : ""}</span>
        <span class="muted">${ftime(c.called_at)}${c.caller ? " · " + esc(c.caller) : ""}</span></div>`).join("") : '<p class="muted">No calls yet.</p>'}</div>
      <h3>Follow-up history</h3>
      <div class="list">${s.follow_ups.length ? s.follow_ups.map((f) => `<div class="row"><span><b>${esc(f.label)}</b>${f.due_date ? " · " + fdate(f.due_date) : ""}</span>
        <span>${f.status === "open" ? '<span class="pill blue">Open</span>' : `<span class="pill ${f.status === "missed" ? "due" : f.status === "paid" ? "zero" : "grey"}">${esc({ done: "Done", missed: "Missed", paid: "Paid (Excel)", cancelled: "Cancelled" }[f.status] || f.status)}</span>`}</span></div>`).join("") : '<p class="muted">None.</p>'}</div>
      <h3>Fee history (from Excel)</h3>
      <div class="tablewrap"><table style="min-width:420px"><thead><tr><th>Date</th><th class="num">Total</th><th class="num">Paid</th><th class="num">Balance</th></tr></thead>
        <tbody>${s.fee_history.map((f) => `<tr><td>${fdate(f.captured_at)}${f.source !== "excel" ? ' <span class="pill grey">reviewed</span>' : ""}</td><td class="num">${rupee(f.total_fee)}</td><td class="num">${rupee(f.paid)}</td><td class="num">${rupee(f.balance)}</td></tr>`).join("")}</tbody></table></div>`;
    if (Number(s.balance) > 0) $("#drawerCall", dr).append(callForm([kid], () => { openStudent(id); route(true); }));
  } catch (e) { dr.innerHTML = `<button class="btn small" data-close>Close</button><p>${esc(e.message)}</p>`; }
}

async function openFamily(fid) {
  const dr = $("#drawer");
  dr.hidden = false; $("#scrim").hidden = false;
  dr.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const f = await api(`/families/${fid}`);
    const title = f.display_name || `Parent of ${f.children.map((k) => k.name).join(" & ")}`;
    dr.innerHTML = `<div class="panel-head"><h2>${esc(title)}</h2><button class="btn small" data-close>Close</button></div>
      <p style="margin:0">${f.phones.map((p) => `<a class="phone" href="tel:${esc(p)}">${esc(p)}</a>`).join(" · ") || '<span class="pill amber">No phone number</span>'}
        ${f.verified ? '<span class="pill zero">Confirmed family</span>' : ""}</p>
      <div class="card"><span class="v">${rupee(f.total_due)}</span><span class="k">Total due for ${f.children.length} child${f.children.length > 1 ? "ren" : ""} (each child counted once)</span></div>
      <div class="list">${f.children.map((k) => `<div class="familykid">${childLine(k)}
        <div class="muted small-note">Total ${rupee(k.total_fee)} · Paid ${rupee(k.paid)}${k.fee_as_of ? " · Excel " + fshort(k.fee_as_of) : ""}
          ${f.children.length > 1 ? ` · <button class="btn link" data-unlink="${k.id}">Not this parent's child</button>` : ""}</div></div>`).join("")}</div>
      <div id="famCall"></div>
      <a class="btn" href="#messages?family=${f.id}" data-close>Prepare one message for all children</a>
      <details><summary>Link a brother or sister</summary>
        <div class="filters"><label style="flex:1">Search student<input type="search" id="linkSearch" placeholder="Name or phone"></label></div>
        <div class="list" id="linkResults"></div></details>
      <h3>Calls with this parent</h3>
      <div class="list">${f.calls.length ? f.calls.map((c) => `<div class="row"><span><b>${esc(c.outcome_label)}</b> · ${esc(c.children)}${c.note ? " · “" + esc(c.note) + "”" : ""}</span><span class="muted">${ftime(c.called_at)}</span></div>`).join("") : '<p class="muted">No calls yet.</p>'}</div>`;
    if (f.children.some((k) => Number(k.balance) > 0)) $("#famCall", dr).append(callForm(f.children, () => { openFamily(fid); route(true); }));
    let t;
    $("#linkSearch", dr).addEventListener("input", (e) => {
      clearTimeout(t);
      t = setTimeout(async () => {
        const q = e.target.value.trim();
        if (!q) { $("#linkResults", dr).innerHTML = ""; return; }
        const d = await api("/students?" + qs({ search: q, page_size: 6 }));
        $("#linkResults", dr).innerHTML = d.items.filter((s) => s.family_id !== f.id).map((s) => `<div class="row"><span><b>${esc(s.name)}</b> · ${esc(s.klass)} · ${esc(s.contact || "")}</span><button class="btn small" data-link="${s.id}">Link</button></div>`).join("") || '<p class="muted">No match.</p>';
      }, 300);
    });
    dr.onclick = async (e) => {
      const l = e.target.closest("[data-link]"), u = e.target.closest("[data-unlink]");
      try {
        if (l) { await api(`/families/${f.id}/link`, { method: "POST", body: { student_id: +l.dataset.link } }); toast("Linked as brother/sister"); openFamily(f.id); }
        if (u) { await api(`/students/${u.dataset.unlink}/unlink`, { method: "POST" }); toast("Separated from this parent"); openFamily(f.id); }
      } catch (er) { fail(er); }
    };
  } catch (e) { dr.innerHTML = `<button class="btn small" data-close>Close</button><p>${esc(e.message)}</p>`; }
}
function closeDrawer() { const d = $("#drawer"); d.hidden = true; d.onclick = null; $("#scrim").hidden = true; }

// ------------------------------------------------------------------ Dashboard
const dashState = store.get("fcd_dash2", { search: "", medium: "", std: "", section: "" });
let qIndex = 0, fuWhen = "overdue";

async function pageDashboard(main) {
  const [d, classes] = await Promise.all([api("/dashboard"), api("/classes")]);
  const targets = store.get("fcd_targets", {});
  const camp = d.campaign;
  main.innerHTML = `
    <section class="cards">
      <div class="card"><span class="v">${rupee(d.pending_amount)}</span><span class="k">Fees pending · ${d.families_due} parents</span></div>
      <div class="card"><span class="v">${d.families_called_today} / ${d.daily_target}</span><span class="k">Parents called today</span>
        <div class="track"><i style="width:${d.progress_pct}%"></i></div></div>
      <a class="card ${d.overdue_promises ? "alert" : ""}" href="#dashboard" data-fu="overdue"><span class="v">${d.overdue_promises}</span><span class="k">Missed payment promises</span></a>
      <a class="card" href="#dashboard" data-fu="today"><span class="v">${d.follow_ups_today}</span><span class="k">Follow-ups due today</span></a>
      ${d.needs_action ? `<a class="card warn" href="#dashboard" data-fu="needs_action"><span class="v">${d.needs_action}</span><span class="k">Need office action</span></a>` : ""}
      ${d.pending_reviews ? `<a class="card warn" href="#students?view=sync"><span class="v">${d.pending_reviews}</span><span class="k">Excel changes to check</span></a>` : ""}
    </section>
    <section class="panel campaign">${camp ? `
      <div class="panel-head"><div><h2>${esc(camp.name)}</h2><span class="muted">Campaign since ${fdate(camp.started_at)}${Object.keys(camp.filters || {}).length ? " · " + esc(Object.values(camp.filters).map(stdName).join(" · ")) : " · all classes"}</span></div>
        <button class="btn small" id="endCamp">End campaign</button></div>
      <div class="plan"><span><b>${camp.progress.families_contacted}</b> of ${camp.progress.families_in_scope} parents contacted</span>
        <span><b>${rupee(camp.progress.collected)}</b> collected (Excel) of ${rupee(camp.progress.due_at_start)}</span>
        <span><b>${camp.progress.students_cleared}</b> students fully paid</span>
        <span>Daily target <input class="target" type="number" min="1" id="targetIn" value="${d.daily_target}" aria-label="Daily target"></span></div>
      <div class="track"><i style="width:${camp.progress.families_in_scope ? Math.min(100, 100 * camp.progress.families_contacted / camp.progress.families_in_scope) : 0}%"></i></div>`
    : `<div class="panel-head"><h2>Fee campaign</h2><span>Daily target <input class="target" type="number" min="1" id="targetIn" value="${d.daily_target}" aria-label="Daily target"></span></div>
      <details><summary>Start a campaign (e.g. start of the year, when most fees are unpaid)</summary>
        <div class="filters" id="campForm"><label>Name<input id="campName" value="Fees ${new Date().getFullYear()}-${String((new Date().getFullYear() + 1) % 100).padStart(2, "0")}"></label>
          ${selectHTML("medium", "Medium", META.medium, "")}${selectHTML("std", "Class", META.std.map((s) => [s, stdName(s)]), "")}${selectHTML("section", "Section", META.section, "")}
          <label>Parents per day<input type="number" id="campTarget" min="1" value="${d.daily_target}"></label>
          <button class="btn main" id="startCamp">Start</button></div></details>`}
    </section>
    <div class="grid2">
      <section class="panel" id="queuePanel">
        <div class="panel-head"><h2>Call next parent</h2><span class="muted" id="qCounts"></span></div>
        ${classFilters(dashState, { search: true, placeholder: "Student or phone" })}
        <div id="qCurrent"><p class="muted">Loading…</p></div>
        <div id="qNext"></div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>Follow-ups</h2></div>
        <div class="seg">${[["overdue", "Missed"], ["today", "Today"], ["upcoming", "Upcoming"], ["needs_action", "Office"]].map(([k, t]) => `<button class="btn small ${fuWhen === k ? "main" : ""}" data-fu="${k}">${t}</button>`).join("")}</div>
        <div class="list" id="fuList"><p class="muted">Loading…</p></div>
      </section>
    </div>
    <section class="panel">
      <div class="panel-head"><h2>Each class today</h2><span class="muted">Optional calling target per class</span></div>
      <div class="tablewrap"><table><thead><tr><th>Class</th><th class="num">Fees due</th><th class="num">Pending</th><th class="num">Called today</th><th>Target</th><th>Progress</th></tr></thead>
      <tbody>${classes.map((c) => { const t = +targets[c.klass] || 0; const pct = t ? Math.min(100, c.called_today / t * 100) : 0;
        return `<tr><td><b>${esc(c.klass)}</b></td><td class="num">${c.students_due}</td><td class="num">${rupee(c.pending_amount)}</td>
          <td class="num">${c.called_today}${t ? " / " + t : ""}</td>
          <td><input class="target" type="number" min="0" inputmode="numeric" data-t="${esc(c.klass)}" value="${t || ""}" placeholder="0" aria-label="Target for ${esc(c.klass)}"></td>
          <td><div class="track"><i style="width:${pct}%"></i></div></td></tr>`; }).join("") || '<tr><td colspan="6" class="empty">No students yet. Use Students &amp; Fees → + Add Excel.</td></tr>'}</tbody></table></div>
    </section>`;

  const loadQueue = async () => {
    try {
      const q = await api("/queue?" + qs({ ...dashState, page_size: 40 }));
      const c = q.counts;
      $("#qCounts").textContent = `${q.total} to call · ${q.families_done_today} done today`;
      if (qIndex >= q.items.length) qIndex = 0;
      const fam = q.items[qIndex];
      const cur = $("#qCurrent");
      if (!fam) { cur.innerHTML = `<div class="empty">Nobody left to call${dashState.std || dashState.medium || dashState.search ? " with these filters" : ""} today. Parents who gave a date come back on that date.</div>`; $("#qNext").innerHTML = ""; return; }
      const name = fam.parent_name || `Parent of ${fam.children.map((k) => k.name).join(" & ")}`;
      cur.innerHTML = `<div class="famhead"><div><span class="pill ${REASON_CLASS[fam.priority]}">${esc(fam.reason)}</span>
          <div class="name">${esc(name)}</div>
          <div>${fam.phones.map((p) => `<a class="phone" href="tel:${esc(p)}">${esc(p)}</a>`).join(" · ") || '<span class="pill amber">No phone number</span>'}</div></div>
          <div class="due"><span class="amt">${rupee(fam.total_due)}</span><small>${fam.children.length > 1 ? "total for " + fam.children.length + " children" : "fees due"}</small></div></div>
        <div class="list">${fam.children.map(childLine).join("")}</div>
        <div class="row-btns"><button class="btn small" data-family="${fam.family_id}">Parent view</button><button class="btn small" id="skipBtn">Skip for now</button></div>`;
      cur.append(callForm(fam.children, () => { pageDashboard(main).catch(fail); }, { preselect: fam.call_children }));
      $("#skipBtn").onclick = () => { qIndex += 1; loadQueue(); };
      const next = q.items.slice(qIndex + 1, qIndex + 7);
      $("#qNext").innerHTML = next.length ? `<h3>Up next</h3><div class="list">${next.map((n, i) => `<button class="row nextrow" data-jump="${qIndex + 1 + i}">
          <span><span class="pill ${REASON_CLASS[n.priority]}">${esc(n.reason)}</span> ${esc(n.parent_name || n.children.map((k) => k.name).join(" & "))}${n.children.length > 1 ? ` <span class="muted">(${n.children.length} children)</span>` : ""}</span>
          <span class="amt">${rupee(n.total_due)}</span></button>`).join("")}</div>` : "";
      $("#qNext").onclick = (e) => { const b = e.target.closest("[data-jump]"); if (b) { qIndex = +b.dataset.jump; loadQueue(); window.scrollTo({ top: $("#queuePanel").offsetTop - 20 }); } };
    } catch (e) { fail(e); }
  };
  const loadFollowUps = async () => {
    const rows = await api("/follow-ups?when=" + fuWhen);
    $("#fuList").innerHTML = rows.length ? rows.map((f) => `<div class="row" data-open="${f.id}" style="cursor:pointer">
        <span><b>${esc(f.name)}</b> · ${esc(f.klass)}<br><span class="muted">${esc(f.follow_up_label || "")}${f.follow_up_date ? " · " + fdate(f.follow_up_date) : ""}${f.note ? " · “" + esc(f.note) + "”" : ""}</span>
        ${f.paid_since_follow_up ? `<br><span class="pill zero">Part paid ${rupee(f.paid_since_follow_up)}</span>` : ""}</span>
        <span style="display:grid;gap:6px;justify-items:end"><span class="amt">${rupee(f.balance)}</span>${fuWhen === "needs_action" ? `<button class="btn small" data-done="${f.fu_id}">Mark done</button>` : ""}</span></div>`).join("")
      : `<p class="muted">${{ overdue: "No missed promises.", today: "Nothing due today.", upcoming: "No dates promised yet.", needs_action: "Nothing waiting for the office." }[fuWhen]}</p>`;
  };
  bindFilters($("#queuePanel"), dashState, () => { qIndex = 0; store.set("fcd_dash2", dashState); loadQueue(); });
  main.addEventListener("click", async (e) => {
    const fu = e.target.closest("[data-fu]");
    if (fu) { e.preventDefault(); fuWhen = fu.dataset.fu; main.querySelectorAll(".seg [data-fu]").forEach((b) => b.classList.toggle("main", b.dataset.fu === fuWhen)); loadFollowUps().catch(fail); $("#fuList").scrollIntoView({ block: "nearest" }); return; }
    const dn = e.target.closest("[data-done]");
    if (dn) { e.stopPropagation(); try { await api(`/follow-ups/${dn.dataset.done}/close`, { method: "POST", body: { reason: "Done by office", by: caller() } }); toast("Marked done"); pageDashboard(main).catch(fail); } catch (er) { fail(er); } }
  });
  main.querySelectorAll("[data-t]").forEach((i) => i.addEventListener("change", () => { targets[i.dataset.t] = i.value; store.set("fcd_targets", targets); pageDashboard(main).catch(fail); }));
  $("#targetIn").addEventListener("change", async (e) => { try { await api("/settings/daily-target", { method: "PUT", body: { daily_target: +e.target.value } }); toast("Daily target saved"); pageDashboard(main).catch(fail); } catch (er) { fail(er); } });
  if ($("#endCamp")) $("#endCamp").onclick = async () => { try { await api(`/campaigns/${camp.id}/close`, { method: "POST" }); toast("Campaign ended"); pageDashboard(main).catch(fail); } catch (er) { fail(er); } };
  if ($("#startCamp")) $("#startCamp").onclick = async () => {
    const f = $("#campForm"), val = (n) => f.querySelector(`[data-f=${n}]`).value || null;
    try { await api("/campaigns", { method: "POST", body: { name: $("#campName").value, medium: val("medium"), std: val("std"), section: val("section"), daily_target: +$("#campTarget").value || 40 } }); toast("Campaign started"); qIndex = 0; pageDashboard(main).catch(fail); } catch (er) { fail(er); }
  };
  loadQueue();
  loadFollowUps().catch(fail);
}

// ------------------------------------------------------------------ Students & Fees (+ Excel sync & review)
const stuState = { view: "students", search: "", medium: "", std: "", section: "", fee_status: "", follow_up: "", sort: "class", page: 1, page_size: 25 };
async function pageStudentsFees(main, params) {
  stuState.view = params.view === "sync" ? "sync" : "students";  // the address decides the view
  const [classes, st] = await Promise.all([api("/classes"), api("/sync/status")]);
  const total = classes.reduce((a, c) => a + Number(c.pending_amount), 0);
  const due = classes.reduce((a, c) => a + Number(c.students_due), 0);
  const all = classes.reduce((a, c) => a + Number(c.students), 0);
  main.innerHTML = `
    <div class="panel-head"><div class="seg"><button class="btn small ${stuState.view === "students" ? "main" : ""}" data-view="students">Students</button>
        <button class="btn small ${stuState.view === "sync" ? "main" : ""}" data-view="sync">Excel sync &amp; review${st.pending_reviews ? ` <span class="badge">${st.pending_reviews}</span>` : ""}</button></div>
      <button class="btn main" id="addExcel">+ Add Excel</button></div>
    <section class="cards">
      <div class="card"><span class="v">${rupee(total)}</span><span class="k">Total fees pending</span></div>
      <div class="card"><span class="v">${due}</span><span class="k">Students with fees due</span></div>
      <div class="card"><span class="v">${all}</span><span class="k">Students in total</span></div>
    </section>
    <div id="view"></div>`;
  main.querySelectorAll("[data-view]").forEach((b) => (b.onclick = () => { stuState.view = b.dataset.view; history.replaceState(null, "", "#students" + (stuState.view === "sync" ? "?view=sync" : "")); pageStudentsFees(main, { view: stuState.view }).catch(fail); }));
  $("#addExcel").onclick = openImport;
  const view = $("#view");
  if (stuState.view === "sync") return syncView(view, st);
  view.innerHTML = `<p class="muted" style="margin:0">Fee amounts come from the school's Excel files. This app never marks fees as paid.</p>
    <section class="panel" id="stuPanel">
      ${classFilters(stuState, { search: true, fee: true, followUp: true })}
      <div class="filters">${selectHTML("sort", "Order", [["balance", "Highest balance first"]], stuState.sort === "balance" ? "balance" : "", "Class and Sr. No.")}</div>
      <div id="stuTable"></div></section>`;
  const load = async () => {
    const d = await api("/students?" + qs({ ...stuState, view: null, sort: stuState.sort || "class" }));
    const box = $("#stuTable");
    box.innerHTML = `<div class="tablewrap"><table><thead><tr><th class="hide-sm">Sr.</th><th>Name</th><th>Class</th><th class="hide-sm">Phone</th>
      <th class="num hide-sm">Total</th><th class="num hide-sm">Paid</th><th class="num">Balance</th><th>Fees</th><th>Next step</th><th class="hide-sm">Last call</th></tr></thead>
      <tbody>${d.items.map((s) => `<tr class="click" data-open="${s.id}" tabindex="0"><td class="hide-sm">${esc(s.sr_no || "")}</td>
        <td><b>${esc(s.name)}</b>${s.siblings > 0 ? ` <span class="pill grey" title="Linked brothers/sisters">+${s.siblings} sibling${s.siblings > 1 ? "s" : ""}</span>` : ""}</td><td>${esc(s.klass)}</td>
        <td class="hide-sm">${esc(s.contact || "–")}</td><td class="num hide-sm">${rupee(s.total_fee)}</td><td class="num hide-sm">${rupee(s.paid)}</td>
        <td class="num amt">${rupee(s.balance)}</td><td>${feePill(s)}</td><td>${followUpText(s)}</td>
        <td class="hide-sm">${s.last_outcome ? esc(label(s.last_outcome)) + `<br><span class="muted">${fdate(s.last_call_at)}</span>` : '<span class="muted">Not called</span>'}</td></tr>`).join("")
        || `<tr><td colspan="10" class="empty">${all ? "No students match. Try clearing the filters." : "No students yet. Press “+ Add Excel” to import the class files."}</td></tr>`}</tbody></table></div>`;
    box.append(pager(d, (p) => { stuState.page = p; load().catch(fail); }));
  };
  bindFilters($("#stuPanel"), stuState, () => load().catch(fail));
  await load();
}

const REASON = { balance_increased: "the balance went up", paid_decreased: "the paid amount went down" };
function reviewHTML(r) {
  const d = r.details;
  if (r.kind === "fee_change") {
    return `<div class="row" style="display:grid;gap:8px"><div><b>${esc(d.name)}</b> · ${esc(d.class)}<br>
      <span class="muted">In Excel, ${(d.reasons || []).map((x) => REASON[x] || x).join(" and ")}.</span><br>
      Balance ${rupee(d.from.balance)} → <b>${rupee(d.to.balance)}</b> · Paid ${rupee(d.from.paid)} → <b>${rupee(d.to.paid)}</b>
      <span class="muted">(${esc(d.file)}, ${esc(d.sheet)} row ${d.row})</span></div>
      <div class="row-btns"><button class="btn small good" data-review="${r.id}" data-act="approve">Excel is correct, use new amounts</button>
      <button class="btn small" data-review="${r.id}" data-act="reject">Keep old amounts</button></div></div>`;
  }
  if (r.kind === "possible_siblings") {
    return `<div class="row" style="display:grid;gap:8px"><div><b>Same parent?</b> These students share the phone number <b>${esc(d.phone)}</b>:<br>
      ${d.students.map((s) => `${esc(s.name)} <span class="muted">(${esc(s.class)})</span>`).join(" · ")}</div>
      <div class="row-btns"><button class="btn small good" data-review="${r.id}" data-act="approve">Yes, same parent</button>
      <button class="btn small" data-review="${r.id}" data-act="reject">No, different parents</button></div></div>`;
  }
  const c = d.candidates[0] || {};
  return `<div class="row" style="display:grid;gap:8px"><div><b>Is this the same student?</b><br>
    Excel now has <b>${esc(d.row.name)}</b> (Sr. ${esc(d.row.sr_no || "–")}) in ${esc(d.row.medium)} ${esc(d.row.std)}-${esc(d.row.section || "")},
    and we already have <b>${esc(c.name)}</b> (Sr. ${esc(c.sr_no || "–")}). <span class="muted">(${esc(d.file)}, ${esc(d.sheet)} row ${d.row_number})</span></div>
    <div class="row-btns"><button class="btn small good" data-review="${r.id}" data-act="approve">Same student (keep call history)</button>
    <button class="btn small" data-review="${r.id}" data-act="reject">Different student (add new)</button></div></div>`;
}
const runPill = (s) => ({ applied: '<span class="pill zero">Synced</span>', dry_run: '<span class="pill grey">Check only</span>',
  failed: '<span class="pill due">Failed</span>', running: '<span class="pill amber">Running</span>' }[s] || esc(s));
const TRIGGER = { watcher: "File saved", reconcile: "Regular re-check", api: "From the app", manual: "Command line", upload: "Add Excel" };

async function syncView(view, st) {
  const [reviews, runs] = await Promise.all([api("/review"), api("/sync/runs?page_size=15")]);
  const yes = (b) => (b ? "Yes" : "No");
  view.innerHTML = `
    ${st.test_mode ? '<div class="note info">Test mode: only synthetic test files are used. No real school data.</div>'
      : '<div class="note warn">Real school folder. From the app you can only run checks (dry runs).</div>'}
    <div class="grid2">
      <section class="panel"><h2>Sync status</h2>
        <dl class="kv"><dt>Folder</dt><dd style="word-break:break-all">${esc(st.watch_folder)}${st.folder_exists ? "" : ' <span class="pill due">not found</span>'}</dd>
          <dt>Watching for saved files</dt><dd>${yes(st.watching)}${st.watcher_error ? ` <span class="muted">(${esc(st.watcher_error)})</span>` : ""}</dd>
          <dt>Sub-folders</dt><dd>${st.recursive ? "Included" : "Not included"}</dd>
          <dt>Column mapping confirmed</dt><dd>${yes(st.mapping_confirmed)}</dd>
          <dt>Payment rules confirmed</dt><dd>${yes(st.payment_rules_confirmed)}</dd>
          <dt>Real messages</dt><dd>${st.messaging_enabled ? "Enabled" : "Off (drafts only)"}</dd></dl>
        <div><button class="btn main" id="checkBtn">Check the folder now (dry run)</button></div>
        <div id="checkResult"></div>
      </section>
      <section class="panel"><div class="panel-head"><h2>Changes to check</h2><span class="pill ${reviews.length ? "amber" : "zero"}">${reviews.length} waiting</span></div>
        <div class="list">${reviews.map(reviewHTML).join("") || '<p class="muted">Nothing to check.</p>'}</div>
      </section>
    </div>
    ${st.current_errors.length ? `<section class="panel"><h2>Problems in the latest files</h2><div class="list">${st.current_errors.map((i) => `<div class="row"><span><span class="pill due">Not synced</span> ${esc(i.message)}</span><span class="muted">${esc(i.file_path.split(/[\\/]/).pop())}${i.sheet ? " · " + esc(i.sheet) : ""}${i.row_number ? " row " + i.row_number : ""}</span></div>`).join("")}</div></section>` : ""}
    <section class="panel"><h2>Recent syncs</h2>
      <div class="tablewrap"><table><thead><tr><th>When</th><th>File</th><th>How</th><th>Result</th><th class="num">New</th><th class="num">Fee updates</th><th class="num">To check</th><th class="num">Errors</th></tr></thead>
      <tbody>${runs.items.map((r) => { const s = r.summary || {}; return `<tr class="click" data-run="${r.id}" tabindex="0"><td>${ftime(r.started_at)}</td><td>${esc(r.file_path.split(/[\\/]/).pop())}</td>
        <td>${esc(TRIGGER[r.trigger] || r.trigger)}</td><td>${runPill(r.status)}</td><td class="num">${s.new_students ?? "–"}</td><td class="num">${s.fee_updates ?? "–"}</td><td class="num">${s.needs_review ?? "–"}</td><td class="num">${s.errors ?? "–"}</td></tr>`; }).join("")
        || '<tr><td colspan="8" class="empty">No syncs yet.</td></tr>'}</tbody></table></div></section>`;
  $("#checkBtn").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Checking…";
    try {
      const r = await api("/sync/run", { method: "POST", body: { dry_run: true, force: true } });
      $("#checkResult").innerHTML = `<div class="tablewrap"><table style="min-width:480px"><thead><tr><th>File</th><th class="num">New</th><th class="num">Fee updates</th><th class="num">To check</th><th class="num">Errors</th></tr></thead><tbody>${
        r.files.map((f) => `<tr><td>${esc(f.file)}</td><td class="num">${f.new_students ?? "–"}</td><td class="num">${f.fee_updates ?? "–"}</td><td class="num">${f.needs_review ?? "–"}</td><td class="num">${f.errors ?? "–"}</td></tr>`).join("") || '<tr><td colspan="5" class="empty">No Excel files in the folder.</td></tr>'}</tbody></table></div>
        <p class="muted">Dry run: nothing was changed.</p>`;
    } catch (er) { fail(er); }
    e.target.disabled = false; e.target.textContent = "Check the folder now (dry run)";
  };
  view.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-review]");
    if (b) { try { await api(`/review/${b.dataset.review}/${b.dataset.act}?` + qs({ by: caller() }), { method: "POST" }); toast("Saved"); route(true); } catch (er) { fail(er); } return; }
    const tr = e.target.closest("[data-run]");
    if (tr) showRun(tr.dataset.run);
  });
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

/* ---------------- Add Excel: choose files -> check -> confirm -> import ---------------- */
const imp = { token: null, files: [], picked: [] };
function modal(html) { const m = $("#modal"); m.hidden = false; $("#scrim").hidden = false; m.innerHTML = `<div class="modal-box" role="dialog" aria-modal="true" aria-label="Add Excel">${html}</div>`; return m; }
async function closeImport() {
  if (imp.token) { try { await api(`/imports/${imp.token}`, { method: "DELETE" }); } catch {} }
  imp.token = null; imp.files = []; imp.picked = [];
  $("#modal").hidden = true; $("#scrim").hidden = true;
}
function openImport() {
  imp.token = null; imp.picked = [];
  const m = modal(`<div class="panel-head"><h2>Add Excel</h2><button class="btn small" data-imp-close>Close</button></div>
    <p>Choose one or more class files (.xlsx, or old .xls). You will see a preview first. <b>Nothing is saved until you press Import.</b> Your Excel files are never changed.</p>
    <label class="drop" for="impFiles"><input type="file" id="impFiles" multiple accept=".xlsx,.xlsm,.xls">
      <b>Choose Excel files</b><span class="muted">or drag them here</span></label>
    <div id="impPicked" class="list"></div>
    <div class="modal-foot"><button class="btn" data-imp-close>Cancel</button><button class="btn main" id="impCheck" disabled>Check files</button></div>`);
  const input = $("#impFiles", m), drop = $(".drop", m);
  const show = (list) => {
    imp.picked = [...list];
    $("#impPicked", m).innerHTML = imp.picked.map((f) => `<div class="row"><span>${esc(f.name)}</span><span class="muted">${Math.ceil(f.size / 1024)} KB</span></div>`).join("");
    $("#impCheck", m).disabled = !imp.picked.length;
  };
  input.onchange = () => show(input.files);
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); show(e.dataTransfer.files); });
  $("#impCheck", m).onclick = checkFiles;
}
async function checkFiles() {
  const btn = $("#impCheck"); btn.disabled = true; btn.textContent = "Checking…";
  const fd = new FormData();
  imp.picked.forEach((f) => fd.append("files", f, f.name));
  try {
    const r = await fetch("/imports/preview", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Could not check the files");
    imp.token = d.token; imp.files = d.files;
    showPreview(d);
  } catch (e) { fail(e); btn.disabled = false; btn.textContent = "Check files"; }
}
function fileStatus(f) {
  if (!f.importable) return '<span class="pill due">Cannot import</span>';
  if (f.rows_with_errors || (f.sheets_failed || []).length) return '<span class="pill amber">Ready, some rows will be skipped</span>';
  return '<span class="pill zero">Ready to import</span>';
}
function showPreview(d) {
  const m = modal(`<div class="panel-head"><h2>Check before importing</h2><button class="btn small" data-imp-close>Close</button></div>
    <p class="muted" style="margin:0">Nothing has been saved yet. Untick any file you don't want to import.</p>
    ${d.files.map((f, i) => `<section class="panel">
      <div class="panel-head"><label class="pick"><input type="checkbox" data-pick="${i}" ${f.importable ? "checked" : "disabled"}> <b>${esc(f.file)}</b></label>${fileStatus(f)}</div>
      ${f.problem ? `<div class="note bad">${esc(f.problem)}</div>` : ""}
      ${f.columns && f.columns.length ? `<div><b>Columns</b><div class="cols">${f.columns.map((c) => `<span class="pill ${c.ok ? "zero" : "due"}">${c.ok ? "✓" : "✗"} ${esc(c.heading)}${c.ok ? "" : " missing" + (c.missing_in.length ? " in " + esc(c.missing_in.join(", ")) : "")}</span>`).join("")}</div></div>` : ""}
      ${f.sheets && f.sheets.length ? `<div><b>Classes found:</b> ${f.sheets.map((s) => `${esc(s.sheet)} (${s.students})`).join(" · ")}</div>` : ""}
      ${f.plan ? `<div class="plan"><span><b>${f.plan.new_students}</b> new students</span><span><b>${f.plan.fee_updates}</b> fee updates</span>
        <span><b>${f.plan.unchanged}</b> unchanged</span><span><b>${f.plan.needs_review}</b> to check by a person</span>
        <span><b>${f.plan.rows_not_synced}</b> rows skipped</span>${f.plan.missing_from_file ? `<span><b>${f.plan.missing_from_file}</b> no longer in file (kept)</span>` : ""}</div>` : ""}
      ${f.issues && f.issues.length ? `<details><summary>${f.issues.length} problem${f.issues.length > 1 ? "s" : ""} found</summary><div class="list">${f.issues.map((x) => `<div class="row"><span><span class="pill ${x.level === "error" ? "due" : "amber"}">${x.level === "error" ? "Skipped" : "Check"}</span> ${esc(x.message)}</span><span class="muted">${esc(x.sheet || "")}${x.row ? " row " + x.row : ""}</span></div>`).join("")}</div></details>` : ""}
      ${f.rows && f.rows.length ? `<div class="tablewrap"><table><thead><tr><th>Sheet</th><th>Sr.</th><th>Name</th><th>Class</th><th>Phone</th><th class="num">Total</th><th class="num">Paid</th><th class="num">Balance</th><th></th></tr></thead>
        <tbody>${f.rows.map((r) => `<tr><td>${esc(r.sheet)}</td><td>${esc(r.sr_no || "")}</td><td>${esc(r.name)}</td><td>${esc(r.class)}</td><td>${esc(r.contact || "–")}</td>
          <td class="num">${rupee(r.total)}</td><td class="num">${rupee(r.paid)}</td><td class="num">${rupee(r.balance)}</td><td>${r.ok ? "" : '<span class="pill due">Skipped</span>'}</td></tr>`).join("")}</tbody></table></div>
        <p class="muted" style="margin:0">Showing the first ${f.rows.length} of ${f.students_in_file} students.</p>` : ""}
    </section>`).join("")}
    <div id="impConfirm"></div>
    <div class="modal-foot"><button class="btn" data-imp-close>Cancel</button><button class="btn main" id="impGo">Import selected files</button></div>`);
  const chosen = () => [...m.querySelectorAll("[data-pick]:checked")].map((c) => imp.files[+c.dataset.pick]);
  const sync = () => { $("#impGo", m).disabled = !chosen().length || !imp.token; $("#impConfirm", m).innerHTML = ""; };
  m.querySelectorAll("[data-pick]").forEach((c) => (c.onchange = sync));
  sync();
  $("#impGo", m).onclick = () => {
    const fs = chosen();
    const add = fs.reduce((a, f) => a + f.plan.new_students, 0), upd = fs.reduce((a, f) => a + f.plan.fee_updates, 0);
    const rev = fs.reduce((a, f) => a + f.plan.needs_review, 0);
    $("#impConfirm", m).innerHTML = `<div class="note warn" style="display:grid;gap:10px"><span>Import ${fs.length} file${fs.length > 1 ? "s" : ""}? This adds <b>${add}</b> new students and updates <b>${upd}</b> fee amounts${rev ? `. <b>${rev}</b> unusual changes will wait for you under “Excel sync &amp; review”` : ""}. Call records are kept.</span>
      <div class="row-btns"><button class="btn main" id="impYes">Yes, import</button><button class="btn" id="impNo">Go back</button></div></div>`;
    $("#impConfirm", m).scrollIntoView({ block: "nearest" });
    $("#impNo", m).onclick = sync;
    $("#impYes", m).onclick = async (e) => {
      e.target.disabled = true; e.target.textContent = "Importing…";
      try {
        const r = await api("/imports/confirm", { method: "POST", body: { token: imp.token, files: fs.map((f) => f.file) } });
        imp.token = null;
        const sib = r.files.reduce((a, f) => a + (f.possible_siblings || 0), 0);
        modal(`<div class="panel-head"><h2>Import finished</h2></div>
          <div class="list">${r.files.map((f) => `<div class="row"><span><b>${esc(f.file)}</b></span><span>${f.status === "applied"
            ? `<span class="pill zero">Imported</span> ${f.new_students} new · ${f.fee_updates} fee updates${f.needs_review ? ` · ${f.needs_review} to check` : ""}`
            : `<span class="pill due">Not imported</span> ${esc(f.error || "")}`}</span></div>`).join("")}</div>
          ${sib ? `<div class="note info">${sib} possible brother/sister group${sib > 1 ? "s" : ""} found (same phone number). Please confirm them under Students &amp; Fees → Excel sync &amp; review.</div>` : ""}
          <div class="modal-foot"><button class="btn main" data-imp-close>Done</button></div>`);
      } catch (er) { fail(er); e.target.disabled = false; e.target.textContent = "Yes, import"; }
    };
  };
}

// ------------------------------------------------------------------ Call Records
const callState = { search: "", medium: "", std: "", section: "", outcome: "", page: 1, page_size: 25 };
async function pageCalls(main) {
  main.innerHTML = `<section class="panel" id="callPanel"><div class="panel-head"><h2>Call records</h2><button class="btn small" id="csvBtn">Copy for Excel</button></div>
    ${classFilters(callState, { search: true, outcome: true, placeholder: "Student name or note" })}<div id="callTable"></div><textarea id="csvBox" hidden readonly style="width:100%;min-height:120px"></textarea></section>`;
  const load = async () => {
    const d = await api("/calls?" + qs(callState));
    const box = $("#callTable");
    box.innerHTML = `<div class="tablewrap"><table><thead><tr><th>When</th><th>Student</th><th>Class</th><th>Result</th><th>Pays by</th><th class="hide-sm">Note</th><th class="hide-sm">Called by</th></tr></thead>
      <tbody>${d.items.map((c) => `<tr class="click" data-open="${c.student_id}" tabindex="0"><td>${ftime(c.called_at)}</td><td><b>${esc(c.name)}</b>${c.with_siblings ? `<br><span class="muted">same call: ${esc(c.with_siblings)}</span>` : ""}</td><td>${esc(c.klass)}</td>
        <td>${esc(c.outcome_label)}${c.promised_amount ? " · " + rupee(c.promised_amount) : ""}</td><td>${fdate(c.follow_up_date)}</td><td class="hide-sm">${esc(c.note || "")}</td><td class="hide-sm">${esc(c.caller || "")}</td></tr>`).join("")
        || `<tr><td colspan="7" class="empty">No calls saved yet.</td></tr>`}</tbody></table></div>`;
    box.append(pager(d, (p) => { callState.page = p; load().catch(fail); }));
  };
  $("#csvBtn").onclick = async () => {
    try {
      const rows = [["Date", "Student", "Class", "Result", "Pays by", "Amount", "Note", "Called by"]];
      for (let p = 1; p <= 50; p++) {
        const d = await api("/calls?" + qs({ ...callState, page: p, page_size: 100 }));
        d.items.forEach((c) => rows.push([ftime(c.called_at), c.name, c.klass, c.outcome_label, c.follow_up_date || "", c.promised_amount || "", c.note || "", c.caller || ""]));
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

// ------------------------------------------------------------------ Messages (drafts; nothing is sent)
const msgState = { status: "", page: 1, lang: store.get("fcd_lang", "mr"), draft: null };
async function pageMessages(main, params) {
  main.innerHTML = `<div class="note warn">Sending is turned off. Prepare a reminder, edit it, approve it and copy it. No SMS, WhatsApp or email is sent by this app.</div>
    <section class="panel"><h2>1. Choose a parent</h2>
      <div class="filters"><label style="flex:1">Search<input type="search" id="msgSearch" placeholder="Student name or phone number"></label></div>
      <div class="list" id="msgResults"></div><div id="msgFamily"></div></section>
    <div id="msgEditor"></div>
    <section class="panel" id="draftPanel"><div class="panel-head"><h2>Drafts</h2>
      <div class="filters">${selectHTML("status", "Show", [["draft", "Drafts"], ["approved", "Approved"], ["discarded", "Discarded"]], msgState.status)}</div></div>
      <div id="draftList"></div></section>`;
  let t;
  $("#msgSearch").addEventListener("input", (e) => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const q = e.target.value.trim();
      if (!q) { $("#msgResults").innerHTML = ""; return; }
      const d = await api("/students?" + qs({ search: q, page_size: 6 }));
      $("#msgResults").innerHTML = d.items.map((s) => `<a class="row" href="#messages?family=${s.family_id}" style="text-decoration:none;color:inherit"><span><b>${esc(s.name)}</b> · ${esc(s.klass)}${s.siblings > 0 ? ` <span class="muted">+${s.siblings} sibling${s.siblings > 1 ? "s" : ""}</span>` : ""}</span><span class="amt">${rupee(s.balance)}</span></a>`).join("") || '<p class="muted">No match.</p>';
    }, 300);
  });
  let fid = params.family;
  if (!fid && params.student) fid = (await api(`/students/${params.student}`)).family_id;
  if (fid) {
    const f = await api(`/families/${fid}`);
    const box = $("#msgFamily");
    box.innerHTML = `<div class="panel" style="gap:10px"><b>${esc(f.display_name || "Parent")} · ${esc(f.phones.join(", ") || "no phone number")}</b>
      ${f.children.map((k) => `<label class="kid"><input type="checkbox" data-mk="${k.id}" ${Number(k.balance) > 0 && (!params.student || String(k.id) === String(params.student) || !params.student) ? "checked" : ""} ${k.balance == null ? "disabled" : ""}>
        <span><b>${esc(k.name)}</b> · ${esc(k.klass)}</span><span class="amt">${k.balance == null ? "no amount in Excel" : rupee(k.balance)}</span></label>`).join("")}
      <div class="filters"><label>Language<select id="msgLang">${Object.entries(META.languages).map(([k, v]) => `<option value="${k}" ${k === msgState.lang ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
        <button class="btn main" id="mkDraft">2. Prepare draft</button></div></div>`;
    $("#mkDraft").onclick = async () => {
      const ids = [...box.querySelectorAll("[data-mk]:checked")].map((c) => +c.dataset.mk);
      msgState.lang = $("#msgLang").value; store.set("fcd_lang", msgState.lang);
      try { const d = await api("/drafts", { method: "POST", body: { student_ids: ids, lang: msgState.lang } }); msgState.draft = d; editor(d); loadDrafts(); }
      catch (e) { fail(e); }
    };
  }
  const editor = (d) => {
    const box = $("#msgEditor");
    if (!d) { box.innerHTML = ""; return; }
    const pill = { draft: '<span class="pill blue">Draft</span>', approved: '<span class="pill zero">Approved</span>', discarded: '<span class="pill grey">Discarded</span>' }[d.status];
    box.innerHTML = `<section class="panel"><div class="panel-head"><h2>3. Check, edit and approve</h2><span>${pill}${d.edited ? ' <span class="pill grey">edited</span>' : ""}</span></div>
      ${(d.left_out || []).length ? `<div class="note info">Not included: ${d.left_out.map((x) => `${esc(x.name)} (${esc(x.why)})`).join(", ")}</div>` : ""}
      ${d.no_phone ? '<div class="note warn">No phone number for this parent in Excel.</div>' : ""}
      <textarea id="draftBody" lang="${esc(d.lang)}" ${d.status === "discarded" ? "readonly" : ""}>${esc(d.body)}</textarea>
      <div class="row-btns">${d.status !== "discarded" ? `<button class="btn" id="dSave">Save changes</button>` : ""}
        ${d.status === "draft" ? `<button class="btn main" id="dApprove">Approve</button>` : ""}
        <button class="btn" id="dCopy">Copy text</button>
        ${d.status !== "discarded" ? `<button class="btn bad" id="dDiscard">Discard</button>` : ""}</div>
      ${d.status === "approved" ? `<p class="muted" style="margin:0">Approved ${ftime(d.approved_at)}${d.approved_by ? " by " + esc(d.approved_by) : ""}. Sending is off: copy it and send it yourself if needed. Editing it makes it a draft again.</p>` : ""}</section>`;
    const act = async (fn, msg) => { try { const r = await fn(); msgState.draft = r; editor(r); loadDrafts(); toast(msg); } catch (e) { fail(e); } };
    if ($("#dSave")) $("#dSave").onclick = () => act(() => api(`/drafts/${d.id}`, { method: "PUT", body: { body: $("#draftBody").value } }), "Changes saved");
    if ($("#dApprove")) $("#dApprove").onclick = async () => {
      if ($("#draftBody").value !== d.body) { await act(() => api(`/drafts/${d.id}`, { method: "PUT", body: { body: $("#draftBody").value } }), "Changes saved"); }
      act(() => api(`/drafts/${msgState.draft.id}/approve?` + qs({ by: caller() }), { method: "POST" }), "Approved");
    };
    if ($("#dDiscard")) $("#dDiscard").onclick = () => act(() => api(`/drafts/${d.id}/discard`, { method: "POST" }), "Discarded");
    $("#dCopy").onclick = async () => { try { await navigator.clipboard.writeText($("#draftBody").value); toast("Copied"); } catch { $("#draftBody").select(); toast("Press Ctrl+C / Cmd+C to copy"); } };
  };
  const loadDrafts = async () => {
    const d = await api("/drafts?" + qs({ status: msgState.status, page: msgState.page, page_size: 10 }));
    const box = $("#draftList");
    box.innerHTML = `<div class="tablewrap"><table><thead><tr><th>When</th><th>Children</th><th>Language</th><th>Status</th></tr></thead><tbody>${d.items.map((m) => `<tr class="click" data-draft="${m.id}" tabindex="0"><td>${ftime(m.updated_at)}</td><td>${esc(m.children || "")}</td><td>${esc(META.languages[m.lang] || m.lang)}</td><td>${{ draft: '<span class="pill blue">Draft</span>', approved: '<span class="pill zero">Approved</span>', discarded: '<span class="pill grey">Discarded</span>' }[m.status]}</td></tr>`).join("") || '<tr><td colspan="4" class="empty">No drafts yet.</td></tr>'}</tbody></table></div>`;
    box.append(pager(d, (pg) => { msgState.page = pg; loadDrafts().catch(fail); }));
    box.onclick = async (e) => { const tr = e.target.closest("[data-draft]"); if (!tr) return; const all = await api("/drafts?" + qs({ page_size: 100 })); const dd = all.items.find((x) => String(x.id) === tr.dataset.draft); if (dd) { msgState.draft = dd; editor(dd); $("#msgEditor").scrollIntoView({ block: "start" }); } };
  };
  bindFilters($("#draftPanel"), msgState, () => loadDrafts().catch(fail));
  if (msgState.draft && !fid) editor(msgState.draft);
  await loadDrafts();
}

// ------------------------------------------------------------------ router
const PAGES = {
  dashboard: ["Dashboard", pageDashboard], students: ["Students & Fees", pageStudentsFees],
  calls: ["Call Records", pageCalls], messages: ["Messages", pageMessages],
};
const OLD = { fees: "students", sync: "students" };
let current = "";
async function route(keepScroll = false) {
  let [name, query] = (location.hash.slice(1) || "dashboard").split("?");
  const params = Object.fromEntries(new URLSearchParams(query || ""));
  if (name === "sync") params.view = "sync";
  name = OLD[name] || name;
  const key = PAGES[name] ? name : "dashboard";
  const [title, fn] = PAGES[key];
  $("#pageTitle").textContent = title;
  document.title = `${title} · Fees Calling Desk`;
  document.querySelectorAll("#nav a").forEach((a) => (a.dataset.page === key ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  $("#side").classList.remove("open"); $("#menuBtn").setAttribute("aria-expanded", "false");
  const main = $("#main");
  const fresh = main.cloneNode(false); main.replaceWith(fresh);
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
    $("#modeNote").textContent = (st.test_mode ? "Test mode: synthetic students only. " : "") + "Messages are drafts; nothing is sent.";
  } catch {}
}

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-imp-close]")) { const done = !imp.token; closeImport().then(() => done && current === "students" && route(true)); return; }
  if (e.target.id === "scrim" && !$("#modal").hidden) return;
  const fam = e.target.closest("[data-family]");
  if (fam) { openFamily(fam.dataset.family); return; }
  if (e.target.closest("[data-close]") || e.target.id === "scrim") { closeDrawer(); return; }
  const o = e.target.closest("[data-open]");
  if (o && !e.target.closest("a,button,input")) openStudent(o.dataset.open);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { if (!$("#modal").hidden) closeImport(); else closeDrawer(); }
  if (e.key === "Enter" && e.target.matches("tr[data-open]")) openStudent(e.target.dataset.open);
});
$("#menuBtn").onclick = () => { const s = $("#side"); s.classList.toggle("open"); $("#menuBtn").setAttribute("aria-expanded", s.classList.contains("open")); };
$("#caller").value = store.get("fcd_caller", "");
$("#caller").addEventListener("input", (e) => store.set("fcd_caller", e.target.value));
window.addEventListener("hashchange", () => { closeDrawer(); route(); });

api("/filters").then((m) => { META = m; route(); }).catch((e) => { $("#main").innerHTML = `<div class="note bad">Cannot reach the SchoolOps service (${esc(e.message)}).</div>`; });
})();
