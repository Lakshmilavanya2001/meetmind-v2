"""Synthesize speech with the TTS service three ways: a WAV file over HTTP,
streamed HTTP, and streamed over WebSocket with timing.

    pip install requests websockets
    python tts.py "வணக்கம், எப்படி இருக்கிறீர்கள்?" --language ta --out hello_ta

Writes <out>_http.wav, <out>_http_stream.wav and <out>_ws.wav.
"""
import argparse
import asyncio
import json
import time
import wave

import requests
import websockets

TTS = "http://localhost:8003"
TTS_WS = "ws://localhost:8003/v1/stream"


def save_pcm(path, pcm: bytes, sample_rate: int):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)               # 16-bit
        w.setframerate(sample_rate)
        w.writeframes(pcm)


def http_wav(a):
    """Simplest: one request, one complete WAV back."""
    body = {"text": a.text, "language": a.language, "gender": a.gender, "format": "wav"}
    r = requests.post(f"{TTS}/v1/tts", json=body, timeout=120)
    r.raise_for_status()
    with open(f"{a.out}_http.wav", "wb") as f:
        f.write(r.content)
    h = r.headers
    print(f"[http]   {h['X-Audio-Seconds']} s of audio by {h['X-Speaker']} in {h['X-Latency-Ms']} ms "
          f"-> {a.out}_http.wav")


def http_stream(a):
    """Chunked HTTP: raw 16-bit PCM arrives while it is generated."""
    body = {"text": a.text, "language": a.language, "gender": a.gender,
            "format": "pcm_s16le", "sample_rate": 16000, "stream": True}
    t0 = time.perf_counter()
    first = None
    pcm = b""
    with requests.post(f"{TTS}/v1/tts", json=body, stream=True, timeout=120) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=None):
            first = first or time.perf_counter()
            pcm += chunk                # in a player, write each chunk to the audio device here
    save_pcm(f"{a.out}_http_stream.wav", pcm, 16000)
    print(f"[stream] first audio after {(first - t0) * 1000:.0f} ms, {len(pcm) / 32000:.2f} s total "
          f"-> {a.out}_http_stream.wav")


async def ws_stream(a):
    """WebSocket: keep one connection open and send many requests; each plays in order.
    {"type": "cancel"} stops the current one and drops the queue (barge-in)."""
    async with websockets.connect(TTS_WS, max_size=None) as ws:
        await ws.recv()                                        # {"type": "ready", ...}
        await ws.send(json.dumps({"type": "synthesize", "id": "u1", "text": a.text, "language": a.language,
                                  "gender": a.gender, "sample_rate": 24000, "format": "pcm_s16le"}))
        t0 = time.perf_counter()
        pcm, first = b"", None
        async for msg in ws:
            if isinstance(msg, bytes):                         # audio for the request announced by "start"
                first = first or time.perf_counter()
                pcm += msg
                continue
            m = json.loads(msg)
            if m["type"] == "start":
                print(f"[ws]     speaker {m['speaker']}, {m['sample_rate']} Hz {m['encoding']}")
            elif m["type"] == "done":
                print(f"[ws]     first audio after {(first - t0) * 1000:.0f} ms, {m['audio_ms'] / 1000:.2f} s audio, "
                      f"real-time factor {m['rtf']} -> {a.out}_ws.wav")
                break
            elif m["type"] in ("error", "cancelled"):
                print(m)
                break
    save_pcm(f"{a.out}_ws.wav", pcm, 24000)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("text")
    p.add_argument("--language", default="hi", help="picks the default voice for that language")
    p.add_argument("--gender", default="female", choices=["female", "male"])
    p.add_argument("--out", default="tts_out")
    a = p.parse_args()
    http_wav(a)
    http_stream(a)
    asyncio.run(ws_stream(a))
