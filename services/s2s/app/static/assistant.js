// The assistant chat: streamed replies (one JSON event per line), tool chips, Confirm / Cancel cards
// for actions that change something, and plan cards from the Planner (Approve / Reject).
"use strict";
const params = new URLSearchParams(location.search);
let conversationId = params.get("c");
const meetingCode = params.get("meeting");
let busy = false;

const fail = (e) => { $("err").textContent = e.message || String(e); $("err").hidden = false; };
const scroll = () => { $("log").scrollTop = $("log").scrollHeight; };

// Text with [label](/path) links: only links inside the app, or to Uber, become links.
function rich(text) {
  const out = document.createDocumentFragment();
  const re = /\[([^\]]+)\]\(([^)\s]+)\)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    out.append(text.slice(last, m.index));
    const safe = /^\/(?!\/)/.test(m[2]) || /^https:\/\/(m\.uber\.com|auth\.uber\.com)\//.test(m[2]);
    out.append(safe ? el("a", { href: m[2], text: m[1] }) : `${m[1]}`);
    last = re.lastIndex;
  }
  out.append(text.slice(last));
  return out;
}

const AGENT = { meetings: "Meetings", knowledge: "Knowledge", dining: "Dining", travel: "Travel", movies: "Movies",
                reminders: "Reminders", planner: "Planner", general: "Assistant" };

function bubble(kind, text, extra = {}) {
  const li = el("li", { class: `msg ${kind}${extra.guard ? " guard" : ""}` });
  if (kind === "assistant" && extra.agent) li.append(el("div", { class: "msg-agent", text: AGENT[extra.agent] || extra.agent }));
  const p = el("div", { class: "msg-text" });
  p.append(rich(text));
  li.append(p);
  if (extra.link) li.append(el("a", { class: "btn ghost small", href: extra.link, text: "Open" }));
  return li;
}

function handoff(h) {
  return el("li", { class: "handoff", text: `→ ${h.name || AGENT[h.agent] || h.agent}${h.total > 1 ? ` (${h.index + 1}/${h.total})` : ""}: ${h.task}` });
}

function chip(t) {
  const state = t.status === "pending_confirmation" ? "waiting" : t.ok === false ? "failed" : t.ok ? "done" : "running";
  const li = el("li", { class: `tool ${state}`, "data-id": t.id || "" },
    el("span", { class: "tool-label", text: t.label || t.name }),
    t.summary ? el("span", { class: "tool-sum", text: ` — ${t.summary}` }) : null);
  if (t.link) li.append(" ", el("a", { href: t.link, text: "open" }));
  if (t.candidates?.length) {
    li.append(el("ul", { class: "cands" }, ...t.candidates.map((c) => el("li", { text: `${c.displayName} · ${c.email}` }))));
  }
  if (t.citations?.length) {
    li.append(el("ol", { class: "cites" }, ...t.citations.map((c) => el("li", {},
      el("a", { href: `/meetings/${c.meeting.joinCode}`, text: c.meeting.title }),
      el("span", { class: "meta", text: ` · ${c.time || ""} · ${c.speaker || ""}` }),
      el("blockquote", { text: c.line })))));
  }
  return li;
}

function confirmCard(actionId, summary, decided) {
  const li = el("li", { class: "confirm card", "data-action": actionId }, el("div", { class: "msg-agent", text: "Needs your confirmation" }), el("p", { text: summary }));
  const status = el("p", { class: "meta" });
  if (decided) { status.textContent = decided; li.append(status); return li; }
  const yes = el("button", { class: "btn primary small", type: "button", text: "Confirm" });
  const no = el("button", { class: "btn ghost small", type: "button", text: "Cancel" });
  const decide = async (decision) => {
    yes.disabled = no.disabled = true;
    try {
      const r = await api(`/api/assistant/actions/${actionId}`, { decision });
      status.textContent = decision === "confirm" ? (r.ok ? "Confirmed" : "Failed") : "Cancelled";
      row.replaceWith(status);
      $("log").append(bubble("assistant", r.message.content, { link: r.message.link }));
      scroll();
    } catch (e) { yes.disabled = no.disabled = false; fail(e); }
  };
  yes.addEventListener("click", () => decide("confirm"));
  no.addEventListener("click", () => decide("reject"));
  const row = el("div", { class: "row" }, yes, no);
  li.append(row);
  return li;
}

