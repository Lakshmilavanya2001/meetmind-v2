"""Call translator: a LiveKit agent worker that joins every room and translates
between the people in it.

Each caller sets three participant attributes (the demo page does this):
``speak`` (the language they talk in), ``hear`` (the language they want to hear)
and ``voice`` (female | male, the voice their translated speech is read in).

For every caller (as a speaker)
    mic track -> VAD (here) -> each utterance to the ASR NIM (partial + final transcripts)
For every final utterance, for each other caller (as a listener)
    hear == speak  -> nothing to translate; the page plays the original audio
    otherwise      -> translate into ``hear`` (once per target language)
                   -> TTS -> that listener's own track ("translation-<identity>"),
                      which only they are allowed to subscribe to
Captions (partial/final transcripts, streamed translations) and per-utterance
latency go to every caller on the "captions" data topic.

Model services: speech recognition is the ASR NIM (IndicConformer on NVIDIA Riva, HTTP
API, one request per utterance, see asr.py), translation is sarvam-translate on the model-free LLM
NIM (OpenAI chat API), and speech is the TTS service's WebSocket API (indic-speak, whose
speech LM runs in a NIM too). See the repo README.
"""
from __future__ import annotations

import asyncio
import bisect
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import aiohttp
import numpy as np
import websockets
from livekit import rtc
from livekit.agents import AgentServer, AutoSubscribe, JobContext, cli

from .asr import AsrStream
from .languages import HEAR, PROMPT_NAME, SPEAK

log = logging.getLogger("s2s")

TRANSLATE_URL = os.environ.get("TRANSLATE_URL", "http://127.0.0.1:8002/v1")      # OpenAI-compatible
TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "sarvam-translate")
TTS_URL = os.environ.get("TTS_URL", "ws://127.0.0.1:8003/v1/stream")
ASR_PARTIALS = os.environ.get("S2S_ASR_PARTIALS", "1") == "1"
VAD_MIN_SILENCE_MS = int(os.environ.get("S2S_VAD_MIN_SILENCE_MS", "500"))
# Echo protection. A browser cancels echo from its own speaker only. Two devices in one
# room, or a speaker loud enough to beat the browser, feed translated speech back into a
# microphone, where it is transcribed and translated again, back and forth. The worker
# knows everything it plays, so it cancels that audio out of every microphone itself
# (WebRTC AEC3, via LiveKit's AudioProcessingModule) before speech recognition.
ECHO_CANCEL = os.environ.get("S2S_ECHO_CANCEL", "1") == "1"
LOG_TEXT = os.environ.get("S2S_LOG_TEXT", "1") == "1"   # transcripts and translations in the log
# an utterance whose loudness follows the room's playout this closely is echo, not a person
# (measured: people talking over the playout 0.53-0.66, echo 0.79-0.99)
ECHO_SCORE_MAX = float(os.environ.get("S2S_ECHO_SCORE_MAX", "0.72"))
# a short echo scores noisily; a moderate score plus words the room just played also counts
ECHO_SCORE_WITH_TEXT = 0.5
SPEECH_PAD_MS = 200                 # the ASR service's default trailing pad in end_ms
IN_SR = 16_000                      # what the ASR wants
OUT_SR = 48_000                     # WebRTC's native rate; the TTS service resamples to it
OUT_FRAME = OUT_SR // 50            # 20 ms frames into the LiveKit audio source
CAPTIONS_TOPIC = "captions"

# Translation route. sarvam-translate's open checkpoint translates English <-> Indian
# languages; between two Indian languages its model card says to go through English. So
# Indian -> Indian is two calls (source -> English -> target), anything with English one.


def route(src: str, tgt: str) -> str:
    return "direct" if "en" in (src, tgt) else "pivot"


