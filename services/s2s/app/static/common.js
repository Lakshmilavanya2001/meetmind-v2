// Small helpers shared by the account pages (dashboard, transcript, search).
"use strict";
const $ = (id) => document.getElementById(id);
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
function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  e.append(...children.filter((c) => c != null));
  return e;
}
// An answer with its checked citations (each links to the meeting's transcript).
function answerView(res) {
  const box = el("div", { class: "answer" }, el("p", { text: res.answer }));
  if (res.citations?.length) {
    box.append(el("ol", { class: "cites" }, ...res.citations.map((c) => el("li", {},
      el("a", { href: `/meetings/${c.meeting.joinCode}`, text: c.meeting.title }),
      el("span", { class: "meta", text: ` · ${c.time || ""} · ${c.speaker || ""}` }),
      el("blockquote", { text: c.line })))));
  }
  return box;
}
