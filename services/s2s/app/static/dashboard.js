// The dashboard: start, schedule and join meetings, and the list of yours.
"use strict";
const $ = (id) => document.getElementById(id);
let me = null;
let members = [];

async function api(url, body, method) {
  const r = await fetch(url, body === undefined && !method ? {} : {
    method: method || "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body ?? {}) });
  if (r.status === 401) { location.assign(`/login?next=${encodeURIComponent(location.pathname)}`); throw new Error("Please sign in."); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = data.detail;
    throw new Error(Array.isArray(d) ? d.map((e) => e.msg.replace(/^Value error, /, "")).join(" ") : d || r.statusText);
  }
  return data;
}
function fail(e) { $("dash-error").textContent = e.message; $("dash-error").hidden = false; }
const when = (iso) => iso ? new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

function renderMeetings(list) {
  const ul = $("meetings");
  if (!list.length) { ul.innerHTML = '<li class="meta">No meetings yet. Start one above.</li>'; return; }
  const order = { LIVE: 0, SCHEDULED: 1, ENDED: 2, CANCELLED: 3 };
  list.sort((a, b) => order[a.status] - order[b.status] || (a.status === "SCHEDULED"
    ? (a.scheduledStart || "").localeCompare(b.scheduledStart || "") : (b.startedAt || b.endedAt || "").localeCompare(a.startedAt || a.endedAt || "")));
  ul.replaceChildren(...list.map((m) => {
    const li = document.createElement("li");
    const left = document.createElement("div");
    const title = document.createElement("div");
    title.textContent = m.title;
    title.style.fontWeight = "600";
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = [m.status === "SCHEDULED" ? when(m.scheduledStart) : m.status === "LIVE" ? `started ${when(m.startedAt)}` : when(m.endedAt),
      `host ${m.host.displayName || ""}`, m.joinCode].filter(Boolean).join(" · ");
    left.append(title, meta);
    const right = document.createElement("div");
    right.className = "row";
    const badge = document.createElement("span");
    badge.className = "badge" + (m.status === "LIVE" ? " live" : "");
    badge.textContent = m.status.toLowerCase();
    right.append(badge);
    if (m.status !== "SCHEDULED" && m.status !== "CANCELLED") {
      const t = document.createElement("a");
      t.className = "btn ghost small";
      t.href = `/meetings/${m.joinCode}`;
      t.textContent = "Transcript";
      right.append(t);
    }
    if (m.status === "LIVE" || m.status === "SCHEDULED") {
      const a = document.createElement("a");
      a.className = "btn primary small";
      a.href = `/m/${m.joinCode}`;
      a.textContent = "Join";
      right.append(a);
      if (m.myRole === "HOST" || m.myRole === "COHOST") {
        const end = document.createElement("button");
        end.className = "btn ghost small";
        end.type = "button";
        end.textContent = m.status === "LIVE" ? "End" : "Cancel";
        end.addEventListener("click", async () => {
          if (!confirm(`${end.textContent} "${m.title}" for everyone?`)) return;
          try { await api(`/api/meetings/${m.joinCode}/end`); await load(); } catch (e) { fail(e); }
        });
        right.append(end);
      }
    }
    li.append(left, right);
    return li;
  }));
}

function renderPeople() {
  const q = $("people-filter").value.trim().toLowerCase();
  const box = $("people");
  const list = members.filter((u) => u.id !== me.user.id && (!q || u.displayName.toLowerCase().includes(q) || u.email.toLowerCase().includes(q)));
  if (!list.length) { box.innerHTML = '<p class="hint">No one else in your organisation yet.</p>'; return; }
  const checked = new Set([...box.querySelectorAll("input:checked")].map((i) => i.value));
  box.replaceChildren(...list.map((u) => {
    const l = document.createElement("label");
    const c = document.createElement("input");
    c.type = "checkbox";
    c.value = u.id;
    c.checked = checked.has(u.id);
    l.append(c, document.createTextNode(`${u.displayName} <${u.email}>`));
    return l;
  }));
}

async function load() {
  const [{ meetings }, m] = await Promise.all([api("/api/meetings"), api("/api/org/members")]);
  members = m.members;
  renderMeetings(meetings);
  renderPeople();
}

$("start-now").addEventListener("click", async () => {
  try {
    const { meeting } = await api("/api/meetings", { title: $("instant-title").value.trim() || "Meeting" });
    location.assign(`/m/${meeting.joinCode}`);
  } catch (e) { fail(e); }
});

$("join-code-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const m = $("join-code").value.trim().toLowerCase().match(/([a-z]{3}-[a-z]{4}-[a-z]{3})/);
  if (!m) return fail(new Error("Enter a code like abc-defg-hij, or paste the meeting link."));
  location.assign(`/m/${m[1]}`);
});

$("schedule-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const start = new Date($("s-start").value);
  const end = new Date(start.getTime() + Number($("s-duration").value) * 60000);
  const ids = [...$("people").querySelectorAll("input:checked")].map((i) => i.value);
  try {
    await api("/api/meetings", { title: $("s-title").value.trim(), scheduledStart: start.toISOString(), scheduledEnd: end.toISOString(), participantIds: ids });
    ev.target.reset();
    await load();
  } catch (e) { fail(e); }
});
$("people-filter").addEventListener("input", renderPeople);

$("logout").addEventListener("click", async () => {
  try { await api("/api/auth/logout"); } finally { location.assign("/login"); }
});

if (new URLSearchParams(location.search).get("ended")) {
  $("dash-note-top").textContent = "The host ended the meeting.";
  $("dash-note-top").hidden = false;
  history.replaceState(null, "", "/dashboard");
}

(async () => {
  try {
    me = await api("/api/me");
    $("me-name").textContent = me.user.displayName;
    $("org-name").textContent = `· ${me.organization.name}`;
    if (me.organization.joinCode) {
      $("admin-card").hidden = false;
      $("admin-org").textContent = me.organization.name;
      $("admin-code").textContent = me.organization.joinCode;
    }
    await load();
  } catch (e) { fail(e); }
})();
