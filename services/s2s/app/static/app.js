// Indic Call Translator - demo client.
// Joins a LiveKit room, publishes the mic, plays the translation track the agent
// publishes for this caller, and renders captions from the agent's "captions" data.
"use strict";

const { Room, RoomEvent, Track, ParticipantKind } = LivekitClient;
const $ = (id) => document.getElementById(id);
function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  e.append(...children.filter((c) => c != null));
  return e;
}

const state = {
  room: null,
  me: null,               // {identity, name, speak, hear, voice}
  langs: { speak: [], hear: [] },
  names: {},              // code -> English name
  utts: new Map(),        // utterance id -> bubble
  playQueue: [],          // partner utterances whose translated audio is still to play, in order
  playing: null,          // the one playing now
  latencies: [],
  originalVol: 0,         // the user's choice; a same-language partner is always at 100
  translationVol: 100,
  micOn: true,
  hearTouched: false,     // on the join form "hear" follows "speak" until changed
  agentPresent: false,
  sameLangNotice: "",
  testMic: null,          // pre-join microphone check
  studio: false,          // server noise removal (NVIDIA BNR) available
  meeting: null,          // meeting mode (/m/<code>): {code, title, myRole}; null on the open demo page
  noiseChain: Promise.resolve(),
};

const store = {
  get(k, d) { try { return localStorage.getItem("ict." + k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("ict." + k, v); } catch { /* private mode */ } },
};

const langName = (code) => state.names[code] || code || "?";

// ------------------------------------------------------------------ dialogs (invoker commands fallback)
// Native `command`/`commandfor` buttons open and close the settings dialog; older
// browsers get the same behaviour from this small delegate.
if (!("commandForElement" in HTMLButtonElement.prototype)) {
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("button[commandfor]");
    const target = btn && document.getElementById(btn.getAttribute("commandfor"));
    if (!(target instanceof HTMLDialogElement)) return;
    const cmd = btn.getAttribute("command");
    if (cmd === "show-modal" && !target.open) target.showModal();
    if (cmd === "close") target.close();
  });
}

// ------------------------------------------------------------------ toasts
const toastBox = $("toasts");
const hasPopover = "popover" in HTMLElement.prototype;
const toastKeys = new Map();

function toast(text, { timeout = 5000, key } = {}) {
  if (key && toastKeys.has(key)) {                      // same topic: update in place
    const el = toastKeys.get(key);
    el.querySelector("p").textContent = text;
    restartTimer(el, timeout);
    return;
  }
  const el = document.createElement("div");
  el.className = "toast";
  el.setAttribute("role", "status");
  const p = document.createElement("p");
  p.textContent = text;
  const close = document.createElement("button");
  close.type = "button";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "×";
  close.onclick = () => dismiss(el);
  el.append(p, close);
  if (key) { el.dataset.key = key; toastKeys.set(key, el); }
  toastBox.append(el);
  if (hasPopover) { if (!toastBox.matches(":popover-open")) toastBox.showPopover(); }
  else toastBox.classList.add("open");
  restartTimer(el, timeout);
}
function restartTimer(el, timeout) {
  clearTimeout(el._t);
  if (timeout) el._t = setTimeout(() => dismiss(el), timeout);
}
function dismiss(el) {
  if (el.dataset.key) toastKeys.delete(el.dataset.key);
  el.classList.add("leaving");
  setTimeout(() => {
    el.remove();
    if (!toastBox.children.length) {
      if (hasPopover) { if (toastBox.matches(":popover-open")) toastBox.hidePopover(); }
      else toastBox.classList.remove("open");
    }
  }, 200);
}

// ------------------------------------------------------------------ audio levels
// One AudioContext; an AnalyserNode per watched track; a rAF loop writes each
// level (0..1) where the UI wants it (avatar rings, mic button, meters).
const levels = { ctx: null, taps: new Map(), raf: 0 };

function levelCtx() {
  if (!levels.ctx) levels.ctx = new AudioContext();
  if (levels.ctx.state === "suspended") levels.ctx.resume();
  return levels.ctx;
}
function watchLevel(key, mediaStreamTrack, apply) {
  unwatchLevel(key);
  const ctx = levelCtx();
  const src = ctx.createMediaStreamSource(new MediaStream([mediaStreamTrack]));
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  src.connect(analyser);
  levels.taps.set(key, { src, analyser, data: new Float32Array(analyser.fftSize), apply, value: 0 });
  if (!levels.raf) levels.raf = requestAnimationFrame(levelLoop);
}
function unwatchLevel(key) {
  const tap = levels.taps.get(key);
  if (!tap) return;
  tap.src.disconnect();
  tap.apply(0);
  levels.taps.delete(key);
}
function levelLoop() {
  for (const tap of levels.taps.values()) {
    tap.analyser.getFloatTimeDomainData(tap.data);
    let sum = 0;
    for (const v of tap.data) sum += v * v;
    const level = Math.min(1, Math.sqrt(sum / tap.data.length) * 5);
    tap.value = level > tap.value ? level : tap.value * 0.85 + level * 0.15;   // fast attack, slow release
    tap.apply(tap.value);
  }
  levels.raf = levels.taps.size ? requestAnimationFrame(levelLoop) : 0;
}
const setLevel = (el, v) => el && el.style.setProperty("--level", v.toFixed(3));
const avatarOf = (identity) => document.querySelector(`.person[data-identity="${CSS.escape(identity)}"] .avatar`);
// A person's circle pulses while they talk, and their state reads "speaking" (meetmind's call screen).
function setSpeaking(identity, v) {
  const tile = document.querySelector(`.person[data-identity="${CSS.escape(identity)}"]`);
  if (!tile) return;
  const talking = v > 0.12;
  if (tile.classList.contains("is-speaking") === talking) return;
  tile.classList.toggle("is-speaking", talking);
  const st = tile.querySelector(".person-state");
  if (st && !tile.classList.contains("muted")) st.textContent = talking ? "speaking" : "listening";
}

// ------------------------------------------------------------------ languages
function fillSelect(sel, list, value) {
  sel.replaceChildren(...list.map((l) => {
    const o = document.createElement("option");
    o.value = l.code;
    o.textContent = l.native && l.native !== l.name ? `${l.name} - ${l.native}` : l.name;
    if (l.preview) o.textContent += " (preview voice)";
    return o;
  }));
  if (value && list.some((l) => l.code === value)) sel.value = value;
}

