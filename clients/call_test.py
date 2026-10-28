"""End-to-end test of a translated call with two simulated callers.

Each caller joins the room through the demo's token API, publishes a WAV file as
its microphone (in real time), records the translated audio the agent publishes
for it, and collects the captions/metrics data messages.

    docker run --rm --network host -v $PWD/clients:/clients -v $PWD/testdata:/td \\
        indic-s2s:0.1 python /clients/call_test.py

Scenario: A (speaks Hindi, hears Hindi) says the Hindi clip; B (speaks Tamil,
hears Tamil) hears it in Tamil. Then B says a Tamil clip and A hears Hindi. Then
B switches what they hear to English mid-call and A speaks again.
"""
import argparse
import asyncio
import json
import time
import urllib.error
import urllib.request
import wave

import numpy as np
from livekit import rtc


def read_wav(path):
    with wave.open(path) as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, "16-bit mono WAV expected"
        return np.frombuffer(w.readframes(w.getnframes()), np.int16), w.getframerate()


def write_wav(path, pcm, sr):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.astype(np.int16).tobytes())


def token(api, name, room, speak, hear, voice, noise="standard"):
    """A call token for `name` in `room`. With accounts on (the default), each test caller signs up
    (the room's first caller creates a test organisation and a meeting; the others join it with
    its code); the open demo's /api/token is used only where it's enabled."""
    body = json.dumps({"name": name, "room": room, "speak": speak, "hear": hear, "voice": voice, "noise": noise}).encode()
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(f"{api}/api/token", body, {"content-type": "application/json"})))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    return _account_token(api, name, room, speak, hear, voice, noise)


_rooms: dict = {}          # room -> {"org_code", "meeting"}


def _account_token(api, name, room, speak, hear, voice, noise):
    import http.cookiejar
    import secrets
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(path, payload):
        req = urllib.request.Request(f"{api}{path}", json.dumps(payload).encode(), {"content-type": "application/json"})
        return json.load(opener.open(req))

    email = f"{name.lower()}-{secrets.token_hex(4)}@calltest.test"
    info = _rooms.get(room)
    if info is None:
        call("/api/auth/signup", {"name": name, "email": email, "password": secrets.token_urlsafe(12), "organization": f"Call test {room}"})
        org_code = json.load(opener.open(f"{api}/api/me"))["organization"]["joinCode"]
        meeting = call("/api/meetings", {"title": room})["meeting"]["joinCode"]
        _rooms[room] = info = {"org_code": org_code, "meeting": meeting}
    else:
        call("/api/auth/signup", {"name": name, "email": email, "password": secrets.token_urlsafe(12), "join_code": info["org_code"]})
    return call(f"/api/meetings/{info['meeting']}/join", {"speak": speak, "hear": hear, "voice": voice, "noise": noise})


class Caller:
    def __init__(self, name, speak, hear, voice):
        self.name, self.speak, self.hear, self.voice = name, speak, hear, voice
        self.room = rtc.Room()
        self.messages = []
        self.received = []                 # (wall time, int16 chunk) of translated audio
        self.source = rtc.AudioSource(48000, 1)
        self.subscribed = []

    async def join(self, url, api, room):
        info = token(api, self.name, room, self.speak, self.hear, self.voice)
        self.identity = info["identity"]

        @self.room.on("data_received")
        def _(pkt: rtc.DataPacket):
            if pkt.topic == "captions":
                self.messages.append((time.time(), json.loads(pkt.data)))

        @self.room.on("track_subscribed")
        def _(track, pub, participant):
            self.subscribed.append((participant.identity, track.name, participant.kind))
            if track.kind == rtc.TrackKind.KIND_AUDIO and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                asyncio.ensure_future(self._record(track))

        await self.room.connect(url, info["token"])
        track = rtc.LocalAudioTrack.create_audio_track("mic", self.source)
        await self.room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    async def _record(self, track):
        async for ev in rtc.AudioStream(track, sample_rate=48000, num_channels=1):
            pcm = np.frombuffer(ev.frame.data, np.int16).copy()
            # an idle WebRTC track still decodes to low-level noise: flag speech-level frames
            loud = np.sqrt(np.mean(pcm.astype(np.float32) ** 2)) > 300
            self.received.append((time.time(), pcm, loud))

    async def say(self, pcm, sr, then_silence_s=2.0):
        """Play a clip into the mic in real time (20 ms frames), then silence."""
        step = sr // 50
        audio = np.concatenate([pcm, np.zeros(int(sr * then_silence_s), np.int16)])
        t0 = time.perf_counter()
        for i in range(0, len(audio) - step + 1, step):
            # the mic source is 48 kHz; upsample by repetition-free linear interpolation
            chunk = audio[i: i + step].astype(np.float32)
            up = np.interp(np.arange(960) * (step / 960), np.arange(step), chunk).astype(np.int16)
            await self.source.capture_frame(rtc.AudioFrame(up.tobytes(), 48000, 1, 960))
            await asyncio.sleep(max(0, t0 + (i + step) / sr - time.perf_counter()))
        return t0

    def audio_since(self, t):
        """Everything from the first to the last speech-level frame after t."""
        frames = [(ts, c, loud) for ts, c, loud in self.received if ts >= t]
        loud = [i for i, f in enumerate(frames) if f[2]]
        if not loud:
            return np.zeros(0, np.int16)
        return np.concatenate([c for _, c, _ in frames[loud[0]: loud[-1] + 1]])

    def first_speech_since(self, t):
        return next((ts for ts, _, loud in self.received if ts >= t and loud), None)

    def msgs(self, kind, since=0.0):
        return [m for ts, m in self.messages if m.get("type") == kind and ts >= since]


