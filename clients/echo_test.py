"""Feedback-loop test: two callers whose microphones also hear the room.

Both callers sit "in the same room": every translated sentence either of them receives
is mixed back into BOTH microphones, delayed and attenuated, as if it came out of a
speaker next to the mics (and as if the browsers' own echo cancellation did nothing).
Caller A says one Hindi sentence; then nobody speaks. Without echo protection the
translation is heard by the mics, transcribed, translated and played again, forever.

    docker run --rm --network host -v $PWD/clients:/clients -v $PWD/testdata:/td \\
        indic-s2s:0.1 python /clients/echo_test.py --seconds 45
"""
import argparse
import asyncio
import bisect
import json
import time
import urllib.request
import wave

import numpy as np
from livekit import rtc

SR = 48000
FRAME = SR // 50                           # 20 ms


def token(api, name, room, speak, hear):
    body = json.dumps({"name": name, "room": room, "speak": speak, "hear": hear}).encode()
    req = urllib.request.Request(f"{api}/api/token", body, {"content-type": "application/json"})
    return json.load(urllib.request.urlopen(req))


class Room:
    """What a microphone in the room hears from the speakers: received translations by time."""

    def __init__(self):
        self.starts, self.chunks = [], []

    def add(self, t, pcm):
        i = bisect.bisect(self.starts, t)
        self.starts.insert(i, t)
        self.chunks.insert(i, pcm.astype(np.float32))

    def read(self, t0, n):
        out = np.zeros(n, np.float32)
        j = bisect.bisect_left(self.starts, t0 - 0.1)
        while j < len(self.starts) and self.starts[j] < t0 + n / SR:
            x, off = self.chunks[j], int(round((self.starts[j] - t0) * SR))
            a, b = max(0, off), min(n, off + len(x))
            if a < b:
                out[a:b] += x[a - off: b - off]
            j += 1
        return out


class Caller:
    def __init__(self, name, speak, hear, room_audio):
        self.name, self.speak, self.hear, self.room_audio = name, speak, hear, room_audio
        self.room = rtc.Room()
        self.source = rtc.AudioSource(SR, 1)
        self.messages = []
        self.speech = np.zeros(0, np.float32)

    async def join(self, url, api, room):
        info = token(api, self.name, room, self.speak, self.hear)
        self.identity = info["identity"]

        @self.room.on("data_received")
        def _(pkt):
            if pkt.topic == "captions":
                self.messages.append((time.time(), json.loads(pkt.data)))

        @self.room.on("track_subscribed")
        def _(track, pub, p):
            if track.kind == rtc.TrackKind.KIND_AUDIO and p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                asyncio.ensure_future(self._hear(track))

        await self.room.connect(url, info["token"])
        await self.room.local_participant.publish_track(
            rtc.LocalAudioTrack.create_audio_track("mic", self.source),
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    async def _hear(self, track):
        async for ev in rtc.AudioStream(track, sample_rate=SR, num_channels=1):
            self.room_audio.add(time.time(), np.frombuffer(ev.frame.data, np.int16))

    async def mic(self, seconds, delay, gain):
        """Real-time microphone: own speech (if any) plus the room's speakers."""
        t0 = time.perf_counter()
        wall0 = time.time()
        for k in range(int(seconds * 50)):
            seg = self.speech[k * FRAME:(k + 1) * FRAME]
            x = np.zeros(FRAME, np.float32)
            x[:len(seg)] = seg
            x += gain * self.room_audio.read(wall0 + k * 0.02 - delay, FRAME)
            pcm = np.clip(x, -32768, 32767).astype(np.int16)
            await self.source.capture_frame(rtc.AudioFrame(pcm.tobytes(), SR, 1, FRAME))
            await asyncio.sleep(max(0, t0 + (k + 1) * 0.02 - time.perf_counter()))


def load_48k(path):
    with wave.open(path) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
        sr = w.getframerate()
    return np.interp(np.arange(int(len(x) * SR / sr)) * sr / SR, np.arange(len(x)), x).astype(np.float32)


async def main(a):
    room_audio = Room()
    room = f"echo-{int(time.time())}"
    A = Caller("Asha", "hi", "hi", room_audio)
    B = Caller("Bala", "ta", "ta", room_audio)
    await A.join(a.url, a.api, room)
    await B.join(a.url, a.api, room)
    deadline = time.time() + 20
    while time.time() < deadline and not any(len(m.get("callers", [])) == 2 for _, m in A.messages if m["type"] == "status"):
        await asyncio.sleep(0.2)
    A.speech = np.concatenate([np.zeros(SR * 2, np.float32), load_48k(a.hindi)])   # speaks once, at 2 s
    if a.b_speaks_at is not None:          # double talk: Bala talks while the room is playing
        B.speech = np.concatenate([np.zeros(int(SR * a.b_speaks_at), np.float32), load_48k(a.tamil)])
    t0 = time.time()
    await asyncio.gather(A.mic(a.seconds, a.delay, a.gain), B.mic(a.seconds, a.delay, a.gain))
    finals = sorted(((ts, m) for c in (A, B) for ts, m in c.messages
                     if m["type"] == "transcript" and m["final"] and m["text"]),
                    key=lambda x: x[0])
    seen = set()
    print(f"\nroom {room}: room echo gain {a.gain}, delay {int(a.delay * 1000)} ms, {a.seconds} s")
    for ts, m in finals:
        if m["utt"] in seen:
            continue
        seen.add(m["utt"])
        who = "Asha" if m["speaker"] == A.identity else "Bala"
        flag = "  [dropped as echo]" if m.get("echo") else ""
        print(f"  {ts - t0:5.1f}s  {who} ({m['lang']}): {m['text'][:90]}{flag}")
    heard = {c.name: sum(1 for _, m in c.messages if m["type"] == "translation" and m["final"]
                         and m["speaker"] != c.identity) for c in (A, B)}
    print(f"translations received: {heard}")
    real = {u for ts, m in finals if not m.get("echo") for u in [m["utt"]]}
    by_a = sum(1 for u in real if u.startswith(A.identity))
    by_b = sum(1 for u in real if u.startswith(B.identity))
    # Asha's sentence is one utterance; Bala's two-sentence clip (if spoken) is at most two.
    # Anything beyond that came out of a speaker and was translated again.
    bala_max = 2 if a.b_speaks_at is not None else 0
    verdict = "no loop" if by_a <= 1 and by_b <= bala_max else "LOOP"
    print(f"translated utterances: Asha {by_a} (max 1), Bala {by_b} (max {bala_max})  ->  {verdict}")
    await A.room.disconnect()
    await B.room.disconnect()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:7880")
    p.add_argument("--api", default="http://127.0.0.1:8090")
    p.add_argument("--hindi", default="/td/sample_hindi.wav")
    p.add_argument("--seconds", type=float, default=45)
    p.add_argument("--delay", type=float, default=0.2, help="speaker-to-mic path delay")
    p.add_argument("--gain", type=float, default=0.5, help="how loud the room's speakers are in the mics")
    p.add_argument("--tamil", default="/td/tamil_16k.wav")
    p.add_argument("--b-speaks-at", type=float, default=None,
                   help="Bala also speaks (Tamil clip) at this second: double talk over the playout")
    asyncio.run(main(p.parse_args()))