const STEP = { done: "✓", failed: "✗", skipped: "–", pending: "•" };
function planCard(plan) {
  const li = el("li", { class: "plan card", "data-plan": plan.id },
    el("div", { class: "msg-agent", text: plan.revision ? `Revised plan (${plan.revision})` : "Plan" }),
    el("p", {}, el("strong", { text: plan.goal })),
    el("ol", { class: "plan-steps" }, ...plan.steps.map((s) => el("li", { class: `step ${s.status}` },
      el("span", { text: `${STEP[s.status] || "•"} ${s.resultSummary || s.summary}` }),
      s.why ? el("div", { class: "meta", text: s.why }) : null,
      s.error ? el("div", { class: "error", text: s.error }) : null,
      s.link ? el("a", { href: s.link, text: "open" }) : null))));
  if (plan.status !== "proposed") {
    li.append(el("p", { class: "meta", text: { completed: "Done", partial: "Partly done", failed: "Failed", rejected: "Rejected", expired: "Expired — ask again", running: "Running…" }[plan.status] || plan.status }));
    return li;
  }
  const yes = el("button", { class: "btn primary small", type: "button", text: "Approve plan" });
  const no = el("button", { class: "btn ghost small", type: "button", text: "Reject" });
  const decide = async (decision) => {
    yes.disabled = no.disabled = true;
    yes.textContent = decision === "approve" ? "Running…" : yes.textContent;
    try {
      const r = await api(`/api/assistant/plans/${plan.id}`, { decision });
      li.replaceWith(planCard(r.plan));
      $("log").append(bubble("assistant", r.message.content, { agent: "planner" }));
      if (r.revision) $("log").append(planCard(r.revision));
      scroll();
    } catch (e) { yes.disabled = no.disabled = false; yes.textContent = "Approve plan"; fail(e); }
  };
  yes.addEventListener("click", () => decide("approve"));
  no.addEventListener("click", () => decide("reject"));
  li.append(el("div", { class: "row" }, yes, no));
  return li;
}

function render(items) {
  const log = $("log");
  log.replaceChildren();
  for (const it of items) {
    if (it.kind === "user") log.append(bubble("user", it.text));
    else if (it.kind === "assistant") log.append(bubble("assistant", it.text, it));
    else if (it.kind === "handoff") log.append(handoff(it));
    else if (it.kind === "plan") log.append(planCard(it.plan));
    else if (it.kind === "tool") {
      log.append(chip(it));
      if (it.actionId) {
        const decided = it.status === "pending_confirmation" ? null
          : it.status === "expired" ? "Expired — ask again" : { confirmed: "Confirmed", failed: "Failed", rejected: "Cancelled" }[it.decision] || "Handled";
        log.append(confirmCard(it.actionId, it.summary, decided));
      }
    }
  }
  if (!items.length) log.append(el("li", { class: "hint", text: "Ask me to schedule meetings, find what was said in a call, order food, book a cab, a table or movie tickets, or set reminders. Anything that changes something waits for your Confirm." }));
  scroll();
}

async function loadList() {
  const { conversations } = await api("/api/assistant/conversations");
  $("convs").replaceChildren(...conversations.map((c) => {
    const a = el("a", { href: `/assistant?c=${c.id}`, class: c.id === conversationId ? "on" : "", text: c.title });
    a.addEventListener("click", (ev) => { ev.preventDefault(); open(c.id); });
    return el("li", {}, a, c.meeting ? el("div", { class: "meta", text: `in ${c.meeting.title}` }) : null);
  }));
}

async function open(id) {
  conversationId = id;
  history.replaceState(null, "", id ? `/assistant?c=${id}` : `/assistant${meetingCode ? `?meeting=${meetingCode}` : ""}`);
  $("err").hidden = true;
  $("attached").hidden = true;
  if (!id) { render([]); loadList().catch(fail); return; }
  const r = await api(`/api/assistant/conversations/${id}`);
  if (r.conversation.meeting) { $("attached").textContent = `Attached to “${r.conversation.meeting.title}”`; $("attached").hidden = false; }
  render(r.items);
  loadList().catch(fail);
}