async def wait_for(pred, timeout, step=0.1):
    t = time.time() + timeout
    while time.time() < t:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


async def turn(label, speaker, listener, clip, out_path):
    pcm, sr = clip
    t_start = time.time()
    await speaker.say(pcm, sr)
    speech_end = t_start + len(pcm) / sr
    # wait until every final utterance has been voiced to the listener
    def done():
        finals = [m for m in speaker.msgs("transcript", t_start) if m["final"] and m["text"]]
        voiced = [m for m in listener.msgs("metrics", t_start) if m["listener"] == listener.identity]
        return finals and len(voiced) >= len(finals)
    ok = await wait_for(done, timeout=40)
    await asyncio.sleep(0.5)
    audio = listener.audio_since(t_start)
    write_wav(out_path, audio, 48000)
    first = listener.first_speech_since(t_start)
    transcripts = [m["text"] for m in speaker.msgs("transcript", t_start) if m["final"]]
    translations = [(m["lang"], m["text"] + (f"  [via English: {m['pivot_text']}]" if m.get("pivot_text") else ""))
                    for m in listener.msgs("translation", t_start) if m["final"]]
    metrics = [m for m in listener.msgs("metrics", t_start) if m["listener"] == listener.identity]
    print(f"\n=== {label}")
    print(f"  {speaker.name} said ({speaker.speak}): {' | '.join(transcripts)}")
    for lang, text in translations:
        print(f"  {listener.name} got ({lang}): {text}")
    print(f"  translated speech received: {len(audio) / 48000:.1f} s -> {out_path}")
    if first:
        print(f"  measured at the listener: speaker stopped -> first translated audio {(first - speech_end) * 1000:.0f} ms")
    for m in metrics:
        print(f"  agent timings (ms after speech end): final transcript {m['asr_final_ms']}, "
              f"translation {m['translation_done_ms']}, audio starts {m['audio_start_ms']} "
              f"({m['audio_s']} s of audio)")
    return ok and len(audio) > 0


async def main(a):
    room = f"test-{int(time.time())}"
    A = Caller("Asha", "hi", "hi", "female")
    B = Caller("Bala", "ta", "ta", "male")
    await A.join(a.url, a.api, room)
    await B.join(a.url, a.api, room)
    assert await wait_for(lambda: any(len(m.get("callers", [])) == 2 for m in A.msgs("status")), 20), \
        "agent did not report both callers"
    await asyncio.sleep(1.0)
    print("A subscribed to:", A.subscribed)
    print("B subscribed to:", B.subscribed)
    agent_tracks_b = [t for i, t, k in B.subscribed if k == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT]
    assert agent_tracks_b == [f"translation-{B.identity}"], f"B can hear someone else's translation: {agent_tracks_b}"

    hindi = read_wav(a.hindi)
    tamil = read_wav(a.tamil)
    results = [await turn("A (Hindi) -> B hears Tamil", A, B, hindi, "/td/out/call_A_to_B_ta.wav"),
               await turn("B (Tamil) -> A hears Hindi", B, A, tamil, "/td/out/call_B_to_A_hi.wav")]

    # B now wants English; nothing else changes
    await B.room.local_participant.set_attributes({"speak": "ta", "hear": "en", "voice": "male"})
    await asyncio.sleep(1.0)
    results.append(await turn("A (Hindi) -> B now hears English", A, B, hindi, "/td/out/call_A_to_B_en.wav"))

    await A.room.disconnect()
    await B.room.disconnect()
    print("\nRESULT:", "PASS" if all(results) else "FAIL", results)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:7880")
    p.add_argument("--api", default="http://127.0.0.1:8090")
    p.add_argument("--hindi", default="/td/sample_hindi.wav")
    p.add_argument("--tamil", default="/td/tamil_16k.wav")
    asyncio.run(main(p.parse_args()))