async function loadLanguages() {
  const r = await fetch("/api/languages");
  state.langs = await r.json();
  for (const l of state.langs.hear) state.names[l.code] = l.name;
  const speak = store.get("speak", "hi");
  state.hearTouched = store.get("hear", null) !== null;
  fillSelect($("speak"), state.langs.speak, speak);
  fillSelect($("hear"), state.langs.hear, store.get("hear", speak));
  fillSelect($("speak-live"), state.langs.speak);
  fillSelect($("hear-live"), state.langs.hear);
  await loadVoices();
  fillVoicePicker("", store.get("voice", "Louise"));
  // "Studio" (NVIDIA BNR in the agent) is offered only when the server has it.
  state.studio = !!state.langs.noise?.studio;
  for (const sel of [$("noise"), $("noise-live")]) sel.querySelector('option[value="studio"]').hidden = !state.studio;
  const noise = store.get("noise", "standard");
  $("noise").value = noise === "studio" && !state.studio ? "standard" : noise;
  $("name").value = store.get("name", "");
  const params = new URLSearchParams(location.search);
  $("room").value = params.get("room") || store.get("room", "") || newRoomName();
  $("show-english").checked = store.get("showEnglish", "0") === "1";
  $("show-original").checked = store.get("showOriginal", "1") === "1";
  applyCaptionPrefs();
}

function newRoomName() { return "call-" + Math.random().toString(36).slice(2, 7); }

// ------------------------------------------------------------------ pre-join mic check
async function startMicTest() {
  stopMicTest();
  const deviceId = $("mic-device").value && !$("mic-device").disabled ? $("mic-device").value : store.get("mic", "");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: deviceId ? { deviceId: { ideal: deviceId } } : true });
    state.testMic = stream;
    await fillDevices($("mic-device"), "audioinput", stream.getAudioTracks()[0].getSettings().deviceId);
    $("mic-device").disabled = false;
    $("mic-test").textContent = "Stop check";
    watchLevel("test", stream.getAudioTracks()[0], (v) => { $("mic-test-level").style.width = `${Math.round(v * 100)}%`; });
  } catch (e) {
    toast(`Microphone not available: ${e.message}`);
  }
}
function stopMicTest() {
  unwatchLevel("test");
  state.testMic?.getTracks().forEach((t) => t.stop());
  state.testMic = null;
  $("mic-test").textContent = "Check mic";
}

async function fillDevices(sel, kind, current) {
  const devices = await Room.getLocalDevices(kind, false);
  sel.replaceChildren(...devices.map((d, i) => {
    const o = document.createElement("option");
    o.value = d.deviceId;
    o.textContent = d.label || `${kind === "audioinput" ? "Microphone" : "Speaker"} ${i + 1}`;
    return o;
  }));
  if (current && devices.some((d) => d.deviceId === current)) sel.value = current;
  return devices;
}

// ------------------------------------------------------------------ join / leave
async function join(ev) {
  ev.preventDefault();
  const btn = $("join-btn");
  btn.disabled = true;
  btn.textContent = "Joining...";
  $("join-error").hidden = true;
  const req = { name: $("name").value.trim(), room: $("room").value.trim(),
                speak: $("speak").value, hear: $("hear").value, voice: pickedVoice(""), noise: $("noise").value };
  const micId = state.testMic ? $("mic-device").value : store.get("mic", "");
  for (const k of ["name", "room", "speak", "hear", "voice", "noise"]) store.set(k, req[k]);
  if (micId) store.set("mic", micId);
  stopMicTest();
  try {
    levelCtx();                                   // created inside the click: allowed to run
    const r = state.meeting
      ? await fetch(`/api/meetings/${state.meeting.code}/join`, { method: "POST", headers: { "content-type": "application/json" },
                                                                body: JSON.stringify({ speak: req.speak, hear: req.hear, voice: req.voice, noise: req.noise }) })
      : await fetch("/api/token", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(req) });
    if (r.status === 401) { location.assign(`/login?next=${encodeURIComponent(location.pathname)}`); return; }
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const { token, identity, room: roomName } = await r.json();
    state.me = { identity, name: req.name, speak: req.speak, hear: req.hear, voice: req.voice, noise: req.noise };

    const room = new Room({ adaptiveStream: true, dynacast: true,
      audioCaptureDefaults: { ...micConstraints(req.noise), ...(micId ? { deviceId: micId } : {}) } });
    state.room = room;
    wireRoom(room);
    await room.connect(`wss://${location.host}`, token);
    await room.localParticipant.setMicrophoneEnabled(true);
    if (req.noise === "enhanced") await applyNoise("enhanced");
    await room.startAudio();
    watchMyMic();

    if (!state.meeting) history.replaceState(null, "", `?room=${encodeURIComponent(roomName)}`);
    $("room-name").textContent = state.meeting ? state.meeting.title : roomName;
    const moderator = !!(state.meeting && ["HOST", "COHOST"].includes(state.meeting.myRole));
    $("end-all-key").hidden = $("save-key").hidden = !moderator;
    startCallChrome(roomName);
    if (state.meeting) watchMeeting();
    syncSettingsForm();
    $("join").hidden = true;
    $("call").hidden = false;
    renderPeople();
    updateTranslatorChip();
  } catch (e) {
    $("join-error").textContent = `Could not join: ${e.message || e}`;
    $("join-error").hidden = false;
    if (state.room) await state.room.disconnect();
    state.room = null;
  } finally {
    btn.disabled = false;
    btn.textContent = "Join call";
  }
}

function watchMyMic() {
  const track = state.room?.localParticipant.getTrackPublication(Track.Source.Microphone)?.track;
  if (!track) return;
  watchLevel("me", track.mediaStreamTrack, (v) => {
    const level = state.micOn ? v : 0;
    setLevel($("mic-ring"), level);
    setLevel(avatarOf(state.me.identity), level);
    setSpeaking(state.me.identity, level);
  });
}

async function leave(note) {
  if ($("settings").open) $("settings").close();
  const room = state.room;
  state.room = null;                      // our own leave isn't a drop
  if (room) await room.disconnect();
  for (const key of [...levels.taps.keys()]) unwatchLevel(key);
  state.utts.clear();
  state.playQueue = [];
  state.playing = null;
  state.latencies = [];
  state.sameLangNotice = "";
  $("feed").replaceChildren($("empty"));
  $("empty").hidden = false;
  pinned = true;
  $("latency").textContent = "";
  document.querySelectorAll("audio[data-lk]").forEach((a) => a.remove());
  $("call").hidden = true;
  clearInterval(state.elapsedTimer);
  $("side").hidden = true;
  $("side").dataset.panel = "";
  if (state.assistant?.v.on) state.assistant.micOff();
  $("assistant-banner").hidden = true;
  $("mind-banner").hidden = true;
  $("side-assistant").replaceChildren();
  state.assistant = null;
  $("join").hidden = false;
  if (state.meeting) location.assign(note === "ended" ? "/dashboard?ended=1" : "/dashboard");
}

