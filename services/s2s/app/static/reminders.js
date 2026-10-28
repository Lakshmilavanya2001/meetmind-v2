// Reminders, notifications, your mobile number (verified by a texted code) and your text messages.
"use strict";
let phone = null;   // { phoneNumber, verified, smsReminders, provider }

function fail(e) { $("err").textContent = e.message; $("err").hidden = false; }
function ok() { $("err").hidden = true; }
const when = (iso) => new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const item = (title, meta, ...right) => el("li", {},
  el("div", {}, el("div", { text: title, style: "font-weight:600" }), el("div", { class: "meta", text: meta })),
  el("div", { class: "row" }, ...right));

async function loadReminders() {
  const { reminders } = await api("/api/reminders");
  const ul = $("reminders");
  if (!reminders.length) { ul.replaceChildren(el("li", { class: "meta", text: "No upcoming reminders." })); return; }
  ul.replaceChildren(...reminders.map((r) => {
    const cancel = el("button", { class: "btn ghost small", type: "button", text: "Cancel" });
    cancel.addEventListener("click", async () => {
      try { await api(`/api/reminders/${r.id}`, undefined, "DELETE"); ok(); await loadReminders(); } catch (e) { fail(e); }
    });
    return item(r.text, [when(r.dueAt), r.sms ? "also by SMS" : ""].filter(Boolean).join(" · "),
      r.status === "PENDING" ? cancel : el("span", { class: "badge", text: "sending" }));
  }));
}

async function loadNotifications() {
  const { items, unread } = await api("/api/notifications?limit=30");
  $("unread").hidden = !unread;
  $("unread").textContent = `${unread} new`;
  const ul = $("notifications");
  if (!items.length) { ul.replaceChildren(el("li", { class: "meta", text: "Nothing yet." })); return; }
  ul.replaceChildren(...items.map((n) => {
    let right = el("span", { class: "meta", text: "read" });
    if (!n.readAt) {
      right = el("button", { class: "btn ghost small", type: "button", text: "Mark read" });
      right.addEventListener("click", async () => {
        try { await api("/api/notifications/read", { ids: [n.id] }); await loadNotifications(); } catch (e) { fail(e); }
      });
    }
    return item(`${n.title}${n.body ? `: ${n.body}` : ""}`, when(n.createdAt), right);
  }));
}

function renderPhone() {
  const p = phone;
  $("phone").value = p.phoneNumber || "";
  $("phone-state").textContent = !p.provider ? "Text messages aren't set up on this server."
    : !p.phoneNumber ? "Add your number to get reminders by text."
    : p.verified ? `${p.phoneNumber} is verified.` : `${p.phoneNumber} isn't verified yet.`;
  $("verify-box").hidden = !p.provider || !p.phoneNumber || p.verified;
  $("sms-default").checked = p.smsReminders;
  $("sms-default").disabled = !p.verified;
  $("r-sms").disabled = !p.verified;
  $("r-sms").checked = p.verified && p.smsReminders;
}

async function loadSms() {
  const res = await api("/api/profile/sms");
  phone = res.phone;
  renderPhone();
  $("sms-note").textContent = res.provider === "outbox"
    ? "Development outbox: nothing is actually sent; texts (codes included) appear here." : "";
  const ul = $("sms-list");
  if (!res.messages.length) { ul.replaceChildren(el("li", { class: "meta", text: "No text messages." })); return; }
  ul.replaceChildren(...res.messages.map((m) => item(m.body,
    [when(m.createdAt), m.to, m.purpose, m.error].filter(Boolean).join(" · "),
    el("span", { class: "badge", text: m.status.toLowerCase() }))));
}

$("add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("/api/reminders", { text: $("r-text").value.trim(), dueAt: new Date($("r-when").value).toISOString(), sms: $("r-sms").checked });
    ok();
    $("r-text").value = "";
    await loadReminders();
  } catch (e) { fail(e); }
});
$("read-all").addEventListener("click", async () => {
  try { await api("/api/notifications/read", {}); await loadNotifications(); } catch (e) { fail(e); }
});
$("phone-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try { phone = await api("/api/profile/phone", { phoneNumber: $("phone").value.trim() || null }); ok(); renderPhone(); } catch (e) { fail(e); }
});
$("send-code").addEventListener("click", async () => {
  try {
    const r = await api("/api/profile/phone/verify/start", {});
    ok();
    if (!r.alreadyVerified) $("code").focus();
    await loadSms();
  } catch (e) { fail(e); }
});
$("code-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try { await api("/api/profile/phone/verify", { code: $("code").value.trim() }); ok(); $("code").value = ""; await loadSms(); } catch (e) { fail(e); }
});
$("sms-default").addEventListener("change", async () => {
  try { await api("/api/profile/sms-reminders", { enabled: $("sms-default").checked }, "PUT"); ok(); await loadSms(); }
  catch (e) { fail(e); $("sms-default").checked = !$("sms-default").checked; }
});

Promise.all([loadReminders(), loadNotifications(), loadSms()]).catch(fail);
