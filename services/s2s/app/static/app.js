// Indic Call Translator - demo client.
// Joins a LiveKit room, publishes the mic, plays the translation track the agent
// publishes for this caller, and renders captions from the agent's "captions" data.
"use strict";

const { Room, RoomEvent, Track, ParticipantKind } = LivekitClient;
const $ = (id) => document.getElementById(id);

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
const radioValue = (name) => document.querySelector(`input[name="${name}"]:checked`)?.value || "female";
const setRadio = (name, value) => {
  const r = document.querySelector(`input[name="${name}"][value="${value}"]`);
  if (r) r.checked = true;
};

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
  setRadio("voice", store.get("voice", "female"));
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
                speak: $("speak").value, hear: $("hear").value, voice: radioValue("voice"), noise: $("noise").value };
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
    $("end-all").hidden = !(state.meeting && ["HOST", "COHOST"].includes(state.meeting.myRole));
    $("save-toggle").hidden = $("end-all").hidden;
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
  $("join").hidden = false;
  if (state.meeting) location.assign(note === "ended" ? "/dashboard?ended=1" : "/dashboard");
}

// ------------------------------------------------------------------ room
const isAgent = (p) => p && (p.kind === ParticipantKind.AGENT || p.isAgent);
const humans = () => state.room ? [...state.room.remoteParticipants.values()].filter((p) => !isAgent(p)) : [];

function wireRoom(room) {
  room
    .on(RoomEvent.Connected, () => $("conn-dot").classList.add("on"))
    .on(RoomEvent.Reconnecting, () => {
      $("conn-dot").classList.remove("on");
      toast("Connection lost, reconnecting...", { key: "conn", timeout: 0 });
    })
    .on(RoomEvent.Reconnected, () => { $("conn-dot").classList.add("on"); toast("Reconnected.", { key: "conn" }); })
    .on(RoomEvent.Disconnected, async () => {
      $("conn-dot").classList.remove("on");
      // Dropped without leaving: if the host ended the meeting, say so and leave (not a dead call screen).
      if (!state.meeting || state.room !== room) return;
      const r = await fetch(`/api/meetings/${state.meeting.code}`).catch(() => null);
      const status = r?.ok ? (await r.json()).meeting.status : null;
      if (status === "ENDED" || status === "CANCELLED") await leave("ended");
    })
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
      el.dataset.lk = isAgent(participant) ? "translation" : "original";
      el.dataset.identity = participant.identity;
      document.body.appendChild(el);
      if (isAgent(participant)) watchLevel("translation", track.mediaStreamTrack, onTranslationLevel);
      else watchLevel(participant.identity, track.mediaStreamTrack, (v) => setLevel(avatarOf(participant.identity), v));
      applyVolumes();
    })
    .on(RoomEvent.TrackUnsubscribed, (track, pub, participant) => {
      track.detach().forEach((el) => el.remove());
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
  const tiles = [personTile(me.identity, me.name, me.speak, me.hear, true)];
  const others = humans();
  for (const p of others) tiles.push(personTile(p.identity, p.name || p.identity, p.attributes.speak, p.attributes.hear, false));
  if (!others.length) {
    const w = document.createElement("div");
    w.className = "person card waiting";
    w.textContent = "Waiting for the other person - send them the invite link.";
    tiles.push(w);
  }
  $("people").replaceChildren(...tiles);
  applyVolumes();
}

function personTile(identity, name, speak, hear, mine) {
  const el = document.createElement("div");
  el.className = "person card" + (mine ? " me" : "");
  el.dataset.identity = identity;
  const av = document.createElement("div");
  av.className = "avatar";
  av.textContent = (name || "?").trim().slice(0, 1).toUpperCase();
  const info = document.createElement("div");
  const nm = document.createElement("div");
  nm.className = "person-name";
  nm.textContent = mine ? `${name} (you)` : name;
  const langs = document.createElement("div");
  langs.className = "person-langs";
  const b1 = document.createElement("b");
  b1.textContent = langName(speak);
  const b2 = document.createElement("b");
  b2.textContent = langName(hear);
  langs.append("speaks ", b1, " · hears ", b2);
  info.append(nm, langs);
  el.append(av, info);
  return el;
}

// a partner who speaks the language I hear needs no translation: play their own voice
function applyVolumes() {
  if (!state.me) return;
  const people = humans();
  const sameLang = people.filter((p) => p.attributes.speak === state.me.hear);
  document.querySelectorAll("audio[data-lk]").forEach((a) => {
    if (a.dataset.lk === "translation") a.volume = state.translationVol / 100;
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

// ------------------------------------------------------------------ settings
function syncSettingsForm() {
  $("noise-live").value = state.me.noise;
  $("speak-live").value = state.me.speak;
  $("hear-live").value = state.me.hear;
  setRadio("voice-live", state.me.voice);
}

async function updateAttributes() {
  if (!state.room) return;
  const attrs = { speak: $("speak-live").value, hear: $("hear-live").value, voice: radioValue("voice-live") };
  Object.assign(state.me, attrs);
  for (const k of ["speak", "hear", "voice"]) store.set(k, attrs[k]);
  await state.room.localParticipant.setAttributes(attrs);
  renderPeople();
  toast(`You speak ${langName(attrs.speak)} and hear ${langName(attrs.hear)}.`, { key: "langs" });
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
  const label = $("noise-live").selectedOptions[0]?.textContent.split(" (")[0] ?? mode;
  toast(`Background noise removal: ${label}.`, { key: "noise" });
}

// ------------------------------------------------------------------ wiring
$("join-form").addEventListener("submit", join);
$("leave").addEventListener("click", () => leave());
$("new-room").addEventListener("click", () => { $("room").value = newRoomName(); });
$("mic-test").addEventListener("click", () => (state.testMic ? stopMicTest() : startMicTest()));
$("mic-device").addEventListener("change", startMicTest);
$("speak").addEventListener("change", () => { if (!state.hearTouched) $("hear").value = $("speak").value; });
$("hear").addEventListener("change", () => { state.hearTouched = true; });

$("speak-live").addEventListener("change", updateAttributes);
$("noise-live").addEventListener("change", changeNoise);
$("hear-live").addEventListener("change", updateAttributes);
document.querySelectorAll('input[name="voice-live"]').forEach((r) => r.addEventListener("change", updateAttributes));
$("mic-live").addEventListener("change", (e) => switchDevice("audioinput", e.target.value));
$("speaker-live").addEventListener("change", (e) => switchDevice("audiooutput", e.target.value));
document.querySelector('button[commandfor="settings"][command="show-modal"]').addEventListener("click", refreshDevices);

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

$("mute").addEventListener("click", async () => {
  state.micOn = !state.micOn;
  await state.room.localParticipant.setMicrophoneEnabled(state.micOn);
  $("mute").setAttribute("aria-pressed", String(!state.micOn));
  $("mute-label").textContent = state.micOn ? "Mute" : "Unmute";
  if (state.micOn) watchMyMic();
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
  state.meeting = { code: meeting.joinCode, title: meeting.title, myRole: meeting.myRole };
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
  if (myPreferences?.voice) setRadio("voice", myPreferences.voice);
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