// ------------------------------------------------------------------ room
const isAgent = (p) => p && (p.kind === ParticipantKind.AGENT || p.isAgent);
const humans = () => state.room ? [...state.room.remoteParticipants.values()].filter((p) => !isAgent(p)) : [];

function wireRoom(room) {
  room
    .on(RoomEvent.Connected, () => { $("conn-dot").classList.add("on"); $("conn-text").textContent = "Connected"; })
    .on(RoomEvent.Reconnecting, () => {
      $("conn-dot").classList.remove("on");
      $("conn-text").textContent = "Reconnecting…";
      toast("Connection lost, reconnecting...", { key: "conn", timeout: 0 });
    })
    .on(RoomEvent.Reconnected, () => { $("conn-dot").classList.add("on"); $("conn-text").textContent = "Connected"; toast("Reconnected.", { key: "conn" }); })
    .on(RoomEvent.Disconnected, async () => {
      $("conn-dot").classList.remove("on");
      $("conn-text").textContent = "Disconnected";
      // Dropped without leaving: if the host ended the meeting, say so and leave (not a dead call screen).
      if (!state.meeting || state.room !== room) return;
      const r = await fetch(`/api/meetings/${state.meeting.code}`).catch(() => null);
      const status = r?.ok ? (await r.json()).meeting.status : null;
      if (status === "ENDED" || status === "CANCELLED") await leave("ended");
    })
    .on(RoomEvent.TrackMuted, () => renderPeople())
    .on(RoomEvent.TrackUnmuted, () => renderPeople())
    .on(RoomEvent.ParticipantConnected, (p) => {
      if (isAgent(p)) return updateTranslatorChip();
      toast(`${p.name || p.identity} joined.`);
      renderPeople();
    })
    .on(RoomEvent.ParticipantDisconnected, (p) => {
      if (isAgent(p)) return updateTranslatorChip();
      toast(`${p.name || p.identity} left.`);
      unwatchLevel(p.identity);
      renderPeople();
    })
    .on(RoomEvent.ParticipantAttributesChanged, (changed, p) => {
      if (p === room.localParticipant || isAgent(p)) return;
      if (changed.speak || changed.hear) {
        toast(`${p.name || p.identity} now speaks ${langName(p.attributes.speak)} and hears ${langName(p.attributes.hear)}.`);
      }
      renderPeople();
    })
    .on(RoomEvent.TrackSubscribed, (track, pub, participant) => {
      if (track.kind !== Track.Kind.Audio) return;
      const el = track.attach();
      // A Studio speaker's voice with the noise removed (NVIDIA BNR), from the agent: played instead of
      // their raw microphone wherever their own voice is heard.
      const clean = isAgent(participant) && pub.trackName?.startsWith("clean-");
      el.dataset.lk = clean ? "original" : isAgent(participant) ? "translation" : "original";
      el.dataset.identity = clean ? pub.trackName.slice(6) : participant.identity;
      if (clean) el.dataset.clean = "1";
      el.dataset.sid = pub.trackSid;                     // to remove it when the track goes away
      document.body.appendChild(el);
      if (clean) { applyVolumes(); return; }
      if (isAgent(participant)) watchLevel("translation", track.mediaStreamTrack, onTranslationLevel);
      else watchLevel(participant.identity, track.mediaStreamTrack, (v) => { setLevel(avatarOf(participant.identity), v); setSpeaking(participant.identity, v); });
      applyVolumes();
    })
    .on(RoomEvent.TrackUnsubscribed, (track, pub, participant) => {
      track.detach().forEach((el) => el.remove());
      // The SDK may have detached it already (then detach() returns nothing): remove ours by track id.
      document.querySelectorAll(`audio[data-sid="${CSS.escape(pub.trackSid)}"]`).forEach((el) => el.remove());
      if (isAgent(participant) && pub.trackName?.startsWith("clean-")) { applyVolumes(); return; }   // raw voice back
      unwatchLevel(isAgent(participant) ? "translation" : participant.identity);
    })
    .on(RoomEvent.DataReceived, (payload, participant, kind, topic) => {
      if (topic !== "captions") return;
      try { onAgentMessage(JSON.parse(new TextDecoder().decode(payload))); } catch (e) { console.warn(e); }
    })
    .on(RoomEvent.AudioPlaybackStatusChanged, () => {
      if (!room.canPlaybackAudio) toast("Your browser blocked audio. Click anywhere to turn it on.", { key: "audio", timeout: 0 });
    });
}

document.addEventListener("click", () => {
  if (!state.room || state.room.canPlaybackAudio) return;
  state.room.startAudio().then(() => {
    const t = toastKeys.get("audio");
    if (t) dismiss(t);
  });
});

function updateTranslatorChip() {
  const was = state.agentPresent;
  state.agentPresent = state.room ? [...state.room.remoteParticipants.values()].some(isAgent) : false;
  const chip = $("translator");
  chip.textContent = state.agentPresent ? "translator ready" : "translator joining...";
  chip.classList.toggle("ok", state.agentPresent);
  if (was && !state.agentPresent) toast("Translator disconnected, bringing it back...", { key: "agent", timeout: 0 });
  if (!was && state.agentPresent && toastKeys.has("agent")) toast("Translator is back.", { key: "agent" });
}

// ------------------------------------------------------------------ people
function renderPeople() {
  if (!state.room) return;
  const me = state.me;
  const others = humans();
  const people = [{ identity: me.identity, name: me.name, speak: me.speak, hear: me.hear, mine: true, muted: !state.micOn },
                  ...others.map((p) => ({ identity: p.identity, name: p.name || p.identity, speak: p.attributes.speak, hear: p.attributes.hear,
                                          mine: false, muted: !p.isMicrophoneEnabled }))];
  const stage = $("people");
  stage.dataset.count = String(people.length);
  stage.replaceChildren(...people.map(personTile));
  if (!others.length) stage.append(el("p", { class: "waiting", text: "Waiting for others. Tap Invite to copy the link." }));
  const names = people.map((p) => (p.mine ? "You" : p.name));
  $("call-title").textContent = people.length === 2 ? `${names[0]}  ⇄  ${names[1]}` : people.length > 2 ? names.slice(0, 3).join(", ") + (people.length > 3 ? ` +${people.length - 3}` : "") : "";
  $("people-count").textContent = `${people.length} ${people.length === 1 ? "person" : "people"}`;
  $("people-list").replaceChildren(...people.map((p) => el("li", {},
    el("span", { class: "pl-avatar", text: initials(p.name) }),
    el("span", { class: "pl-name", text: p.mine ? `${p.name} (you)` : p.name }),
    el("span", { class: "pl-meta", text: `speaks ${langName(p.speak)} · hears ${langName(p.hear)}${p.muted ? " · muted" : ""}` }))));
  applyVolumes();
}

