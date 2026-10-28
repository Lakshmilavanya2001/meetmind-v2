"""Studio noise removal (NVIDIA BNR before speech detection) vs Standard, in a real call.

A speaks English into a noisy mic: 5 s of background noise alone, the sentence with the noise
under it, then 5 s of noise alone. B hears Hindi. For each noise mode, we record what A's
utterances were transcribed as and how many translations B got. Noise alone should produce
no utterances; the sentence should come through whole.

    docker run --rm --network host --env-file .env -v $PWD/clients:/clients -v $PWD/testdata:/td \\
        meetmind-v2-s2s python /clients/noise_test.py --url ws://127.0.0.1:7890 --api http://127.0.0.1:8091
"""
import argparse
import asyncio
import json
import time
import urllib.request

import numpy as np

import call_test
from call_test import Caller, read_wav


def token(api, name, room, speak, hear, voice, noise):
    body = json.dumps({"name": name, "room": room, "speak": speak, "hear": hear, "voice": voice, "noise": noise}).encode()
    return json.load(urllib.request.urlopen(urllib.request.Request(f"{api}/api/token", body, {"content-type": "application/json"})))


def clip(sample: str):
    """noise alone (5 s) + speech in noise + noise alone (5 s), 16 kHz int16."""
    speech, sr = read_wav(f"/td/{sample}.wav")
    clean, _ = read_wav("/td/clean.wav")
    noise = speech.astype(np.int32) - clean.astype(np.int32)        # the noise track on its own
    loop = np.resize(noise, 5 * sr).astype(np.int16)
    out = np.concatenate([loop, speech, loop])
    if sr != 16000:
        idx = np.arange(int(len(out) * 16000 / sr)) * sr / 16000
        out = np.interp(idx, np.arange(len(out)), out).astype(np.int16)
    return out, 16000


async def run(url, api, sample, mode):
    room = f"noise-{mode}-{int(time.time())}"
    call_test.token = lambda api_, name, room_, speak, hear, voice: token(api_, name, room_, speak, hear, voice, mode if name == "Asha" else "standard")
    a, b = Caller("Asha", "en", "en", "female"), Caller("Ben", "hi", "hi", "male")
    await a.join(url, api, room)
    await b.join(url, api, room)
    await asyncio.sleep(6)                                            # the agent joins
    pcm, sr = clip(sample)
    t0 = time.time()
    await a.say(pcm, sr, then_silence_s=4)
    await asyncio.sleep(3)
    finals = [m for m in b.msgs("transcript", t0) if m.get("final") and m.get("speaker") == a.identity]
    translations = [m for m in b.msgs("translation", t0) if m.get("speaker") == a.identity and m.get("final", True)]
    for c in (a, b):
        await c.room.disconnect()
    return finals, translations


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:7880")
    p.add_argument("--api", default="http://127.0.0.1:8090")
    p.add_argument("--samples", default="office,street,cafe")
    args = p.parse_args()
    for sample in args.samples.split(","):
        for mode in ("standard", "studio"):
            finals, translations = await run(args.url, args.api, sample, mode)
            texts = [f["text"] for f in finals]
            print(f"[{sample:6} {mode:8}] {len(finals)} utterance(s): {json.dumps(texts, ensure_ascii=False)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
