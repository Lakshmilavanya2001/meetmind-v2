"""/health/ready: is everything the app needs reachable? Only names and up/down are shown (no addresses).

A model server counts as up if it answers HTTP at all within 3 s (a 404 still means it's running);
BNR is a gRPC port, so a TCP connection is enough. "required" ones make the status "degraded" when down.
"""
from __future__ import annotations

import asyncio
import os
import time

import aiohttp
from sqlalchemy import text

from . import bnr, db, embeddings
from .assistant import llm

TIMEOUT_S = 3


async def _http(http: aiohttp.ClientSession, url: str) -> bool:
    try:
        async with http.get(url) as r:
            return r.status < 500
    except Exception:  # noqa: BLE001
        return False


async def _tcp(hostport: str) -> bool:
    host, _, port = hostport.rpartition(":")
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), TIMEOUT_S)
        w.close()
        return True
    except Exception:  # noqa: BLE001
        return False


async def _db() -> bool:
    try:
        async with db.sessionmaker()() as s:
            await s.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


async def check() -> dict:
    t0 = time.time()
    checks: dict[str, tuple[bool, bool]] = {}          # name -> (required, coroutine result later)
    jobs = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as http:
        def add(name, required, coro):
            jobs[name] = (required, coro)
        if os.environ.get("DATABASE_URL"):
            add("database", True, _db())
        add("livekit", True, _http(http, os.environ.get("LIVEKIT_URL", "http://127.0.0.1:7880").replace("ws", "http", 1)))
        add("call_agent", True, _http(http, f"http://127.0.0.1:{os.environ.get('S2S_HEALTH_PORT', '8081')}/"))
        if os.environ.get("ASR_URL"):
            add("speech_recognition", True, _http(http, os.environ["ASR_URL"].rstrip("/") + "/v1/health/ready"))
        if os.environ.get("MAGPIE_TTS_URL"):
            add("speech_synthesis", True, _http(http, os.environ["MAGPIE_TTS_URL"].rstrip("/") + "/health/ready"))
        if llm.configured():
            add("assistant_llm", False, _http(http, llm.LLM_URL + "/models"))
        if embeddings.configured():
            add("embeddings", False, _http(http, embeddings.URL + "/models"))
        if bnr.available():
            add("noise_removal", False, _tcp(bnr.BNR_URL))
        results = await asyncio.gather(*(c for _, c in jobs.values()))
    for (name, (required, _)), ok in zip(jobs.items(), results):
        checks[name] = (required, ok)
    down_required = [n for n, (req, ok) in checks.items() if req and not ok]
    return {"status": "degraded" if down_required else "ok",
            "checks": {n: "up" if ok else "down" for n, (_, ok) in checks.items()},
            "ms": round((time.time() - t0) * 1000)}