@dataclass
class Caller:
    identity: str
    name: str
    speak: str
    hear: str
    voice: str

    @staticmethod
    def from_participant(p: rtc.RemoteParticipant) -> "Caller":
        a = p.attributes
        speak = a.get("speak", "hi") if a.get("speak") in SPEAK else "hi"
        hear = a.get("hear", speak) if a.get("hear") in HEAR else speak
        voice = "male" if a.get("voice") == "male" else "female"
        return Caller(p.identity, p.name or p.identity, speak, hear, voice)

    def public(self) -> dict:
        return {"identity": self.identity, "name": self.name, "speak": self.speak,
                "hear": self.hear, "voice": self.voice}


@dataclass
class Utterance:
    id: str
    speaker: str
    text: str
    speech_end: float              # wall clock when the speaker stopped (from stream time)
    final_at: float                # wall clock when the final transcript arrived
    marks: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------- translate
class TranslateClient:
    """Streamed chat completions from the translate NIM (sarvam-translate, OpenAI-compatible
    API). One HTTP session per call; requests run concurrently.

    The request follows the model card: a system message naming the target language,
    the text as the user message, (near-)greedy decoding. Gemma's end-of-turn token
    ends the reply (it is in the model's generation config)."""

    SYSTEM = "Translate the text below to {tgt}."

    def __init__(self):
        self.http: aiohttp.ClientSession | None = None

    async def _complete(self, text: str, tgt_name: str, on_delta=None) -> str:
        if self.http is None:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=5, sock_read=30))
        body = {
            "model": TRANSLATE_MODEL, "stream": True, "temperature": 0,
            # Indic scripts can take ~2-3x the tokens of the source; cap runaway generations
            "max_tokens": min(1024, 2 * len(text) + 32),
            "messages": [{"role": "system", "content": self.SYSTEM.replace("{tgt}", tgt_name)},
                         {"role": "user", "content": text}],
        }
        parts = []
        async with self.http.post(f"{TRANSLATE_URL}/chat/completions", json=body) as r:
            if r.status != 200:
                raise RuntimeError(f"translate service: HTTP {r.status} {(await r.text())[:200]}")
            async for line in r.content:                              # server-sent events
                if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                    continue
                delta = json.loads(line[6:])["choices"][0]["delta"].get("content")
                if delta:
                    parts.append(delta)
                    if on_delta is not None:
                        await on_delta("".join(parts))
        return "".join(parts).strip()

    async def translate(self, text: str, src: str, tgt: str, on_delta) -> tuple[str, dict]:
        text = " ".join(text.split())
        if route(src, tgt) == "pivot":
            english = await self._complete(text, "English")
            return await self._complete(english, PROMPT_NAME[tgt], on_delta), {"route": "pivot", "pivot_text": english}
        return await self._complete(text, PROMPT_NAME[tgt], on_delta), {"route": "direct"}

    async def close(self):
        if self.http is not None:
            await self.http.close()


# ---------------------------------------------------------------------------- echo reference
class PlayoutTimeline:
    """Everything the worker plays into the room, placed at the wall-clock time it starts
    playing (48 kHz). Every microphone's echo canceller reads its reference from here."""

    def __init__(self, keep_s: float = 5.0):
        self.starts: list[float] = []
        self.chunks: list[np.ndarray] = []
        self.keep_s = keep_s

    def add(self, start: float, pcm16: bytes):
        i = bisect.bisect(self.starts, start)
        self.starts.insert(i, start)
        self.chunks.insert(i, np.frombuffer(pcm16, np.int16).astype(np.float32))
        cutoff = time.time() - self.keep_s
        while self.starts and self.starts[0] < cutoff:
            self.starts.pop(0)
            self.chunks.pop(0)

    def read(self, t0: float, n: int) -> bytes:
        """n samples of what was playing from wall time t0 on, as 16-bit PCM."""
        out = np.zeros(n, np.float32)
        j = bisect.bisect_left(self.starts, t0 - 0.1)       # chunks are 20 ms; start a bit early
        while j < len(self.starts) and self.starts[j] < t0 + n / OUT_SR:
            x = self.chunks[j]
            off = int(round((self.starts[j] - t0) * OUT_SR))
            a, b = max(0, off), min(n, off + len(x))
            if a < b:
                out[a:b] += x[a - off: b - off]
            j += 1
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


