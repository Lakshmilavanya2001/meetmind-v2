// The assistant chat as a component, used by the Assistant page and the assistant panel in calls.
//
//   const chat = new AssistantChat(rootElement, { meetingCode, conversationId, onConversation, voice, dark });
//
// - Streamed replies (one JSON event per line), tool chips, Confirm / Cancel cards for actions that
//   change something, and plan cards from the Planner (Approve / Reject).
// - Voice (voice: true): the mic stays on until it's turned off. Each sentence you say (ended by a
//   pause) goes to Parakeet, then to the agents in voice mode; each reply is spoken by Magpie as soon
//   as it arrives, streamed. While the assistant thinks or speaks the mic is held (not sent), so it
//   doesn't hear itself; then it listens again. "Yes" / "no" confirm or cancel a waiting action.
// Self-contained (no page globals), so it can sit on any page.
"use strict";
(() => {
  const el = (tag, attrs = {}, ...children) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
    }
    e.append(...children.filter((c) => c != null));
    return e;
  };
  async function api(url, body, method) {
    const r = await fetch(url, body === undefined && !method ? {} : {
      method: method || "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body ?? {}) });
    if (r.status === 401) { location.assign(`/login?next=${encodeURIComponent(location.pathname)}`); throw new Error("Please sign in."); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(Array.isArray(data.detail) ? data.detail.map((e) => e.msg).join(" ") : data.detail || r.statusText);
    return data;
  }
  // **bold** inside plain text (the model's light markdown); everything else stays text.
  function bold(text) {
    const out = document.createDocumentFragment();
    const parts = text.split(/\*\*([^*\n]+)\*\*/g);
    parts.forEach((p, i) => out.append(i % 2 ? el("strong", { text: p }) : p));
    return out;
  }
  // Text with [label](/path) links: only links inside the app, or to Uber, become links.
  function rich(text) {
    const out = document.createDocumentFragment();
    const re = /\[([^\]]+)\]\(([^)\s]+)\)/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      out.append(bold(text.slice(last, m.index)));
      const safe = /^\/(?!\/)/.test(m[2]) || /^https:\/\/(m\.uber\.com|auth\.uber\.com)\//.test(m[2]);
      out.append(safe ? el("a", { href: m[2], text: m[1].replace(/\*\*/g, "") }) : m[1].replace(/\*\*/g, ""));
      last = re.lastIndex;
    }
    out.append(bold(text.slice(last)));
    return out;
  }
  const AGENT = { meetings: "Meetings", knowledge: "Knowledge", dining: "Dining", travel: "Travel", movies: "Movies",
                  reminders: "Reminders", planner: "Planner", general: "Assistant" };
  const STEP = { done: "✓", failed: "✗", skipped: "–", pending: "•" };
  const SAY_YES = { en: "Say yes to go ahead, or no to cancel.", hi: "आगे बढ़ने के लिए हाँ कहें, या रद्द करने के लिए ना।" };
  const MIC_SVG = '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5 11a7 7 0 0 0 14 0M12 18v3"/></svg>';
  // Speech detection on the page: a sentence starts above this level and ends after this much quiet.
  const SPEECH_RMS = 0.012, END_SILENCE_MS = 900, MAX_SENTENCE_S = 30, PREROLL_S = 0.3;

  function micProblem(e) {
    const n = e?.name || "";
    if (!window.isSecureContext) return "The microphone needs the HTTPS address of this page.";
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) return "This browser can't record here. Try a current Chrome, Edge, Firefox or Safari.";
    if (n === "NotAllowedError" || n === "SecurityError") return "Microphone access is blocked. Allow it for this site (the icon in the address bar), then turn the mic on again.";
    if (n === "NotFoundError" || n === "OverconstrainedError") return "No microphone found. Connect one and turn the mic on again.";
    if (n === "NotReadableError" || n === "AbortError") return "The microphone is busy in another app or tab. Close it there and try again.";
    return `The microphone didn't start: ${e?.message || e}`;
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

  class AssistantChat {
    constructor(root, opts = {}) {
      this.opts = { meetingCode: null, conversationId: null, onConversation: () => {}, onList: () => {}, voice: true,
                    onMicChange: () => {}, lang: null,
                    placeholder: "Schedule a review with Priya tomorrow at 4 pm · What did we decide about the budget? · Book a table for 3 on Friday", ...opts };
      this.conversationId = this.opts.conversationId;
      this.busy = false;
      this.v = { on: false, phase: "off", ctx: null, stream: null, node: null, worklet: false, chunks: [], ring: [],
                 speech: 0, quietSince: null, startedAt: 0, playEnd: 0, sources: new Set(), lang: "en", gen: 0 };
      root.classList.add("assistant-chat");
      if (this.opts.dark) root.classList.add("dark");
      this.attached = el("p", { class: "save-banner", hidden: "" });
      this.log = el("ol", { class: "chat-log", "aria-live": "polite" });
      this.err = el("p", { class: "error", role: "alert", hidden: "" });
      this.msg = el("textarea", { rows: "2", maxlength: "4000", placeholder: this.opts.placeholder, "aria-label": "Message" });
      this.sendBtn = el("button", { class: "btn primary", type: "submit", text: "Send" });
      this.form = el("form", { class: "chat-form" }, this.msg, this.sendBtn);
      root.append(this.attached, this.log, this.err);
      if (this.opts.voice) root.append(this.buildVoiceBar());
      root.append(this.form);
      this.form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        const text = this.msg.value.trim();
        if (!text || this.busy) return;
        this.msg.value = "";
        this.send(text);
      });
      this.msg.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); this.form.requestSubmit(); }
      });
    }

    fail(e) { this.err.textContent = e.message || String(e); this.err.hidden = false; }
    scroll() { this.log.scrollTop = this.log.scrollHeight; }

    bubble(kind, text, extra = {}) {
      const li = el("li", { class: `msg ${kind}${extra.guard ? " guard" : ""}` });
      if (kind === "assistant" && extra.agent) li.append(el("div", { class: "msg-agent", text: AGENT[extra.agent] || extra.agent }));
      const p = el("div", { class: "msg-text" });
      p.append(rich(text));
      li.append(p);
      if (extra.link) li.append(el("a", { class: "btn ghost small", href: extra.link, text: "Open" }));
      return li;
    }
    handoff(h) {
      return el("li", { class: "handoff", text: `→ ${h.name || AGENT[h.agent] || h.agent}${h.total > 1 ? ` (${h.index + 1}/${h.total})` : ""}: ${h.task}` });
    }
    chip(t) {
      const state = t.status === "pending_confirmation" ? "waiting" : t.ok === false ? "failed" : t.ok ? "done" : "running";
      const li = el("li", { class: `tool ${state}`, "data-id": t.id || "" },
        el("span", { class: "tool-label", text: t.label || t.name }),
        t.summary ? el("span", { class: "tool-sum", text: ` — ${t.summary}` }) : null);
      if (t.link) li.append(" ", el("a", { href: t.link, text: "open" }));
      if (t.candidates?.length) li.append(el("ul", { class: "cands" }, ...t.candidates.map((c) => el("li", { text: `${c.displayName} · ${c.email}` }))));
      if (t.citations?.length) {
        li.append(el("ol", { class: "cites" }, ...t.citations.map((c) => el("li", {},
          el("a", { href: `/meetings/${c.meeting.joinCode}`, text: c.meeting.title }),
          el("span", { class: "meta", text: ` · ${c.time || ""} · ${c.speaker || ""}` }),
          el("blockquote", { text: c.line })))));
      }
      return li;
    }
    confirmCard(actionId, summary, decided) {
      const li = el("li", { class: "confirm card", "data-action": actionId }, el("div", { class: "msg-agent", text: "Needs your confirmation" }), el("p", { text: summary }));
      const status = el("p", { class: "meta" });
      if (decided) { status.textContent = decided; li.append(status); return li; }
      const yes = el("button", { class: "btn primary small", type: "button", text: "Confirm" });
      const no = el("button", { class: "btn ghost small", type: "button", text: "Cancel" });
      const row = el("div", { class: "row" }, yes, no);
      const decide = async (decision) => {
        yes.disabled = no.disabled = true;
        try {
          const r = await api(`/api/assistant/actions/${actionId}`, { decision });
          status.textContent = decision === "confirm" ? (r.ok ? "Confirmed" : "Failed") : "Cancelled";
          row.replaceWith(status);
          this.log.append(this.bubble("assistant", r.message.content, { link: r.message.link }));
          this.scroll();
        } catch (e) { yes.disabled = no.disabled = false; this.fail(e); }
      };
      yes.addEventListener("click", () => decide("confirm"));
      no.addEventListener("click", () => decide("reject"));
      li.append(row);
      return li;
    }
    planCard(plan) {
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
        if (decision === "approve") yes.textContent = "Running…";
        try {
          const r = await api(`/api/assistant/plans/${plan.id}`, { decision });
          li.replaceWith(this.planCard(r.plan));
          this.log.append(this.bubble("assistant", r.message.content, { agent: "planner" }));
          if (r.revision) this.log.append(this.planCard(r.revision));
          this.scroll();
        } catch (e) { yes.disabled = no.disabled = false; yes.textContent = "Approve plan"; this.fail(e); }
      };
      yes.addEventListener("click", () => decide("approve"));
      no.addEventListener("click", () => decide("reject"));
      li.append(el("div", { class: "row" }, yes, no));
      return li;
    }

    render(items) {
      this.log.replaceChildren();
      for (const it of items) {
        if (it.kind === "user") this.log.append(this.bubble("user", it.text));
        else if (it.kind === "assistant") this.log.append(this.bubble("assistant", it.text, it));
        else if (it.kind === "handoff") this.log.append(this.handoff(it));
        else if (it.kind === "plan") this.log.append(this.planCard(it.plan));
        else if (it.kind === "tool") {
          this.log.append(this.chip(it));
          if (it.actionId) {
            const decided = it.status === "pending_confirmation" ? null
              : it.status === "expired" ? "Expired — ask again" : { confirmed: "Confirmed", failed: "Failed", rejected: "Cancelled" }[it.decision] || "Handled";
            this.log.append(this.confirmCard(it.actionId, it.summary, decided));
          }
        }
      }
      if (!items.length) {
        this.log.append(el("li", { class: "hint", text: this.opts.meetingCode
          ? "Ask the assistant anything: schedule a follow-up, summarise what was said, order food, book a cab or set a reminder. Only you see this. You can also say “Hey Mind …” in the call."
          : "Ask me to schedule meetings, find what was said in a call, order food, book a cab, a table or movie tickets, or set reminders. Anything that changes something waits for your Confirm." }));
      }
      this.scroll();
    }

    async open(id) {
      this.conversationId = id;
      this.err.hidden = true;
      this.attached.hidden = true;
      if (!id) { this.render([]); return; }
      const r = await api(`/api/assistant/conversations/${id}`);
      if (r.conversation.meeting && !this.opts.meetingCode) { this.attached.textContent = `Attached to “${r.conversation.meeting.title}”`; this.attached.hidden = false; }
      this.render(r.items);
    }

    // The latest conversation of this meeting (the call's panel), or a new one.
    async openLatestForMeeting() {
      const { conversations } = await api(`/api/assistant/conversations?meeting=${encodeURIComponent(this.opts.meetingCode)}`);
      await this.open(conversations[0]?.id || null);
    }

    async send(text, voice = false) {
      this.busy = true;
      this.sendBtn.disabled = true;
      this.err.hidden = true;
      const log = this.log;
      log.querySelector(".hint")?.remove();
      log.append(this.bubble("user", text));
      const thinking = el("li", { class: "handoff", text: "Thinking…" });
      log.append(thinking);
      this.scroll();
      const chips = new Map();
      let asks = false;
      try {
        const res = await fetch("/api/assistant/chat", { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ message: text, conversationId: this.conversationId, meetingCode: this.conversationId ? null : this.opts.meetingCode,
                                 voice, language: voice ? this.v.lang : null }) });
        if (res.status === 401) { location.assign(`/login?next=${encodeURIComponent(location.pathname)}`); return; }
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
            if (ev.type === "conversation") { this.conversationId = ev.id; this.opts.onConversation(ev.id); }
            else if (ev.type === "handoff") { if (ev.total > 1 || ev.agent !== "general") log.append(this.handoff(ev)); }
            else if (ev.type === "tool_start") { const c = this.chip({ ...ev }); chips.set(ev.id, c); log.append(c); }
            else if (ev.type === "tool_end") { const c = this.chip({ ...ev, label: chips.get(ev.id)?.querySelector(".tool-label")?.textContent }); chips.get(ev.id)?.replaceWith(c); chips.set(ev.id, c); }
            else if (ev.type === "confirmation") { log.append(this.confirmCard(ev.actionId, ev.summary)); asks = true; }
            else if (ev.type === "decided") {            // a spoken "yes" / "no" decided the waiting action
              log.querySelector(`.confirm[data-action="${ev.actionId}"] .row`)
                ?.replaceWith(el("p", { class: "meta", text: ev.decision === "confirm" ? (ev.ok ? "Confirmed" : "Failed") : "Cancelled" }));
              log.append(this.bubble("assistant", ev.text, { link: ev.link }));
              if (voice) this.say(ev.text);
            }
            else if (ev.type === "plan") log.append(this.planCard(ev.plan));
            else if (ev.type === "text") { log.append(this.bubble("assistant", ev.text, { agent: ev.agent })); if (voice) this.say(ev.text); }
            else if (ev.type === "guard") { log.append(this.bubble("assistant", ev.message, { guard: true })); if (voice) this.say(ev.message); }
            else if (ev.type === "error") { this.fail(new Error(ev.message)); if (voice) this.say(ev.message); }
            this.scroll();
          }
        }
      } catch (e) { this.fail(e); }
      finally { thinking.remove(); this.busy = false; this.sendBtn.disabled = false; this.opts.onList(); }
      if (voice && asks) this.say(SAY_YES[this.v.lang] || SAY_YES.en);
      if (voice) this.afterTurn();
    }

    // ---------------------------------------------------------------- voice
    buildVoiceBar() {
      this.micBtn = el("button", { class: "mic", type: "button", "aria-pressed": "false", "aria-label": "Microphone for the assistant: on or off" });
      this.micBtn.innerHTML = MIC_SVG;
      this.vStatus = el("span", { class: "voice-status", "aria-live": "polite", text: "Turn the mic on and talk. It stays on until you turn it off." });
      this.langSel = el("select", { "aria-label": "Language you speak" }, el("option", { value: "en", text: "English" }), el("option", { value: "hi", text: "हिंदी" }));
      this.langSel.addEventListener("change", () => { this.v.lang = this.langSel.value; });
      this.micBtn.addEventListener("click", () => (this.v.on ? this.micOff() : this.micOn()));
      const setLang = (l) => { if (l === "hi" || l === "en") { this.langSel.value = l; this.v.lang = l; } };
      if (this.opts.lang) setLang(this.opts.lang);
      else api("/api/me").then((m) => setLang(m.user.preferredLanguage)).catch(() => {});
      return el("div", { class: "voice-bar" }, this.micBtn, this.vStatus, el("label", { class: "voice-opt" }, this.langSel));
    }
    status(t) { if (this.vStatus) this.vStatus.textContent = t; }

    async micOn() {
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) { this.status(micProblem()); return; }
      this.status("Allow the microphone if your browser asks…");
      const v = this.v;
      try {
        v.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
        v.ctx ??= new AudioContext();
        await v.ctx.resume();
        if (!v.worklet) { await v.ctx.audioWorklet.addModule(`/recorder-worklet.js?v=${Date.now()}`); v.worklet = true; }
        const src = v.ctx.createMediaStreamSource(v.stream);
        v.node = new AudioWorkletNode(v.ctx, "recorder");
        v.node.port.onmessage = ({ data }) => this.onFrame(data);
        src.connect(v.node);
        v.on = true;
        this.micBtn.setAttribute("aria-pressed", "true");
        this.opts.onMicChange(true);
        this.listen();
      } catch (e) {
        console.error("assistant microphone", e);
        v.stream?.getTracks().forEach((t) => t.stop());
        v.on = false;
        this.micBtn.setAttribute("aria-pressed", "false");
        this.status(micProblem(e));
      }
    }

    micOff() {
      const v = this.v;
      const was = v.on;
      v.on = false;
      v.phase = "off";
      v.gen++;
      v.node?.port.close();
      v.node?.disconnect();
      v.stream?.getTracks().forEach((t) => t.stop());
      this.stopSpeaking();
      this.micBtn.setAttribute("aria-pressed", "false");
      this.micBtn.classList.remove("speaking", "held");
      this.micBtn.style.setProperty("--level", 0);
      this.status("Mic off. Turn it on to talk.");
      if (was) this.opts.onMicChange(false);
    }

    listen() {
      const v = this.v;
      if (!v.on) return;
      v.phase = "waiting";
      v.chunks = [];
      v.speech = 0;
      v.quietSince = null;
      this.micBtn.classList.remove("speaking", "held");
      this.status("Listening… speak whenever you're ready.");
    }

    onFrame(data) {
      const v = this.v;
      if (!v.on) return;
      let sum = 0;
      for (const x of data) sum += x * x;
      const rms = Math.sqrt(sum / data.length);
      const rate = v.ctx.sampleRate;
      if (v.phase !== "waiting" && v.phase !== "speech") { this.micBtn.style.setProperty("--level", 0); return; }   // held while thinking / speaking
      this.micBtn.style.setProperty("--level", Math.min(1, rms * 12).toFixed(2));
      const now = performance.now();
      if (v.phase === "waiting") {
        v.ring.push(data);                                   // a little audio from before the sentence starts
        while (v.ring.length * 128 > PREROLL_S * rate) v.ring.shift();
        if (rms > SPEECH_RMS) {
          v.speech += data.length / rate;
          if (v.speech > 0.12) { v.phase = "speech"; v.chunks = [...v.ring]; v.startedAt = now; v.quietSince = null; this.status("Hearing you…"); }
        } else v.speech = Math.max(0, v.speech - data.length / rate);
        return;
      }
      v.chunks.push(data);
      if (rms > SPEECH_RMS) v.quietSince = null; else v.quietSince ??= now;
      if ((v.quietSince && now - v.quietSince > END_SILENCE_MS) || now - v.startedAt > MAX_SENTENCE_S * 1000) {
        v.phase = "thinking";
        this.micBtn.classList.add("held");
        const chunks = v.chunks;
        v.chunks = [];
        v.ring = [];
        this.handleSentence(chunks, rate).catch((e) => { this.status(`Couldn't send what you said: ${e.message}`); this.listen(); });
      }
    }

    async handleSentence(chunks, rate) {
      const gen = this.v.gen;
      this.status("Working out what you said…");
      const r = await fetch(`/api/assistant/transcribe?lang=${this.v.lang}`, { method: "POST", headers: { "content-type": "audio/wav" }, body: wav16k(chunks, rate) });
      if (r.status === 401) { location.assign(`/login?next=${encodeURIComponent(location.pathname)}`); return; }
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Speech recognition failed");
      const { text } = await r.json();
      if (gen !== this.v.gen) return;                       // the mic was turned off meanwhile
      if (!text || this.busy) { this.listen(); return; }
      this.status("Thinking…");
      await this.send(text, true);
    }

    // Speak one piece of a reply now, streamed: it plays after anything already queued.
    say(text) {
      const v = this.v;
      if (!text?.trim() || !v.on) return;
      const gen = v.gen;
      const prev = this.speaking;                            // pieces play in the order they arrived
      const job = (async () => {
        const r = await fetch("/api/assistant/speak?stream=1", { method: "POST", headers: { "content-type": "application/json" },
                                                                  body: JSON.stringify({ text, lang: v.lang }) });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "No spoken reply");
        const rate = Number(r.headers.get("x-sample-rate")) || 22050;
        const reader = r.body.getReader();
        let rest = new Uint8Array(0);
        await prev;
        for (;;) {
          const { value, done } = await reader.read();
          if (done || gen !== v.gen) break;
          const bytes = new Uint8Array(rest.length + value.length);
          bytes.set(rest); bytes.set(value, rest.length);
          const n = bytes.length >> 1;
          rest = bytes.slice(n * 2);
          if (!n) continue;
          const pcm = new Int16Array(bytes.buffer, 0, n);
          const buf = v.ctx.createBuffer(1, n, rate);
          const ch = buf.getChannelData(0);
          for (let i = 0; i < n; i++) ch[i] = pcm[i] / 32768;
          const s = v.ctx.createBufferSource();
          s.buffer = buf;
          s.connect(v.ctx.destination);
          const at = Math.max(v.ctx.currentTime + 0.03, v.playEnd);
          s.start(at);
          v.playEnd = at + buf.duration;
          v.sources.add(s);
          s.onended = () => v.sources.delete(s);
          if (v.phase !== "off") { v.phase = "speaking"; this.micBtn.classList.add("speaking"); this.status("Speaking… (turn the mic off to stop)"); }
        }
      })().catch((e) => this.status(`Reply shown above (${e.message}).`));
      this.speaking = job;
      return job;
    }

    stopSpeaking() {
      for (const s of this.v.sources) { try { s.stop(); } catch { /* already ended */ } }
      this.v.sources.clear();
      this.v.playEnd = 0;
    }

    // After a spoken turn: once everything queued has been said, listen again (the mic stayed on).
    async afterTurn() {
      const v = this.v, gen = v.gen;
      await (this.speaking ?? Promise.resolve());
      const wait = Math.max(0, (v.playEnd - (v.ctx?.currentTime ?? 0)) * 1000) + 350;
      setTimeout(() => { if (gen === v.gen && v.on && !this.busy) this.listen(); }, wait);
    }
  }

  window.AssistantChat = AssistantChat;
})();
