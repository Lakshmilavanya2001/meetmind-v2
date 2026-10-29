"""Studio noise removal on what listeners actually hear, in a real call.

Ben speaks English into a noisy mic with Studio (NVIDIA BNR) on: 5 s of background noise alone, the
sentence with the noise under it, then 5 s of noise alone. Asha also speaks English, so she hears Ben's
own voice. We record, on Asha's side, Ben's raw microphone and the agent's `clean-<Ben>` track, and
compare the level of the noise-only parts (should drop a lot) with the speech part (should stay).

    docker run --rm --network host -v $PWD/clients:/clients -v $PWD/testdata:/td \\
        indic-s2s:0.1 python /clients/clean_voice_test.py --url ws://127.0.0.1:7890 --api http://127.0.0.1:8091 --noise cafe
"""
import argparse
import asyncio
import time

import numpy as np
from livekit import rtc

from call_test import Caller
from noise_test import clip


def db(x: np.ndarray) -> float:
    return 20 * np.log10(max(1e-9, float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))) / 32768)


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:7880")
    p.add_argument("--api", default="http://127.0.0.1:8090")
    p.add_argument("--noise", default="cafe", help="cafe | office | street (testdata/<name>.wav)")
    args = p.parse_args()
    room = f"clean-{int(time.time())}"
    asha = Caller("Asha", "en", "en", "female")
    ben = Caller("Ben", "en", "en", "male")
    tracks: dict[str, list] = {"raw": [], "clean": []}

    async def record(kind, track):
        async for ev in rtc.AudioStream(track, sample_rate=48000, num_channels=1):
            tracks[kind].append((time.time(), np.frombuffer(ev.frame.data, np.int16).copy()))

    await ben.join(args.url, args.api, room)
    await ben.room.local_participant.set_attributes({"speak": "en", "hear": "en", "voice": "male", "noise": "studio"})
    await asha.join(args.url, args.api, room)

    @asha.room.on("track_subscribed")
    def _(track, pub, participant):
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        if participant.identity == ben.identity:
            asyncio.ensure_future(record("raw", track))
        elif track.name == f"clean-{ben.identity}":
            asyncio.ensure_future(record("clean", track))
    for pub in [p for part in asha.room.remote_participants.values() for p in part.track_publications.values()]:
        if pub.track is not None and pub.kind == rtc.TrackKind.KIND_AUDIO:
            owner = next(part for part in asha.room.remote_participants.values() if pub.sid in part.track_publications)
            if owner.identity == ben.identity:
                asyncio.ensure_future(record("raw", pub.track))
            elif pub.name == f"clean-{ben.identity}":
                asyncio.ensure_future(record("clean", pub.track))

    await asyncio.sleep(8)
    pcm, sr = clip(args.noise)
    t0 = time.time()
    await ben.say(pcm, sr, then_silence_s=1.0)
    t1 = time.time()
    await asyncio.sleep(1.5)
    for c in (asha, ben):
        await c.room.disconnect()

    total = len(pcm) / sr
    print(f"noise: {args.noise}, clip {total:.1f} s (5 s noise, speech in noise, 5 s noise)")
    ok = True
    res = {}
    for kind in ("raw", "clean"):
        frames = [(ts, c) for ts, c in tracks[kind] if t0 <= ts <= t1 + 1.0]
        if not frames:
            print(f"  {kind}: no audio received")
            ok = False
            continue
        audio = np.concatenate([c for _, c in frames])
        # align on the clip: frames arrive in real time from t0 (allow ~0.3 s of network/BNR delay)
        lag = int(0.3 * 48000)
        a = audio[lag:]
        noise_only = np.concatenate([a[int(0.5 * 48000): int(4.5 * 48000)], a[int((total - 4.5) * 48000): int((total - 0.5) * 48000)]])
        speech = a[int(5.5 * 48000): int((total - 5.5) * 48000)]
        res[kind] = (db(noise_only), db(speech))
        print(f"  {kind:5s}: noise-only {res[kind][0]:6.1f} dBFS, speech part {res[kind][1]:6.1f} dBFS")
    if len(res) == 2:
        noise_drop = res["raw"][0] - res["clean"][0]
        speech_drop = res["raw"][1] - res["clean"][1]
        print(f"  noise reduced by {noise_drop:.1f} dB; speech part changed by {-speech_drop:+.1f} dB")
        ok = ok and noise_drop >= 10 and speech_drop < noise_drop - 6
    print("RESULT:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    asyncio.run(main())
