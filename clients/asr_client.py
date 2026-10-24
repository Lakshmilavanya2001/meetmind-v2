"""ASR NIM client (IndicConformer on NVIDIA Riva, HTTP API): transcribe a file, or benchmark.

    .venv/bin/python clients/asr_client.py testdata/sample_hindi.wav --language hi-IN
    .venv/bin/python clients/asr_client.py testdata/sample_hindi.wav --language hi-IN --bench --concurrency 1 4 8 16

One request per utterance of at most 16 s (the call agent cuts live audio into
utterances with VAD first). Languages: ml-IN, hi-IN, kn-IN, te-IN, ta-IN. For longer
files use docs/examples/asr_transcribe.py, which cuts them at pauses.
"""
import argparse
import asyncio
import statistics
import sys
import time
import wave
from pathlib import Path

import httpx


async def transcribe(client, url, audio: bytes, language: str):
    t0 = time.perf_counter()
    r = await client.post(f"{url}/v1/audio/transcriptions", data={"language": language},
                          files={"file": ("audio.wav", audio, "audio/wav")})
    r.raise_for_status()
    return r.json(), time.perf_counter() - t0


async def main(a):
    with wave.open(a.file) as w:
        if w.getnframes() / w.getframerate() > 15.5:
            sys.exit("longer than 16 s: one request would crash the ASR server; use docs/examples/asr_transcribe.py")
    audio = Path(a.file).read_bytes()
    async with httpx.AsyncClient(timeout=120) as client:
        res, secs = await transcribe(client, a.url, audio, a.language)
        print(f"{res['text'].strip()}\n\n[{a.language}] {secs * 1000:.0f} ms")
        if not a.bench:
            return
        for n in a.concurrency:
            t0 = time.perf_counter()
            out = await asyncio.gather(*[transcribe(client, a.url, audio, a.language) for _ in range(n)])
            wall = time.perf_counter() - t0
            lat = [s * 1000 for _, s in out]
            print(f"concurrency {n:3d}: p50 {statistics.median(lat):5.0f} ms max {max(lat):5.0f} ms | "
                  f"{n / wall:5.1f} files/s")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("file")
    p.add_argument("--language", default="hi-IN", help="ml-IN, hi-IN, kn-IN, te-IN or ta-IN")
    p.add_argument("--url", default="http://localhost:8001")
    p.add_argument("--bench", action="store_true")
    p.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8, 16])
    asyncio.run(main(p.parse_args()))
