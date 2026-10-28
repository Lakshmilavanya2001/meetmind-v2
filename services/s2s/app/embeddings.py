"""Text embeddings for transcript search: an OpenAI-compatible /embeddings server (NVIDIA
llama-nemotron-embed-1b-v2 here, 2048 dimensions; it retrieved better without query/passage
prefixes, as measured in meetmind)."""
from __future__ import annotations

import os

import aiohttp

URL = os.environ.get("EMBEDDING_BASE_URL", "").rstrip("/")
MODEL = os.environ.get("EMBEDDING_MODEL", "")
KEY = os.environ.get("EMBEDDING_API_KEY", "")
DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSIONS", "2048"))
BATCH = 32


def configured() -> bool:
    return bool(URL and MODEL)


async def embed(http: aiohttp.ClientSession, texts: list[str], kind: str = "passage") -> list[list[float]]:
    out: list[list[float]] = []
    headers = {"Authorization": f"Bearer {KEY}"} if KEY else {}
    for i in range(0, len(texts), BATCH):
        body = {"model": MODEL, "input": texts[i:i + BATCH], "input_type": "query" if kind == "query" else "passage"}
        async with http.post(f"{URL}/embeddings", json=body, headers=headers) as r:
            if r.status != 200:
                raise RuntimeError(f"embeddings HTTP {r.status}: {(await r.text())[:200]}")
            data = await r.json()
        vecs = [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
        if any(len(v) != DIMENSIONS for v in vecs):
            raise RuntimeError(f"embedding model returned vectors of the wrong size (expected {DIMENSIONS})")
        out += vecs
    return out