class EchoMonitor:
    """Watches one microphone against the room's playout, as loudness envelopes in 10 ms steps.

    1. Echo-path delay, up to 1.5 s. AEC3 only searches about 0.4 s back, but over real
       networks the path worker -> a speaker -> this mic -> worker can be longer. On a
       clear, repeated correlation peak beyond that range, the echo canceller's reference
       is shifted by the lag minus a margin, leaving AEC3 only the fine alignment.
    2. Echo score of an utterance: how closely its envelope follows the playout at the
       best lag. Echo follows it almost exactly; a person talking does not. This also
       catches what leaks through while AEC3 is still converging.
    3. Whether playout is leaking into this mic at all (a strong peak in the last 20 s).
       Only then does speech recognition get the echo-cancelled audio: AEC3 turns down the
       start of speech that overlaps playback even when nothing leaks, which cost the first
       word of such utterances."""

    STEP_S, MAX_LAG, WINDOW, HISTORY = 0.01, 150, 400, 2200
    # AEC3 handles up to ~0.4 s itself. Every shift makes it re-converge (when echo leaks),
    # so shift only when the delay left over for AEC3 falls outside that range, and only on
    # a strong peak seen three times in a row.
    AEC_RANGE_S, MARGIN_S, MIN_CORR, AGREE = 0.4, 0.25, 0.8, 3
    LEAK_HOLD_S = 20.0

    def __init__(self):
        self.ref = np.zeros(0, np.float32)
        self.mic = np.zeros(0, np.float32)
        self.t_last = 0.0                       # wall time of the newest 10 ms step
        self.seconds = 0.0                      # current reference shift
        self.lags: list[int] = []               # recent strong peaks
        self.count = 0
        self.leak_seen = float("-inf")          # wall time of the last strong peak

    @staticmethod
    def _level(pcm: bytes) -> float:
        x = np.frombuffer(pcm, np.int16).astype(np.float32)
        return float(np.log10(np.mean(x * x) + 1.0))

    def observe(self, t: float, ref_pcm: bytes, mic_pcm: bytes):
        self.ref = np.append(self.ref, self._level(ref_pcm))[-self.HISTORY:]
        self.mic = np.append(self.mic, self._level(mic_pcm))[-self.HISTORY:]
        self.t_last = t
        self.count += 1
        if self.count % 100 == 0 and len(self.mic) >= self.WINDOW + self.MAX_LAG:
            self._estimate()

    def _best_lag(self, i0: int, i1: int) -> tuple[float, int | None]:
        """Best correlation of mic[i0:i1] with the playout `lag` steps earlier."""
        m = self.mic[i0:i1] - self.mic[i0:i1].mean()
        nm = np.linalg.norm(m)
        best, best_lag = 0.0, None
        if nm < 1e-6:
            return best, best_lag
        for lag in range(min(self.MAX_LAG, i0 + 1)):
            r = self.ref[i0 - lag: i1 - lag]
            if r.max() - r.min() < 2.0:             # nothing was playing then
                continue
            r = r - r.mean()
            c = float((m @ r) / (nm * np.linalg.norm(r) + 1e-9))      # plain float: it goes into JSON
            if c > best:
                best, best_lag = c, lag
        return best, best_lag

    def _estimate(self):
        n = len(self.mic)
        best, lag = self._best_lag(n - self.WINDOW, n)
        if lag is None or best < self.MIN_CORR:
            self.lags.clear()
            return
        if self.t_last - self.leak_seen >= self.LEAK_HOLD_S:
            log.info("playout is leaking into a microphone (correlation %.2f): cancelling it", best)
        self.leak_seen = self.t_last
        self.lags = (self.lags + [lag])[-self.AGREE:]
        if len(self.lags) < self.AGREE or max(self.lags) - min(self.lags) > 3:
            return
        lag_s = lag * self.STEP_S
        left_for_aec = lag_s - self.seconds
        if 0.0 <= left_for_aec <= self.AEC_RANGE_S:       # AEC3 can reach it: leave it alone
            return
        new = max(0.0, lag_s - self.MARGIN_S)
        log.info("echo path delay %.0f ms (correlation %.2f): reference shifted %.0f ms",
                 lag_s * 1000, best, new * 1000)
        self.seconds = new

    @property
    def leaking(self) -> bool:
        return self.t_last - self.leak_seen < self.LEAK_HOLD_S

    def echo_score(self, t0: float, t1: float) -> float:
        """0..1: how much the mic audio between wall times t0 and t1 looks like playout echo."""
        n = len(self.mic)
        i0 = max(0, n - int(round((self.t_last - t0) / self.STEP_S)))
        i1 = min(n, n - int(round((self.t_last - t1) / self.STEP_S)))
        if i1 - i0 < 10:
            return 0.0
        return self._best_lag(i0, i1)[0]