async function send(text, voice = false) {
  const spoken = [];                 // what to read aloud when the reply was asked for by voice
  let asks = false;
  busy = true;
  $("send").disabled = true;
  $("err").hidden = true;
  const log = $("log");
  log.querySelector(".hint")?.remove();
  log.append(bubble("user", text));
  const thinking = el("li", { class: "handoff", text: "Thinking…" });
  log.append(thinking);
  scroll();
  const chips = new Map();
  try {
    const res = await fetch("/api/assistant/chat", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ message: text, conversationId, meetingCode: conversationId ? null : meetingCode,
                             voice, language: voice ? $("voice-lang").value : null }) });
    if (res.status === 401) { location.assign("/login?next=/assistant"); return; }
    if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || res.statusText); }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        const ev = JSON.parse(line);
        thinking.remove();
        if (ev.type === "conversation") { conversationId = ev.id; history.replaceState(null, "", `/assistant?c=${ev.id}`); }
        else if (ev.type === "handoff") { if (ev.total > 1 || ev.agent !== "general") log.append(handoff(ev)); }
        else if (ev.type === "tool_start") { const c = chip({ ...ev }); chips.set(ev.id, c); log.append(c); }
        else if (ev.type === "tool_end") { const c = chip({ ...ev, label: chips.get(ev.id)?.querySelector(".tool-label")?.textContent }); chips.get(ev.id)?.replaceWith(c); chips.set(ev.id, c); }
        else if (ev.type === "confirmation") { log.append(confirmCard(ev.actionId, ev.summary)); asks = true; }
        else if (ev.type === "decided") {            // a spoken "yes" / "no" decided the waiting action
          const card = log.querySelector(`.confirm[data-action="${ev.actionId}"] .row`);
          card?.replaceWith(el("p", { class: "meta", text: ev.decision === "confirm" ? (ev.ok ? "Confirmed" : "Failed") : "Cancelled" }));
          log.append(bubble("assistant", ev.text, { link: ev.link }));
          spoken.push(ev.text);
        }
        else if (ev.type === "plan") log.append(planCard(ev.plan));
        else if (ev.type === "text") { log.append(bubble("assistant", ev.text, { agent: ev.agent })); spoken.push(ev.text); }
        else if (ev.type === "guard") { log.append(bubble("assistant", ev.message, { guard: true })); spoken.push(ev.message); }
        else if (ev.type === "error") fail(new Error(ev.message));
        scroll();
      }
    }
  } catch (e) { fail(e); }
  finally { thinking.remove(); busy = false; $("send").disabled = false; loadList().catch(() => {}); }
  if (voice) await speakReply(spoken.slice(-2).join(" "), asks);
}

$("chat-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const text = $("msg").value.trim();
  if (!text || busy) return;
  $("msg").value = "";
  send(text);
});
$("msg").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("chat-form").requestSubmit(); }
});
$("new-chat").addEventListener("click", () => open(null));
// ------------------------------------------------------------------ voice: talk to the assistant
// Tap the mic and speak; listening stops after a pause. Parakeet turns it into text, the agents
// answer in short spoken sentences, and Magpie reads the reply. "Yes" / "no" confirm an action.
// Hands-free: after each reply, it listens again.
const SAY_YES = { en: "Say yes to go ahead, or no to cancel.", hi: "आगे बढ़ने के लिए हाँ कहें, या रद्द करने के लिए ना।" };
const voiceState = { listening: false, ctx: null, stream: null, node: null, chunks: [], audio: null };
const status = (t) => { $("voice-status").textContent = t; };