const initials = (name) => (name || "?").trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join("").toUpperCase();

function personTile(p) {
  const tile = el("div", { class: `person${p.mine ? " me" : ""}${p.muted ? " muted" : ""}`, "data-identity": p.identity });
  const circle = el("div", { class: "call-avatar" }, el("div", { class: "avatar", text: initials(p.name) }));
  if (p.muted) circle.append(el("span", { class: "muted-badge", "aria-label": "Muted", text: "" }));
  tile.append(circle,
    el("p", { class: "person-name", text: p.name }, p.mine ? el("span", { class: "you", text: " (you)" }) : null),
    el("div", { class: "person-meta" },
      el("span", { class: "lang-chip", title: `speaks ${langName(p.speak)}, hears ${langName(p.hear)}`, text: langName(p.speak) }),
      el("span", { class: "person-state", text: p.muted ? "muted" : "listening" })));
  return tile;
}

// The call screen's top bar, timer, panels and keys (meetmind's layout).
function startCallChrome(roomName) {
  const title = state.meeting ? state.meeting.title : roomName;
  $("d-title").textContent = title;
  $("d-code").textContent = state.meeting ? state.meeting.code : roomName;
  $("conn-text").textContent = "Connected";
  $("panel-assistant").closest(".key").hidden = !state.meeting;       // the assistant needs an account
  $("noise-label").textContent = noiseName(state.me.noise);
  const since = state.meeting?.startedAt ? new Date(state.meeting.startedAt).getTime() : Date.now();
  clearInterval(state.elapsedTimer);
  const tick = () => {
    const sec = Math.max(0, Math.floor((Date.now() - since) / 1000));
    const hh = Math.floor(sec / 3600), mm = String(Math.floor((sec % 3600) / 60)).padStart(2, "0"), ss = String(sec % 60).padStart(2, "0");
    $("elapsed").textContent = hh ? `${hh}:${mm}:${ss}` : `${mm}:${ss}`;
  };
  tick();
  state.elapsedTimer = setInterval(tick, 1000);
}
const noiseName = (mode) => ({ off: "Noise: off", standard: "Noise", enhanced: "Enhanced", studio: "Studio" }[mode] || "Noise");

function openPanel(name) {
  const side = $("side");
  const same = !side.hidden && side.dataset.panel === name;
  for (const b of ["people", "assistant", "details"]) $(`panel-${b}`).setAttribute("aria-pressed", String(!same && b === name));
  if (same) { side.hidden = true; side.dataset.panel = ""; return; }
  side.hidden = false;
  side.dataset.panel = name;
  $("side-title").textContent = { people: "People", assistant: "Assistant", details: "Meeting details" }[name];
  side.querySelectorAll(".side-body").forEach((b) => { b.hidden = b.dataset.panel !== name; });
  if (name === "assistant") ensureAssistant();
}

// The assistant's own mic in the call: while it's on, the call mic is muted (what you say to the
// assistant isn't sent to the others); turning it off puts the call mic back as it was.
async function assistantMic(on) {
  if (on) {
    state.micBeforeAssistant = state.micOn;
    if (state.micOn) await setCallMic(false);
    $("assistant-banner").hidden = false;
  } else {
    $("assistant-banner").hidden = true;
    if (state.micBeforeAssistant && !state.micOn && state.room) await setCallMic(true);
    state.micBeforeAssistant = false;
  }
}

// The assistant in the call: the same agents as the Assistant page, attached to this meeting, and the
// same conversation Hey Mind uses, so spoken requests and their Confirm cards show up here too.
function ensureAssistant(conversation) {
  if (!state.meeting) return;
  if (!state.assistant) {
    state.assistant = new AssistantChat($("side-assistant"), {
      meetingCode: state.meeting.code, voice: true, dark: true, lang: state.me.speak,
      placeholder: "Ask the assistant… (only you see this)",
      onMicChange: assistantMic,
    });
    (conversation ? state.assistant.open(conversation) : state.assistant.openLatestForMeeting()).catch((e) => state.assistant.fail(e));
  } else if (conversation && conversation !== state.assistant.conversationId) {
    state.assistant.open(conversation).catch(() => {});
  }
}

// a partner who speaks the language I hear needs no translation: play their own voice
function applyVolumes() {
  if (!state.me) return;
  const people = humans();
  const sameLang = people.filter((p) => p.attributes.speak === state.me.hear);
  // Whose voice arrives cleaned (Studio): their raw microphone stays silent.
  const cleaned = new Set([...document.querySelectorAll('audio[data-clean="1"]')].map((a) => a.dataset.identity));
  document.querySelectorAll("audio[data-lk]").forEach((a) => {
    if (a.dataset.lk === "translation") a.volume = state.translationVol / 100;
    else if (!a.dataset.clean && cleaned.has(a.dataset.identity)) a.volume = 0;
    else a.volume = (sameLang.some((p) => p.identity === a.dataset.identity) ? 100 : state.originalVol) / 100;
  });
  const allSame = sameLang.length > 0 && sameLang.length === people.length;
  $("vol-original").disabled = allSame;
  $("original-hint").textContent = allSame
    ? "They speak your language, so you hear their own voice at full volume."
    : "Mix in the other person's own voice under the translation.";
  const key = sameLang.map((p) => p.identity).sort().join(",");
  if (key && key !== state.sameLangNotice) {
    const names = [...new Set(sameLang.map((p) => p.name || p.identity))];
    const who = names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)} speak` : `${names[0]} speaks`;
    toast(`${who} ${langName(state.me.hear)}, so you'll hear their own voice.`, { key: "same-lang" });
  }
  state.sameLangNotice = key;
}

// ------------------------------------------------------------------ conversation feed
function nameOf(identity) {
  if (state.me && identity === state.me.identity) return "You";
  const p = state.room?.remoteParticipants.get(identity);
  return (p && (p.name || p.identity)) || "Someone";
}
const partnerAttrs = (identity) => state.room?.remoteParticipants.get(identity)?.attributes || {};