# ---------------------------------------------------------------------------- listener side
class ListenerOutput:
    """What one caller hears: their own published track, fed by their own TTS
    socket. The TTS service synthesizes one request at a time per socket, so
    translations reach the listener in utterance order."""

    def __init__(self, call: "CallTranslator", identity: str):
        self.call, self.identity = call, identity
        self.source = rtc.AudioSource(OUT_SR, 1, queue_size_ms=1000)
        self.track = rtc.LocalAudioTrack.create_audio_track(f"translation-{identity}", self.source)
        self.sid: str | None = None
        self.ws = None
        self.reader: asyncio.Task | None = None
        self.current: Utterance | None = None
        self.waiting: dict[str, Utterance] = {}
        self.lock = asyncio.Lock()

    async def start(self):
        opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        pub = await self.call.room.local_participant.publish_track(self.track, opts)
        self.sid = pub.sid

    async def _ensure_tts(self):
        async with self.lock:
            if self.ws is not None and self.reader is not None and not self.reader.done():
                return
            self.ws = await websockets.connect(TTS_URL, max_size=None, ping_interval=20)
            await self.ws.recv()                                  # ready
            self.reader = asyncio.create_task(self._read())

    async def say(self, utt: Utterance, text: str, lang: str, voice: str):
        await self._ensure_tts()
        self.waiting[utt.id] = utt
        await self.ws.send(json.dumps({"type": "synthesize", "id": utt.id, "text": text, "language": lang,
                                       "gender": voice, "sample_rate": OUT_SR, "format": "pcm_s16le"}))

    async def _read(self):
        pending = b""
        try:
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    pending += msg
                    n = len(pending) // (OUT_FRAME * 2) * OUT_FRAME * 2
                    for i in range(0, n, OUT_FRAME * 2):
                        await self._push(pending[i: i + OUT_FRAME * 2])
                    pending = pending[n:]
                    continue
                m = json.loads(msg)
                if m["type"] == "start":
                    self.current = self.waiting.pop(m["id"], None)
                elif m["type"] in ("done", "cancelled", "error"):
                    if pending:                                   # flush the last partial frame
                        await self._push(pending + b"\0" * (OUT_FRAME * 2 - len(pending)))
                        pending = b""
                    if m["type"] == "error":
                        log.error("tts error for %s: %s", m.get("id"), m.get("message"))
                    if self.current is not None and m["type"] == "done":
                        await self.call.utterance_audio_done(self.current, self.identity, m)
                    self.current = None
        except websockets.ConnectionClosed:
            log.warning("tts socket for %s closed", self.identity)

    async def _push(self, pcm: bytes):
        play_at = time.time() + self.source.queued_duration       # after whatever is still queued
        utt = self.current
        if utt is not None and f"audio:{self.identity}" not in utt.marks:
            utt.marks[f"audio:{self.identity}"] = play_at
        self.call.timeline.add(play_at, pcm)
        frame = rtc.AudioFrame(pcm, OUT_SR, 1, len(pcm) // 2)
        await self.source.capture_frame(frame)

    async def close(self):
        if self.reader is not None:
            self.reader.cancel()
        if self.ws is not None:
            await self.ws.close()
        if self.sid is not None:
            with contextlib.suppress(Exception):
                await self.call.room.local_participant.unpublish_track(self.sid)
        await self.source.aclose()


# ---------------------------------------------------------------------------- speaker side
class SpeakerInput:
    """One caller's microphone, cut into utterances and transcribed in their ``speak`` language."""

    def __init__(self, call: "CallTranslator", identity: str, track: rtc.RemoteAudioTrack):
        self.call, self.identity, self.track = call, identity, track
        self.lang: str | None = None
        self.task: asyncio.Task | None = None
        # stream position (16 kHz samples sent) -> wall clock, to place ASR timestamps in time
        self.sent_samples: list[int] = []
        self.sent_wall: list[float] = []
        self.echo = EchoMonitor()

    def start(self, lang: str):
        if self.task is not None and self.lang == lang:
            return
        self.stop()
        self.lang = lang
        self.task = asyncio.create_task(self._run(lang))

    def stop(self):
        if self.task is not None:
            self.task.cancel()
            self.task = None

    def wall_at(self, stream_ms: int) -> float:
        i = bisect.bisect_left(self.sent_samples, stream_ms * IN_SR // 1000)
        if not self.sent_wall:
            return time.time()
        return self.sent_wall[min(i, len(self.sent_wall) - 1)]

    async def _run(self, lang: str):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=5, sock_read=30)) as http:
            asr = AsrStream(http, lang, ASR_PARTIALS, VAD_MIN_SILENCE_MS)
            self.sent_samples, self.sent_wall = [], []
            log.info("speech recognition for %s (%s) started", self.identity, lang)
            pump = asyncio.create_task(self._pump(asr))
            try:
                while True:
                    m = await asr.events.get()
                    try:
                        await self.call.on_asr_event(self, m)
                    except Exception:  # noqa: BLE001 - keep listening after one bad event
                        log.exception("asr event for %s failed", self.identity)
            finally:
                pump.cancel()
                asr.close()

    async def _pump(self, asr: AsrStream):
        sent = 0
        stream = rtc.AudioStream(self.track, sample_rate=IN_SR, num_channels=1, frame_size_ms=20)
        apm = rtc.AudioProcessingModule(echo_cancellation=True) if ECHO_CANCEL else None
        self.echo = EchoMonitor()
        clock = None                                    # wall time of the next mic sample
        try:
            async for ev in stream:
                data = ev.frame.data.tobytes()
                if apm is not None:
                    data, clock = self._cancel_echo(apm, data, clock)
                asr.feed(data)
                sent += len(data) // 2
                self.sent_samples.append(sent)
                self.sent_wall.append(time.time())
                if len(self.sent_samples) > 50 * 600:            # keep ~10 minutes of mapping
                    del self.sent_samples[:25 * 600], self.sent_wall[:25 * 600]
        except Exception:  # noqa: BLE001
            log.exception("microphone pump for %s failed", self.identity)
        finally:
            await stream.aclose()

    def _cancel_echo(self, apm: rtc.AudioProcessingModule, data: bytes, clock: float | None):
        """Run one mic frame through AEC3 in 10 ms blocks, each preceded by the 10 ms of room
        playout from the same moment. The echo arrives later (network + speaker + mic); AEC3
        estimates that delay itself. AEC3 always runs, so it stays adapted, but its output
        replaces the mic audio only while playout is leaking into this mic (EchoMonitor)."""
        n = len(data) // 2
        start = time.time() - n / IN_SR                 # the frame just arrived: it ends now
        if clock is None or abs(clock - start) > 0.25:  # first frame, or a gap/stall: resync
            clock = start
        block = IN_SR // 100
        out = bytearray()
        for k in range(0, n - block + 1, block):
            ref = self.call.timeline.read(clock - self.echo.seconds, OUT_SR // 100)
            mic = data[2 * k: 2 * (k + block)]
            self.echo.observe(clock, self.call.timeline.read(clock, OUT_SR // 100), mic)
            far = rtc.AudioFrame(ref, OUT_SR, 1, OUT_SR // 100)
            apm.process_reverse_stream(far)
            near = rtc.AudioFrame(bytearray(mic), IN_SR, 1, block)
            apm.process_stream(near)
            out += near.data.tobytes()
            clock += 0.01
        return (bytes(out) if self.echo.leaking else data), clock


# ---------------------------------------------------------------------------- the call
class CallTranslator:
    def __init__(self, room: rtc.Room):
        self.room = room
        self.callers: dict[str, Caller] = {}
        self.inputs: dict[str, SpeakerInput] = {}
        self.outputs: dict[str, ListenerOutput] = {}
        self.translator = TranslateClient()
        self.timeline = PlayoutTimeline()        # echo reference: everything played into the room
        self.played_text: list[tuple[float, str, set[str]]] = []   # (time, language, words) of recent translations
        self.queues: dict[str, asyncio.Queue] = {}
        self.workers: dict[str, asyncio.Task] = {}
        self.tasks: set[asyncio.Task] = set()
        self.reconcile_lock = asyncio.Lock()     # room events arrive in bursts

    # ------------------------------------------------------------------ room state
    def humans(self) -> list[rtc.RemoteParticipant]:
        return [p for p in self.room.remote_participants.values()
                if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT]

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def reconcile(self):
        """Bring inputs/outputs in line with who is in the room and their attributes."""
        async with self.reconcile_lock:
            await self._reconcile()

    async def _reconcile(self):
        present = {p.identity: p for p in self.humans()}
        for ident in list(self.callers):
            if ident not in present:
                await self._remove(ident)
        changed = False
        for ident, p in present.items():
            caller = Caller.from_participant(p)
            if self.callers.get(ident) != caller:
                self.callers[ident] = caller
                changed = True
            if ident not in self.workers:
                self.queues[ident] = asyncio.Queue()
                self.workers[ident] = asyncio.create_task(self.utterance_worker(ident))
            if ident not in self.outputs:
                out = ListenerOutput(self, ident)
                self.outputs[ident] = out
                await out.start()
                changed = True
            for pub in p.track_publications.values():
                if pub.kind == rtc.TrackKind.KIND_AUDIO and pub.track is not None:
                    self.attach_mic(ident, pub.track)
            if ident in self.inputs:
                self.inputs[ident].start(caller.speak)       # restarts ASR if `speak` changed
        if changed:
            self.update_permissions()
            await self.status()

    def attach_mic(self, ident: str, track: rtc.Track):
        if not isinstance(track, rtc.RemoteAudioTrack) or ident not in self.callers:
            return
        cur = self.inputs.get(ident)
        if cur is not None and cur.track.sid == track.sid:
            return
        if cur is not None:
            cur.stop()
        inp = SpeakerInput(self, ident, track)
        self.inputs[ident] = inp
        inp.start(self.callers[ident].speak)

    def update_permissions(self):
        """Each translation track may only be subscribed to by the caller it is for."""
        perms = [rtc.ParticipantTrackPermission(participant_identity=ident, allow_all=False,
                                                allowed_track_sids=[out.sid])
                 for ident, out in self.outputs.items() if out.sid]
        self.room.local_participant.set_track_subscription_permissions(
            allow_all_participants=False, participant_permissions=perms)

    async def _remove(self, ident: str):
        self.callers.pop(ident, None)
        if (inp := self.inputs.pop(ident, None)) is not None:
            inp.stop()
        if (out := self.outputs.pop(ident, None)) is not None:
            await out.close()
        if (w := self.workers.pop(ident, None)) is not None:
            w.cancel()
        self.queues.pop(ident, None)
        self.update_permissions()
        await self.status()

    async def close(self):
        async with self.reconcile_lock:
            for ident in list(self.callers):
                await self._remove(ident)
        for t in list(self.tasks):
            t.cancel()
        await self.translator.close()

    # ------------------------------------------------------------------ messages to callers
    async def send(self, msg: dict):
        try:
            payload = json.dumps(msg, ensure_ascii=False).encode()
        except (TypeError, ValueError):
            log.exception("caption message %s could not be encoded", msg.get("type"))
            return
        try:
            await self.room.local_participant.publish_data(payload, reliable=True, topic=CAPTIONS_TOPIC)
        except Exception as e:  # noqa: BLE001 - a caller leaving mid-send is normal
            log.warning("caption message %s not sent: %s", msg.get("type"), e)

    async def status(self, message: str | None = None):
        await self.send({"type": "status", "callers": [c.public() for c in self.callers.values()],
                         "message": message})

    # ------------------------------------------------------------------ echo: words the room just heard
    @staticmethod
    def _words(text: str) -> set[str]:
        return {w for w in re.sub(r"[^\w\s]", " ", text.lower()).split() if w}

    def remember_played(self, lang: str, text: str):
        now = time.time()
        self.played_text = [p for p in self.played_text if now - p[0] < 30] + [(now, lang, self._words(text))]

    def repeats_played(self, lang: str, text: str) -> bool:
        """Do most of these words repeat a translation played in this language in the last 30 s?"""
        words = self._words(text)
        if not words:
            return False
        played = set().union(*(w for t, l, w in self.played_text if l == lang and time.time() - t < 30))
        return len(words & played) / len(words) >= 0.6

    # ------------------------------------------------------------------ speech
    async def on_asr_event(self, inp: SpeakerInput, m: dict):
        caller = self.callers.get(inp.identity)
        if caller is None:
            return
        kind = m.get("type")
        utt_id = f"{inp.identity}:{inp.lang}:{m.get('segment_id')}"
        if kind == "speech_start":
            await self.send({"type": "speech_start", "utt": utt_id, "speaker": caller.identity})
        elif kind == "partial":
            await self.send({"type": "transcript", "utt": utt_id, "speaker": caller.identity,
                             "lang": caller.speak, "text": m["text"], "final": False})
        elif kind == "final":
            text = m["text"].strip()
            score = inp.echo.echo_score(inp.wall_at(m["start_ms"]), inp.wall_at(m["end_ms"])) if text else 0.0
            repeats = self.repeats_played(caller.speak, text)
            echo = bool(ECHO_CANCEL and (score >= ECHO_SCORE_MAX or (score >= ECHO_SCORE_WITH_TEXT and repeats)))
            if LOG_TEXT:
                log.info("heard %s (%s, echo score %.2f%s%s): %s", utt_id, caller.speak, score,
                         ", repeats played words" if repeats else "",
                         ", dropped as echo" if echo else "", text)
            await self.send({"type": "transcript", "utt": utt_id, "speaker": caller.identity,
                             "lang": caller.speak, "text": text, "final": True, "echo": echo})
            if not text or echo:
                return
            end_ms = m["end_ms"] - (0 if m.get("forced") else SPEECH_PAD_MS)
            utt = Utterance(utt_id, caller.identity, text, inp.wall_at(end_ms), time.time())
            utt.marks["asr_decode_ms"] = m.get("decode_ms")
            await self.queues[caller.identity].put(utt)
        elif kind == "error":
            log.warning("asr error for %s: %s", inp.identity, m.get("message"))

    async def utterance_worker(self, ident: str):
        """One speaker's utterances, in order: translate, then hand to each listener's
        TTS (which queues), then move on - the next translation overlaps playback."""
        q = self.queues[ident]
        while True:
            utt: Utterance = await q.get()
            speaker = self.callers.get(ident)
            if speaker is None:
                continue
            listeners = [c for c in self.callers.values() if c.identity != ident]
            by_lang: dict[str, list[Caller]] = {}
            for c in listeners:
                if c.hear != speaker.speak:
                    by_lang.setdefault(c.hear, []).append(c)
            for lang, group in by_lang.items():
                try:
                    t0 = time.time()

                    async def on_delta(text, lang=lang):
                        if "translate_first" not in utt.marks:
                            utt.marks["translate_first"] = time.time()
                        await self.send({"type": "translation", "utt": utt.id, "speaker": ident,
                                         "lang": lang, "text": text, "final": False})

                    text, stats = await self.translator.translate(utt.text, speaker.speak, lang, on_delta)
                    utt.marks[f"translated:{lang}"] = time.time()
                    if LOG_TEXT:
                        log.info("translated %s -> %s: %s", utt.id, lang, text)
                    await self.send({"type": "translation", "utt": utt.id, "speaker": ident, "lang": lang,
                                     "text": text, "final": True, "ms": round((time.time() - t0) * 1000),
                                     "route": stats.get("route"), "pivot_text": stats.get("pivot_text")})
                    if text:
                        self.remember_played(lang, text)
                    for c in group:
                        if c.identity in self.outputs and text:
                            await self.outputs[c.identity].say(utt, text, lang, speaker.voice)
                except Exception as e:  # noqa: BLE001
                    log.exception("translation of %s failed", utt.id)
                    await self.status(f"translation failed: {e}")

    async def utterance_audio_done(self, utt: Utterance, listener: str, tts: dict):
        """Per-utterance latency, measured from the moment the speaker stopped talking."""
        first_audio = utt.marks.get(f"audio:{listener}")
        caller = self.callers.get(listener)
        lang = caller.hear if caller else None
        ms = lambda t: round((t - utt.speech_end) * 1000) if t else None  # noqa: E731
        metrics = {
            "type": "metrics", "utt": utt.id, "speaker": utt.speaker, "listener": listener,
            "route": route(self.callers[utt.speaker].speak, lang) if utt.speaker in self.callers and lang else None,
            "asr_final_ms": ms(utt.final_at),                        # includes the VAD pause
            "translation_first_ms": ms(utt.marks.get("translate_first")),
            "translation_done_ms": ms(utt.marks.get(f"translated:{lang}")),
            "audio_start_ms": ms(first_audio),                       # translated speech starts playing
            "audio_s": round(tts.get("audio_ms", 0) / 1000, 2), "tts_ttfa_ms": tts.get("ttfa_ms"),
            "vad_pause_ms": VAD_MIN_SILENCE_MS,
        }
        log.info("utterance %s -> %s: %s", utt.id, listener, json.dumps(metrics))
        await self.send(metrics)


# ---------------------------------------------------------------------------- worker
# drain_timeout: on restart, leave calls within seconds (the web app's watchdog hands
# them to the new worker) instead of lingering until Docker kills the process
server = AgentServer(num_idle_processes=int(os.environ.get("S2S_IDLE_PROCESSES", "2")), host="127.0.0.1",
                     drain_timeout=int(os.environ.get("S2S_DRAIN_TIMEOUT_S", "5")))


@server.rtc_session()
async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    call = CallTranslator(ctx.room)
    log.info("joined room %s", ctx.room.name)

    def later(coro):
        call.spawn(coro)

    @ctx.room.on("participant_connected")
    def _(p):
        later(call.reconcile())

    @ctx.room.on("participant_disconnected")
    def _(p):
        later(call.reconcile())

    @ctx.room.on("participant_attributes_changed")
    def _(changed, p):
        later(call.reconcile())

    @ctx.room.on("track_subscribed")
    def _(track, pub, p):
        if track.kind == rtc.TrackKind.KIND_AUDIO and p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            later(call.reconcile())

    ctx.add_shutdown_callback(call.close)
    await call.reconcile()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    cli.run_app(server)