async function startListening() {
  if (voiceState.listening || busy) return;
  stopSpeaking();
  try {
    voiceState.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (e) { fail(new Error(`Microphone not available: ${e.message}`)); return; }
  const ctx = voiceState.ctx ??= new AudioContext();
  await ctx.resume();
  await ctx.audioWorklet.addModule("/recorder-worklet.js").catch(() => {});
  const src = ctx.createMediaStreamSource(voiceState.stream);
  const node = new AudioWorkletNode(ctx, "recorder");
  voiceState.node = node;
  voiceState.chunks = [];
  voiceState.listening = true;
  $("mic").setAttribute("aria-pressed", "true");
  status("Listening…");
  const started = performance.now();
  let heard = 0, quietSince = null;
  node.port.onmessage = ({ data }) => {
    voiceState.chunks.push(data);
    let sum = 0;
    for (const v of data) sum += v * v;
    const rms = Math.sqrt(sum / data.length);
    $("mic").style.setProperty("--level", Math.min(1, rms * 12).toFixed(2));
    const now = performance.now();
    if (rms > 0.015) { heard += data.length / ctx.sampleRate; quietSince = null; } else quietSince ??= now;
    const total = (now - started) / 1000;
    if ((heard > 0.25 && quietSince && now - quietSince > 1100) || total > 30) stopListening(true);
    else if (heard < 0.25 && total > 8) stopListening(false);          // nothing said
  };
  src.connect(node);
}

function stopListening(send_ = true) {
  if (!voiceState.listening) return;
  voiceState.listening = false;
  voiceState.node?.port.close();
  voiceState.node?.disconnect();
  voiceState.stream?.getTracks().forEach((t) => t.stop());
  $("mic").setAttribute("aria-pressed", "false");
  $("mic").style.setProperty("--level", 0);
  if (!send_) { status($("hands-free").checked ? "Hands-free paused (nothing heard). Tap the mic to talk." : "Tap the mic and speak"); return; }
  transcribeAndSend(voiceState.chunks, voiceState.ctx.sampleRate).catch(fail);
}

// Float32 at the device rate -> 16 kHz mono 16-bit WAV (what Parakeet takes).
function wav16k(chunks, rate) {
  const n = chunks.reduce((a, c) => a + c.length, 0);
  const all = new Float32Array(n);
  let o = 0;
  for (const c of chunks) { all.set(c, o); o += c.length; }
  const ratio = rate / 16000, out = Math.floor(n / ratio);
  const pcm = new Int16Array(out);
  for (let i = 0; i < out; i++) {
    const a = Math.floor(i * ratio), b = Math.min(n - 1, Math.floor((i + 1) * ratio));
    let s = 0;
    for (let j = a; j <= b; j++) s += all[j];
    pcm[i] = Math.max(-1, Math.min(1, s / (b - a + 1))) * 32767;
  }
  const buf = new ArrayBuffer(44 + pcm.length * 2), v = new DataView(buf);
  const str = (off, t) => { for (let i = 0; i < t.length; i++) v.setUint8(off + i, t.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + pcm.length * 2, true); str(8, "WAVE"); str(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, 16000, true);
  v.setUint32(28, 32000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, "data"); v.setUint32(40, pcm.length * 2, true);
  new Int16Array(buf, 44).set(pcm);
  return new Blob([buf], { type: "audio/wav" });
}

async function transcribeAndSend(chunks, rate) {
  status("Working out what you said…");
  const r = await fetch(`/api/assistant/transcribe?lang=${$("voice-lang").value}`, { method: "POST", headers: { "content-type": "audio/wav" }, body: wav16k(chunks, rate) });
  if (r.status === 401) { location.assign("/login?next=/assistant"); return; }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Speech recognition failed");
  const { text } = await r.json();
  if (!text) {
    status("I didn't catch that. Tap the mic and try again.");
    return;
  }
  status("Thinking…");
  await send(text, true);
}

async function speakReply(text, asks) {
  const lang = $("voice-lang").value;
  if (asks) text = `${text} ${SAY_YES[lang]}`;
  if (!text.trim()) { afterReply(); return; }
  try {
    const r = await fetch("/api/assistant/speak", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text, lang }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "No spoken reply");
    const url = URL.createObjectURL(await r.blob());
    const a = new Audio(url);
    voiceState.audio = a;
    $("mic").classList.add("speaking");
    status("Speaking… (tap the mic to interrupt)");
    a.onended = () => { URL.revokeObjectURL(url); voiceState.audio = null; $("mic").classList.remove("speaking"); afterReply(); };
    await a.play();
  } catch (e) {
    status(`Reply shown above (${e.message}).`);
    afterReply();
  }
}

function stopSpeaking() {
  if (!voiceState.audio) return;
  voiceState.audio.pause();
  voiceState.audio = null;
  $("mic").classList.remove("speaking");
}

function afterReply() {
  if ($("hands-free").checked) startListening();
  else status("Tap the mic and speak");
}

$("mic").addEventListener("click", () => (voiceState.listening ? stopListening(true) : startListening()));
api("/api/me").then((m) => { if (m.user.preferredLanguage === "hi") $("voice-lang").value = "hi"; }).catch(() => {});

open(conversationId).catch(fail);