function bubble(utt, speaker) {
  let u = state.utts.get(utt);
  if (u) return u;
  const mine = speaker === state.me.identity;
  const el = document.createElement("article");
  el.className = "utt" + (mine ? " mine" : "");
  const head = document.createElement("div");
  head.className = "utt-head";
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = nameOf(speaker);
  const langs = document.createElement("span");
  const status = document.createElement("span");
  status.className = "status";
  head.append(who, langs, status);
  const main = document.createElement("div");
  main.className = "utt-main partial";
  const sub = document.createElement("div");
  sub.className = "utt-sub " + (mine ? "they-hear" : "their-original");
  sub.hidden = true;
  const via = document.createElement("div");
  via.className = "utt-sub via-english";
  via.hidden = true;
  el.append(head, main, sub, via);

  $("empty").hidden = true;
  $("feed").appendChild(el);
  u = { id: utt, el, main, sub, via, status, langs, mine, speaker, original: "", translated: "" };
  state.utts.set(utt, u);
  return u;
}

// Follow new text while the reader is at the bottom; leave them alone if they scrolled up.
// Bubbles grow after they are added (translation arrives), so follow on every update.
let pinned = true;
$("feed").addEventListener("scroll", () => {
  const f = $("feed");
  pinned = f.scrollHeight - f.scrollTop - f.clientHeight < 80;
});
function followFeed() {
  if (pinned) $("feed").scrollTop = $("feed").scrollHeight;
}
new ResizeObserver(followFeed).observe($("feed"));   // rotating a phone reflows the bubbles

function setStatus(u, text, cls = "") {
  u.status.textContent = text;
  u.status.className = "status" + (cls ? " " + cls : "");
}

function labeled(el, tag, text) {
  const t = document.createElement("span");
  t.className = "tag";
  t.textContent = tag + " ";
  el.replaceChildren(t, text);
  el.hidden = !text;
}

function onAgentMessage(m) {
  if (!state.me) return;
  handleAgentMessage(m);
  followFeed();
}

// "Hey Mind": the assistant's answer to me, in my feed only (nobody else gets these messages).
function mindBubble(utt) {
  const id = `${utt}:mind`;
  let u = state.utts.get(id);
  if (u) return u;
  u = bubble(id, "mind");
  u.el.classList.add("mind");
  u.el.querySelector(".who").textContent = "Mind";
  u.langs.textContent = "only you hear this";
  return u;
}

function handleAgentMessage(m) {
  switch (m.type) {
    case "transcript": {
      if (!m.text) {
        if (m.final) state.utts.get(m.utt)?.el.remove();
        return;
      }
      const u = bubble(m.utt, m.speaker);
      u.original = m.text;
      if (m.assistant) {                    // my words to the assistant: private, not translated
        u.el.classList.add("to-mind");
        u.main.textContent = m.text;
        u.main.classList.toggle("partial", !m.final);
        u.langs.textContent = "to Mind, private";
        setStatus(u, m.final ? "asking..." : "listening...", m.final ? "done" : "");
        break;
      }
      if (u.mine && !m.final && u.el.classList.contains("to-mind")) break;
      if (m.final && m.echo) {            // the worker recognised a speaker's sound, not a person
        u.main.textContent = m.text;
        u.main.classList.remove("partial");
        u.el.classList.add("echo");
        setStatus(u, "echo, not translated", "done");
        u.status.title = "This matched the translated speech playing in the room, so it was not translated again.";
        break;
      }
      if (u.mine) {
        u.main.textContent = m.text;
        u.main.classList.toggle("partial", !m.final);
        u.langs.textContent = langName(m.lang);
        setStatus(u, m.final ? "translating..." : "listening...");
        break;
      }
      const needsTranslation = partnerAttrs(m.speaker).speak !== state.me.hear;
      u.langs.textContent = needsTranslation ? `${langName(m.lang)} → ${langName(state.me.hear)}` : langName(m.lang);
      if (u.translated) labeled(u.sub, langName(m.lang), m.text);
      else {
        u.main.textContent = m.text;
        u.main.classList.toggle("partial", !m.final || needsTranslation);
      }
      if (!m.final) setStatus(u, "listening...");
      else if (needsTranslation) setStatus(u, "translating...");
      else setStatus(u, "their own voice", "done");
      break;
    }
    case "translation": {
      const u = bubble(m.utt, m.speaker);
      if (m.pivot_text) labeled(u.via, "via English", m.pivot_text);
      if (u.mine) {
        labeled(u.sub, `They hear (${langName(m.lang)})`, m.text);
        if (m.final) setStatus(u, "sent");
      } else if (m.lang === state.me.hear) {
        u.translated = m.text;
        u.main.textContent = m.text;
        u.main.classList.toggle("partial", !m.final);
        labeled(u.sub, langName(partnerAttrs(m.speaker).speak), u.original);
        if (m.final) {
          setStatus(u, "voicing...");
          if (state.playing !== u && !state.playQueue.includes(u)) state.playQueue.push(u);
        }
      }
      break;
    }
    case "metrics": {
      const u = state.utts.get(m.utt);
      if (!u || m.audio_start_ms == null) return;
      u.delay = `+${(m.audio_start_ms / 1000).toFixed(1)} s`;
      u.status.title = `Speaker stopped -> translated voice: ${m.audio_start_ms} ms (pause detection ${m.vad_pause_ms}, ` +
        `transcript ${m.asr_final_ms}, translation ${m.translation_done_ms}, route ${m.route || "direct"})`;
      if (m.listener === state.me.identity) {
        if (state.playing !== u) setStatus(u, u.delay, "done");
        state.latencies.push(m.audio_start_ms);
        if (state.latencies.length > 20) state.latencies.shift();
        const sorted = [...state.latencies].sort((a, b) => a - b);
        $("latency").textContent = `delay ~${(sorted[Math.floor(sorted.length / 2)] / 1000).toFixed(1)} s`;
      } else if (u.mine) {
        setStatus(u, `heard ${u.delay}`, "done");
      }
      break;
    }
    case "mind_session":                               // my words are going to Mind (or to the call again)
      $("mind-banner").hidden = !m.active;
      $("ask-mind").setAttribute("aria-pressed", String(!!m.active));
      break;
    case "assistant": {
      const u = mindBubble(m.utt);
      if (m.state === "thinking") {
        u.main.textContent = "…";
        u.main.classList.add("partial");
        setStatus(u, "thinking...");
      } else {
        u.main.textContent = m.text || "";
        u.main.classList.remove("partial");
        setStatus(u, "voicing...");
        u.status.title = `Answered in ${m.ms} ms`;
        if (m.conversation && state.assistant) ensureAssistant(m.conversation);   // show it (and any Confirm card) in the panel
        if (state.playing !== u && !state.playQueue.includes(u)) state.playQueue.push(u);
      }
      followFeed();
      break;
    }
    case "status":
      if (m.message) toast(m.message, { key: "agent-status" });
      break;
  }
}

