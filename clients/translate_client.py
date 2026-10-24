"""Translate service client (the NIM's OpenAI-compatible API): stream one translation,
or benchmark concurrency.

    .venv/bin/python clients/translate_client.py --text "नमस्ते, आप कैसे हैं?" --tgt ta
    .venv/bin/python clients/translate_client.py --bench --concurrency 1 8 32 64

Requests follow sarvam-translate's model card: the target language in a system message
("Translate the text below to Tamil."), the text as the user message, greedy decoding.
The model translates English <-> Indian languages; between two Indian languages it goes
through English (--route pivot does the two calls).
"""
import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "s2s"))
from app.languages import PROMPT_NAME  # noqa: E402  (ISO code -> the prompt's language name)

SENTENCES = [                     # the benchmark targets English (one call each)
    ("hi", "नमस्ते, आज हम गणित की कक्षा शुरू करेंगे, कृपया अपनी पुस्तकें खोलिए।"),
    ("ta", "நாளை காலை பத்து மணிக்கு மருத்துவரைப் பார்க்க வேண்டும்."),
    ("te", "మీరు ఈ రోజు సాయంత్రం ఆఫీసుకు వస్తారా?"),
    ("bn", "আমার ট্রেন এক ঘণ্টা দেরিতে চলছে, আমি একটু পরে পৌঁছাব।"),
    ("mr", "मला उद्या बँकेत जायचे आहे, तुम्ही माझ्यासोबत याल का?"),
    ("gu", "તમારો ઓર્ડર આવતીકાલે સાંજ સુધીમાં પહોંચી જશે."),
]
TARGETS = ["en"]


async def stream(client, url, text, tgt, on_delta=None):
    """One streamed translation. Returns (translation, first-text seconds, total seconds)."""
    body = {"model": "sarvam-translate", "stream": True, "temperature": 0,
            "max_tokens": min(1024, 2 * len(text) + 32),
            "messages": [{"role": "system", "content": f"Translate the text below to {PROMPT_NAME.get(tgt, tgt)}."},
                         {"role": "user", "content": text}]}
    t0 = time.perf_counter()
    first, parts = None, []
    async with client.stream("POST", f"{url}/chat/completions", json=body) as r:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {(await r.aread()).decode()[:300]}")
        async for line in r.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            delta = json.loads(line[6:])["choices"][0]["delta"].get("content")
            if delta:
                first = first or time.perf_counter()
                parts.append(delta)
                if on_delta:
                    on_delta(delta)
    return "".join(parts).strip(), (first or time.perf_counter()) - t0, time.perf_counter() - t0


async def single(a):
    async with httpx.AsyncClient(timeout=60) as client:
        text = a.text
        if a.route == "pivot" and a.tgt != "en":           # source -> English -> target
            text, _, pivot_s = await stream(client, a.url, text, "en")
            print(f"English pivot ({pivot_s * 1000:.0f} ms): {text}\n")
        out, first, total = await stream(client, a.url, text, a.tgt,
                                         lambda d: print(d, end="", flush=True))
        print(f"\n\nfirst text after {first * 1000:.0f} ms, done after {total * 1000:.0f} ms")


async def bench(a):
    limits = httpx.Limits(max_connections=max(a.concurrency), max_keepalive_connections=max(a.concurrency))
    async with httpx.AsyncClient(timeout=60, limits=limits) as client:
        await stream(client, a.url, SENTENCES[0][1], "ta")                      # connect + warm
        for n in a.concurrency:
            jobs = []
            for i in range(n):
                src, text = SENTENCES[i % len(SENTENCES)]
                jobs.append((text, next(t for t in TARGETS[i % len(TARGETS):] + TARGETS if t != src)))
            t0 = time.perf_counter()
            res = await asyncio.gather(*[stream(client, a.url, text, tgt) for text, tgt in jobs])
            wall = time.perf_counter() - t0
            ttft = [r[1] * 1000 for r in res]
            total = [r[2] * 1000 for r in res]
            print(f"concurrency {n:3d}: first text p50 {statistics.median(ttft):5.0f} ms max {max(ttft):5.0f} ms | "
                  f"full sentence p50 {statistics.median(total):5.0f} ms max {max(total):5.0f} ms | "
                  f"{n / wall:5.1f} sentences/s")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8002/v1")
    p.add_argument("--text", default=SENTENCES[0][1])
    p.add_argument("--tgt", default="ta", help="ISO code (ta, hi, en, ...) or a prompt language name")
    p.add_argument("--route", default="direct", choices=["direct", "pivot"],
                   help="pivot: source -> English -> target, for two Indian languages")
    p.add_argument("--bench", action="store_true")
    p.add_argument("--concurrency", type=int, nargs="+", default=[1, 8, 32, 64])
    a = p.parse_args()
    asyncio.run(bench(a) if a.bench else single(a))
