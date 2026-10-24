"""TTS WebSocket client: save streamed audio, or benchmark concurrent streams.

    .venv/bin/python clients/tts_stream.py --text "வணக்கம்" --language ta --out ta.wav
    .venv/bin/python clients/tts_stream.py --bench --concurrency 1 2 4 8

The benchmark simulates a listener: playback starts when the first audio arrives
(plus --jitter-ms of buffering) and a "stall" is any time the next chunk arrives
after the audio already received has finished playing.
"""
import argparse
import asyncio
import json
import statistics
import time

import numpy as np
import soundfile as sf
import websockets

BENCH_TEXTS = {
    "hi": "नमस्ते, आज हम गणित की कक्षा शुरू करेंगे। कृपया अपनी पुस्तकें खोलिए और पहला पाठ पढ़िए।",
    "ta": "வணக்கம், இன்று நாம் கணித வகுப்பைத் தொடங்குகிறோம், தயவுசெய்து உங்கள் புத்தகங்களைத் திறந்து முதல் பாடத்தைப் படியுங்கள்.",
    "te": "నమస్తే, ఈ రోజు మనం గణిత తరగతిని ప్రారంభిస్తాము, దయచేసి మీ పుస్తకాలను తెరిచి మొదటి పాఠాన్ని చదవండి.",
    "bn": "নমস্কার, আজ আমরা গণিতের ক্লাস শুরু করব, অনুগ্রহ করে আপনার বই খুলুন এবং প্রথম পাঠটি পড়ুন।",
}


async def synth(url, text, language, sample_rate=24000, jitter_ms=0.0, **kw):
    async with websockets.connect(url, max_size=None) as ws:
        await ws.recv()                                   # ready
        t0 = time.perf_counter()
        await ws.send(json.dumps({"type": "synthesize", "id": "x", "text": text, "language": language,
                                  "sample_rate": sample_rate, **kw}))
        chunks, arrivals, done = [], [], None
        async for msg in ws:
            if isinstance(msg, bytes):
                arrivals.append(time.perf_counter() - t0)
                chunks.append(np.frombuffer(msg, "<i2"))
                continue
            m = json.loads(msg)
            if m["type"] == "done":
                done = m
                break
            if m["type"] == "error":
                raise RuntimeError(m)
    audio = np.concatenate(chunks) if chunks else np.zeros(0, np.int16)
    # playback simulation
    play_start = arrivals[0] + jitter_ms / 1000 if arrivals else 0
    played_until, stall = play_start, 0.0
    for t, c in zip(arrivals, chunks):
        if t > played_until:
            stall += t - played_until
            played_until = t
        played_until += len(c) / sample_rate
    return audio, {"ttfa_ms": arrivals[0] * 1000 if arrivals else None, "stall_ms": stall * 1000,
                   "audio_s": len(audio) / sample_rate, "server": done}


async def bench(a):
    for conc in a.concurrency:
        langs = list(BENCH_TEXTS)
        jobs = [synth(a.url, BENCH_TEXTS[langs[i % len(langs)]], langs[i % len(langs)], jitter_ms=a.jitter_ms)
                for i in range(conc)]
        t0 = time.perf_counter()
        res = await asyncio.gather(*jobs)
        wall = time.perf_counter() - t0
        ttfa = [r[1]["ttfa_ms"] for r in res]
        stall = [r[1]["stall_ms"] for r in res]
        rtf = [r[1]["server"]["rtf"] for r in res]
        audio = sum(r[1]["audio_s"] for r in res)
        print(f"concurrency {conc:3d}: TTFA p50 {statistics.median(ttfa):6.0f} ms  max {max(ttfa):6.0f} ms | "
              f"RTF p50 {statistics.median(rtf):.2f} max {max(rtf):.2f} | stalls>0: {sum(s > 0 for s in stall)}/{conc}, "
              f"max stall {max(stall):5.0f} ms | {audio / wall:5.1f} s audio per s")


async def main(a):
    if a.bench:
        await bench(a)
        return
    kw = {k: v for k, v in dict(speaker=a.speaker, gender=a.gender, style=a.style).items() if v}
    audio, st = await synth(a.url, a.text, a.language, a.sample_rate, **kw)
    sf.write(a.out, audio, a.sample_rate)
    print(json.dumps(st, ensure_ascii=False, indent=1))
    print("wrote", a.out)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://localhost:8003/v1/stream")
    p.add_argument("--text", default=BENCH_TEXTS["hi"])
    p.add_argument("--language", default="hi")
    p.add_argument("--speaker")
    p.add_argument("--gender")
    p.add_argument("--style")
    p.add_argument("--sample-rate", type=int, default=24000)
    p.add_argument("--out", default="tts_out.wav")
    p.add_argument("--bench", action="store_true")
    p.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4, 8])
    p.add_argument("--jitter-ms", type=float, default=0.0)
    asyncio.run(main(p.parse_args()))