// The translation track carries the partner's utterances one after another, so
// "sound starts" marks the next queued bubble as playing and a pause ends it.
let quietSince = 0;
function onTranslationLevel(v) {
  if (v > 0.04) {
    quietSince = 0;
    if (!state.playing && state.playQueue.length) {
      state.playing = state.playQueue.shift();
      setStatus(state.playing, "playing", "playing");
    }
  } else if (state.playing) {
    quietSince ||= performance.now();
    if (performance.now() - quietSince > 900) {
      const u = state.playing;
      setStatus(u, u.delay || "played", "done");
      state.playing = null;
    }
  }
}

// ------------------------------------------------------------------ voice and tone
// A speaker's voice is a Magpie voice, optionally with a tone ("Sofia.Calm"); listeners hear the
// nearest their language has. Without an account (open demo) or a voice list, Louise and Leo.
const TONE_LABEL = { PleasantSurprised: "Pleasantly surprised" };
const LEGACY_VOICE = { female: "Louise", male: "Leo" };
function voiceLabel(id) {
  const [speaker, tone] = (LEGACY_VOICE[id] || id || "Louise").split(".");
  return tone ? `${speaker} · ${TONE_LABEL[tone] || tone}` : speaker;
}
async function loadVoices() {
  let list = [];
  try {
    const r = await fetch(`/api/voices?lang=${encodeURIComponent(store.get("speak", "en"))}`);
    if (r.ok) ({ voices: list, languages: state.voiceLangs } = await r.json());
  } catch { /* fall back below */ }
  if (!list.length) list = [{ id: "Louise", speaker: "Louise", tone: null }, { id: "Leo", speaker: "Leo", tone: null }];
  state.voices = new Map();                      // speaker -> [{id, tone}], the plain voice first
  for (const v of list) {
    if (!state.voices.has(v.speaker)) state.voices.set(v.speaker, []);
    state.voices.get(v.speaker).push(v);
  }
  for (const opts of state.voices.values()) opts.sort((a, b) => (a.tone === null ? -1 : b.tone === null ? 1 : 0));
}
function fillTones(suffix, speaker, id) {
  const opts = state.voices.get(speaker) || [];
  $(`tone${suffix}`).replaceChildren(...opts.map((v) => {
    const o = document.createElement("option");
    o.value = v.id;
    o.textContent = v.tone ? (TONE_LABEL[v.tone] || v.tone) : "Standard";
    return o;
  }));
  if (id && opts.some((v) => v.id === id)) $(`tone${suffix}`).value = id;
  $(`tone${suffix}`).disabled = opts.length < 2;
  showHeard(suffix);
}
function fillVoicePicker(suffix, id) {
  id = LEGACY_VOICE[id] || id || "Louise";
  let speaker = id.split(".")[0];
  if (!state.voices.has(speaker)) { speaker = state.voices.has("Louise") ? "Louise" : state.voices.keys().next().value; id = speaker; }
  $(`voice${suffix}`).replaceChildren(...[...state.voices.keys()].map((sp) => {
    const o = document.createElement("option");
    o.value = sp;
    o.textContent = sp + (state.voices.get(sp).length > 1 ? ` (${state.voices.get(sp).length - 1} tones)` : "");
    return o;
  }));
  $(`voice${suffix}`).value = speaker;
  fillTones(suffix, speaker, id);
}
// What listeners of each language hear for a choice (as the server picks it: tone, else voice, else Louise).
function heardIn(lang, id) {
  const ids = state.voiceLangs?.[lang];
  if (!ids?.length) return id;
  if (ids.includes(id)) return id;
  const speaker = id.split(".")[0];
  return ids.includes(speaker) ? speaker : (ids.includes("Louise") ? "Louise" : ids[0]);
}
function showHeard(suffix) {
  const out = $(`voice-heard${suffix}`);
  if (!out || !state.voiceLangs) return;
  const id = pickedVoice(suffix);
  out.textContent = "Heard as: " + Object.keys(state.voiceLangs).map((l) => `${langName(l)} ${voiceLabel(heardIn(l, id))}`).join(" · ");
}
function pickedVoice(suffix) {
  return $(`tone${suffix}`).value || $(`voice${suffix}`).value || "Louise";
}
async function previewVoice(voice, lang, btn) {
  if (state.sample) { state.sample.pause(); state.sample = null; btn.textContent = "Play a sample"; return; }
  btn.disabled = true;
  try {
    const r = await fetch("/api/voices/preview", { method: "POST", headers: { "content-type": "application/json" },
                                                   body: JSON.stringify({ voice, lang: lang === "hi" ? "hi" : "en" }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Sample not available");
    const url = URL.createObjectURL(await r.blob());
    const a = new Audio(url);
    state.sample = a;
    btn.textContent = "Stop sample";
    a.onended = () => { URL.revokeObjectURL(url); state.sample = null; btn.textContent = "Play a sample"; };
    await a.play();
  } catch (e) {
    toast(`Couldn't play the sample: ${e.message}`);
  } finally {
    btn.disabled = false;
  }
}
// Changes made in the call are remembered for this meeting (accounts only).
function savePreferences(prefs) {
  if (!state.meeting) return;
  fetch(`/api/meetings/${state.meeting.code}/preferences`, { method: "PATCH", headers: { "content-type": "application/json" },
                                                             body: JSON.stringify(prefs) }).catch(() => {});
}

// ------------------------------------------------------------------ settings
function syncSettingsForm() {
  $("noise-live").value = state.me.noise;
  $("speak-live").value = state.me.speak;
  $("hear-live").value = state.me.hear;
  fillVoicePicker("-live", state.me.voice);
}

async function updateAttributes() {
  if (!state.room) return;
  const attrs = { speak: $("speak-live").value, hear: $("hear-live").value, voice: pickedVoice("-live") };
  const voiceChanged = attrs.voice !== state.me.voice;
  Object.assign(state.me, attrs);
  for (const k of ["speak", "hear", "voice"]) store.set(k, attrs[k]);
  await state.room.localParticipant.setAttributes(attrs);
  savePreferences(attrs);
  renderPeople();
  toast(voiceChanged ? `Others will hear you as ${voiceLabel(attrs.voice)}.` : `You speak ${langName(attrs.speak)} and hear ${langName(attrs.hear)}.`, { key: "langs" });
}

async function refreshDevices() {
  if (!state.room) return;
  await fillDevices($("mic-live"), "audioinput", state.room.getActiveDevice("audioinput"));
  const canPickSpeaker = "setSinkId" in HTMLMediaElement.prototype;
  $("speaker-field").hidden = !canPickSpeaker;
  if (canPickSpeaker) await fillDevices($("speaker-live"), "audiooutput", state.room.getActiveDevice("audiooutput"));
}

async function switchDevice(kind, id) {
  try {
    await state.room.switchActiveDevice(kind, id);
    if (kind === "audioinput") {
      store.set("mic", id);
      watchMyMic();                 // the mic track was replaced
    }
  } catch (e) {
    toast(`Could not switch device: ${e.message}`);
  }
}

function applyCaptionPrefs() {
  document.body.classList.toggle("hide-english", !$("show-english").checked);
  document.body.classList.toggle("hide-original", !$("show-original").checked);
}

// ------------------------------------------------------------------ noise removal
// off: raw mic (echo cancellation stays on) · standard: the browser's noise suppression ·
// enhanced: RNNoise in the browser (an AI filter on the published track) · studio: the plain mic
// is sent and the agent cleans it with NVIDIA BNR before speech detection (two cleaners in a row
// make artefacts, so the browser's own suppression is off then).
function micConstraints(mode) {
  const dsp = mode === "standard" || mode === "enhanced";
  return { echoCancellation: true, noiseSuppression: dsp, autoGainControl: dsp };
}

let rnnoiseWasm = null;
function rnnoiseProcessor() {
  let ctx = null, node = null, source = null;
  const quietly = (f) => { try { f(); } catch { /* already torn down */ } };
  const teardown = async () => {
    quietly(() => source?.disconnect());
    quietly(() => node?.destroy());
    quietly(() => node?.disconnect());
    await ctx?.close().catch(() => {});
    ctx = node = source = null;
  };
  const proc = {
    name: "rnnoise",
    async init({ track }) {
      const lib = await import("/noise/web-noise-suppressor.js");
      rnnoiseWasm ??= lib.loadRnnoise({ url: "/noise/rnnoise.wasm", simdUrl: "/noise/rnnoise_simd.wasm" });
      ctx = new AudioContext({ sampleRate: 48000 });
      await ctx.audioWorklet.addModule("/noise/rnnoiseWorklet.js");
      node = new lib.RnnoiseWorkletNode(ctx, { maxChannels: 1, wasmBinary: await rnnoiseWasm });
      source = ctx.createMediaStreamSource(new MediaStream([track]));
      const dest = ctx.createMediaStreamDestination();
      source.connect(node).connect(dest);
      proc.processedTrack = dest.stream.getAudioTracks()[0];
    },
    async restart(opts) { await teardown(); await proc.init(opts); },
    async destroy() { await teardown(); proc.processedTrack = undefined; },
  };
  return proc;
}

// Changes are applied one at a time, in order.
function applyNoise(mode) {
  state.noiseChain = state.noiseChain.then(async () => {
    const track = state.room?.localParticipant.getTrackPublication(Track.Source.Microphone)?.track;
    if (!track) return;
    // Take the RNNoise filter off before restarting the mic: while it's on, the track is the filter's
    // output, and asking for "its" device again fails (OverconstrainedError).
    if (track.getProcessor()) await track.stopProcessor();
    const deviceId = track.mediaStreamTrack?.getSettings().deviceId;
    try {
      await track.restartTrack({ ...micConstraints(mode), ...(deviceId ? { deviceId: { exact: deviceId } } : {}) });
    } catch (e) {
      if (e?.name !== "OverconstrainedError" || !deviceId) throw e;
      await track.restartTrack(micConstraints(mode));          // that mic is gone: the default one
    }
    if (mode === "enhanced") await track.setProcessor(rnnoiseProcessor());
    watchMyMic();
  }).catch((e) => {
    console.error("[noise] couldn't apply", mode, e);
    toast(`Couldn't change noise removal${e?.message ? `: ${e.message}` : ". Try again, or rejoin the call."}`);
  });
  return state.noiseChain;
}

async function changeNoise() {
  const mode = $("noise-live").value;
  state.me.noise = mode;
  store.set("noise", mode);
  await applyNoise(mode);
  await state.room?.localParticipant.setAttributes({ noise: mode });   // the agent switches BNR on or off
  savePreferences({ noise: mode });
  $("noise-label").textContent = noiseName(mode);
  const label = $("noise-live").selectedOptions[0]?.textContent.split(" (")[0] ?? mode;
  toast(`Background noise removal: ${label}.`, { key: "noise" });
}

// ------------------------------------------------------------------ wiring
$("join-form").addEventListener("submit", join);
$("leave").addEventListener("click", () => leave());
for (const name of ["people", "assistant", "details"]) $(`panel-${name}`).addEventListener("click", () => openPanel(name));
$("side-close").addEventListener("click", () => openPanel($("side").dataset.panel));
$("captions-toggle").addEventListener("click", () => {
  const on = $("captions-toggle").getAttribute("aria-pressed") !== "true";
  $("captions-toggle").setAttribute("aria-pressed", String(on));
  $("feed").hidden = !on;
});
$("d-copy").addEventListener("click", () => $("copy-link").click());
// "Ask Mind": a conversation with the assistant starts, as after saying "Hey Mind"; pressed again
// (or Done on the banner), it ends and your words go to the call again.
async function toMind(msg) {
  const agent = state.room && [...state.room.remoteParticipants.values()].find(isAgent);
  if (!agent) { toast("The assistant joins with the translator. Try again in a moment."); return false; }
  await state.room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify(msg)), { reliable: true, topic: "mind", destinationIdentities: [agent.identity] });
  return true;
}
$("ask-mind").addEventListener("click", async () => {
  if (!$("mind-banner").hidden) { await toMind({ end: true }); return; }
  if (await toMind({})) toast("Mind is listening: ask your question. Only you hear the answer.", { key: "mind" });
});
$("mind-done").addEventListener("click", () => toMind({ end: true }));
$("new-room").addEventListener("click", () => { $("room").value = newRoomName(); });
$("mic-test").addEventListener("click", () => (state.testMic ? stopMicTest() : startMicTest()));
$("mic-device").addEventListener("change", startMicTest);
$("speak").addEventListener("change", () => { if (!state.hearTouched) $("hear").value = $("speak").value; });
$("hear").addEventListener("change", () => { state.hearTouched = true; });

$("speak-live").addEventListener("change", updateAttributes);
$("noise-live").addEventListener("change", changeNoise);
$("hear-live").addEventListener("change", updateAttributes);
$("voice-live").addEventListener("change", () => { fillTones("-live", $("voice-live").value); updateAttributes(); });
$("tone-live").addEventListener("change", () => { showHeard("-live"); updateAttributes(); });
$("tone").addEventListener("change", () => showHeard(""));
$("voice").addEventListener("change", () => fillTones("", $("voice").value));
$("voice-preview").addEventListener("click", (e) => previewVoice(pickedVoice(""), $("speak").value, e.currentTarget));
$("voice-preview-live").addEventListener("click", (e) => previewVoice(pickedVoice("-live"), state.me?.speak, e.currentTarget));
$("mic-live").addEventListener("change", (e) => switchDevice("audioinput", e.target.value));
$("speaker-live").addEventListener("change", (e) => switchDevice("audiooutput", e.target.value));
document.querySelectorAll('button[commandfor="settings"][command="show-modal"]').forEach((b) => b.addEventListener("click", refreshDevices));

$("vol-translation").addEventListener("input", (e) => {
  state.translationVol = +e.target.value;
  $("vol-translation-out").textContent = `${state.translationVol}%`;
  applyVolumes();
});
$("vol-original").addEventListener("input", (e) => {
  state.originalVol = +e.target.value;
  $("vol-original-out").textContent = `${state.originalVol}%`;
  applyVolumes();
});
for (const [id, key] of [["show-english", "showEnglish"], ["show-original", "showOriginal"]]) {
  $(id).addEventListener("change", () => {
    store.set(key, $(id).checked ? "1" : "0");
    applyCaptionPrefs();
  });
}

async function setCallMic(on) {
  state.micOn = on;
  await state.room.localParticipant.setMicrophoneEnabled(on);
  $("mute").setAttribute("aria-pressed", String(!on));
  $("mute-label").textContent = on ? "Mute" : "Unmute";
  if (on) watchMyMic();
}
$("mute").addEventListener("click", async () => {
  // Unmuting for the call while talking to the assistant: the assistant's mic goes off first.
  if (!state.micOn && state.assistant?.v.on) { state.micBeforeAssistant = false; state.assistant.micOff(); }
  await setCallMic(!state.micOn);
});

$("copy-link").addEventListener("click", async () => {
  const link = state.meeting ? `${location.origin}/m/${state.meeting.code}`
    : `${location.origin}${location.pathname}?room=${encodeURIComponent($("room-name").textContent)}`;
  try {
    await navigator.clipboard.writeText(link);
    toast("Invite link copied. Send it to the other person.", { key: "invite" });
  } catch {
    toast(`Invite link: ${link}`, { key: "invite", timeout: 15000 });
  }
});

// Meeting mode: /m/<code> is a meeting of your organisation; your name comes from your account.
async function loadMeeting() {
  const m = location.pathname.match(/^\/m\/([a-z]{3}-[a-z]{4}-[a-z]{3})$/);
  if (!m) return;
  const [meRes, mtRes] = await Promise.all([fetch("/api/me"), fetch(`/api/meetings/${m[1]}`)]);
  if (meRes.status === 401) { location.assign(`/login?next=${encodeURIComponent(location.pathname)}`); return; }
  if (!mtRes.ok) throw new Error((await mtRes.json().catch(() => ({}))).detail || "Meeting not found");
  const me = await meRes.json(), { meeting, myPreferences } = await mtRes.json();
  state.meeting = { code: meeting.joinCode, title: meeting.title, myRole: meeting.myRole, startedAt: meeting.startedAt };
  $("name").value = me.user.displayName;
  $("name").readOnly = true;
  $("room").value = meeting.joinCode;
  $("room-field").hidden = true;
  document.querySelector(".join-intro h1").textContent = meeting.title;
  document.querySelector(".join-intro .lede").textContent =
    meeting.status === "ENDED" || meeting.status === "CANCELLED" ? "This meeting has ended." : `Hosted by ${meeting.host.displayName}. Choose your languages and join.`;
  const pref = (k, fallback) => myPreferences?.[k] || fallback;
  const fill = (id, v) => { if (v && [...$(id).options].some((o) => o.value === v)) $(id).value = v; };
  fill("speak", pref("speak", me.user.preferredLanguage));
  fill("hear", pref("hear", $("speak").value));
  fill("noise", pref("noise", $("noise").value));
  if (myPreferences?.voice) fillVoicePicker("", myPreferences.voice);
  if (meeting.status === "ENDED" || meeting.status === "CANCELLED") $("join-btn").disabled = true;
}

// "Save transcript": the host's switch, and a notice for everyone while it's on (re-read every 8 s).
function showSaving(on) {
  state.meeting.saving = on;
  $("save-banner").hidden = !on;
  $("save-toggle").setAttribute("aria-pressed", String(on));
  $("save-label").textContent = on ? "Stop saving" : "Save transcript";
}
async function watchMeeting() {
  clearInterval(state.meetingTimer);
  const check = async () => {
    if (!state.room || !state.meeting) return clearInterval(state.meetingTimer);
    const r = await fetch(`/api/meetings/${state.meeting.code}`).catch(() => null);
    if (r?.ok) {
      const on = !!(await r.json()).meeting.saveTranscript;
      if (on !== state.meeting.saving) {
        if (on) toast("The host is saving this meeting's transcript.", { key: "saving" });
        showSaving(on);
      }
    }
  };
  state.meeting.saving = undefined;
  await check();
  state.meetingTimer = setInterval(check, 8000);
}
$("save-toggle").addEventListener("click", async () => {
  const on = !state.meeting.saving;
  if (on && !confirm("Save this meeting's transcript? Everyone in the call will be told.")) return;
  const r = await fetch(`/api/meetings/${state.meeting.code}/transcript`, { method: "POST", headers: { "content-type": "application/json" },
                                                                        body: JSON.stringify({ enabled: on }) });
  if (!r.ok) { toast((await r.json().catch(() => ({}))).detail || "Couldn't change it"); return; }
  showSaving((await r.json()).saveTranscript);
});

$("end-all").addEventListener("click", async () => {
  if (!state.meeting || !confirm("End the meeting for everyone?")) return;
  const r = await fetch(`/api/meetings/${state.meeting.code}/end`, { method: "POST" });
  if (!r.ok) { toast((await r.json().catch(() => ({}))).detail || "Couldn't end the meeting"); return; }
  await leave();
});

loadLanguages().then(loadMeeting).catch((e) => {
  $("join-error").textContent = `Could not load languages: ${e.message}`;
  $("join-error").hidden = false;
});
