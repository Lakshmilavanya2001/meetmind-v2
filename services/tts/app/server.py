"""Streaming TTS service (bodhan-ai/indic-speak: Llama-3.2-3B speech LM + Vocos).

The speech LM runs in an NVIDIA NIM (the model-free LLM NIM, `tts-lm` in compose): this
service sends it the prompt as token ids on the OpenAI completions API and streams the
generated audio-token ids back. Turning those tokens into sound (SNAC codes -> Vocos) is
not something a NIM does, so it happens here, on the GPU, in streamed windows.

HTTP
  GET  /health
  GET  /v1/voices
  POST /v1/tts      {"text": "...", "language": "ta", "speaker": null, "gender": "female",
                     "style": "", "sample_rate": 24000, "format": "wav", "stream": false}
                    stream=true returns chunked audio as it is generated
WebSocket  /v1/stream
  client -> {"type": "synthesize", "id": "u1", "text": "...", "language": "ta", ...same fields}
            {"type": "cancel", "id": "u1"}   or   {"type": "cancel"}  (current + queued)
  server -> {"type": "start", "id": "u1", ...}, binary audio frames, {"type": "done", "id": "u1", ...}
  Requests on one socket are synthesized in order, so binary frames always belong
  to the request most recently announced by "start".
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import struct
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator

import aiohttp
import numpy as np
import soundfile as sf
import soxr
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .codec import SR, Codec, FrameAssembler, PlaybackClock, StreamDecoder, VocoderBatcher
from .voices import ALL_SPEAKERS, DEFAULT_VOICES, STYLES, normalize_language, pick_speaker

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tts")

MODEL_DIR = os.environ.get("MODEL_DIR", "/models/indic-speak")
SNAC_DIR = os.environ.get("SNAC_DIR", "/models/snac_24khz")
LM_URL = os.environ.get("TTS_LM_URL", "http://tts-lm:8000/v1")          # OpenAI-compatible (the NIM)
LM_MODEL = os.environ.get("TTS_LM_MODEL", "indic-speak")
MAX_MODEL_LEN = int(os.environ.get("MAX_MODEL_LEN", "4096"))           # must match the NIM's --max-model-len
TOKENS_PER_SECOND = 82                  # model card: 82 tokens ~ 1 s of audio
MIN_CHARS_PER_S = float(os.environ.get("TTS_MIN_CHARS_PER_S", "9"))   # length cap, see synthesize()
MAX_CHUNK_CHARS = int(os.environ.get("TTS_MAX_CHUNK_CHARS", "300"))
DEFAULTS = dict(
    temperature=float(os.environ.get("TTS_TEMPERATURE", "0.6")),
    top_p=float(os.environ.get("TTS_TOP_P", "0.9")),
    top_k=int(os.environ.get("TTS_TOP_K", "50")),
    # voices.md documents the production config as t 0.6 / top_p 0.9 / rp 1.2
    repetition_penalty=float(os.environ.get("TTS_REPETITION_PENALTY", "1.2")),
)
# Vocoder context per streamed chunk, in SNAC frames (85 ms each). Measured against a
# one-shot decode: right=4 is bit-exact (~70 dB SNR); right=2 saves ~140 ms of
# first-audio latency but leaves ~30 dB errors (possible clicks) at chunk edges.
STREAM = dict(left=int(os.environ.get("TTS_CONTEXT_LEFT", "8")),
              right=int(os.environ.get("TTS_CONTEXT_RIGHT", "4")),
              max_chunk=int(os.environ.get("TTS_MAX_CHUNK_FRAMES", "4")),
              low_water_ms=float(os.environ.get("TTS_LOW_WATER_MS", "200")))

app = FastAPI(title="Indic Speak TTS")
state: dict = {"ready": False}


@app.on_event("startup")
async def startup():
    from transformers import AutoTokenizer

    t0 = time.perf_counter()
    tok = AutoTokenizer.from_pretrained(MODEL_DIR)
    state["tok"] = tok
    tid = tok.convert_tokens_to_ids
    state["ids"] = {k: tid(v) for k, v in dict(
        start_human="<|start_of_human|>", end_human="<|end_of_human|>", start_ai="<|start_of_ai|>",
        end_ai="<|end_of_ai|>", start_speech="<|start_of_speech|>", end_speech="<|end_of_speech|>",
        eot="<|eot_id|>", spk_open="<|speaker>", spk_close="<speaker|>", style_open="<|style>",
        style_close="<style|>", audio_base="<|snac_0|>").items()}
    state["nl"] = tok.encode("\n", add_special_tokens=False)
    codec = await asyncio.to_thread(Codec, MODEL_DIR, SNAC_DIR)
    state["vocoder"] = VocoderBatcher(codec, max_batch=int(os.environ.get("TTS_VOCODER_MAX_BATCH", "32")))
    state["http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=5, sock_read=30),
                                          connector=aiohttp.TCPConnector(limit=0))
    state["max_model_len"] = MAX_MODEL_LEN
    # seeded/unseeded and single/batched sampling each JIT-compile their own Triton
    # kernels in the LM server (a multi-second spike if left to the first real request);
    # the first vocoder windows also allocate. Warm them all.
    async def drain(req: SynthRequest):
        async for _ in synthesize(req, uuid.uuid4().hex):
            pass

    for seed in (None, 1):
        await drain(SynthRequest(text="नमस्ते।", language="hi", seed=seed))
    await asyncio.gather(*[drain(SynthRequest(text="வணக்கம், எப்படி இருக்கிறீர்கள்?", language="ta",
                                              seed=(1 if i % 2 else None))) for i in range(4)])
    state["ready"] = True
    log.info("ready in %.1fs", time.perf_counter() - t0)


@app.on_event("shutdown")
async def shutdown():
    if "http" in state:
        await state["http"].close()


async def lm_tokens(prompt: list[int], params: dict) -> AsyncIterator[list[int]]:
    """Stream generated token ids from the speech-LM NIM (OpenAI completions, token-id prompt)."""
    body = {"model": LM_MODEL, "prompt": prompt, "stream": True, "return_token_ids": True, **params}
    async with state["http"].post(f"{LM_URL}/completions", json=body) as r:
        if r.status != 200:
            raise RuntimeError(f"speech LM: HTTP {r.status} {(await r.text())[:200]}")
        async for line in r.content:                                # server-sent events
            if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                continue
            toks = json.loads(line[6:])["choices"][0].get("token_ids")
            if toks:
                yield toks
    # leaving early closes the connection, and the server aborts the request


# ---------------------------------------------------------------------------- request model
class SynthRequest(BaseModel):
    text: str
    language: str | None = Field(None, description="picks the default voice; the model reads any script")
    speaker: str | None = None
    gender: str | None = Field(None, description="female | male, used when speaker is not given")
    style: str = ""
    sample_rate: int = Field(SR, description="24000 native; 16000 / 8000 are resampled")
    format: str = Field("wav", description="wav | pcm_s16le | pcm_f32le")
    stream: bool = False
    temperature: float | None = Field(None, ge=0, le=2)
    top_p: float | None = Field(None, gt=0, le=1)
    top_k: int | None = None
    repetition_penalty: float | None = Field(None, ge=1, le=2)
    seed: int | None = None
    max_seconds: float | None = Field(None, gt=0, le=60)


def build_prompt(text: str, speaker: str, style: str) -> list[int]:
    """Same layout as the model repo's inference.py::build_prompt."""
    ids, tok, nl = state["ids"], state["tok"], state["nl"]
    enc = lambda s: tok.encode(s, add_special_tokens=False)  # noqa: E731
    blocks = []
    if speaker.strip():
        blocks.append([ids["spk_open"]] + enc(speaker.strip()) + [ids["spk_close"]])
    if style.strip():
        blocks.append([ids["style_open"]] + enc(style.strip()) + [ids["style_close"]])
    meta: list[int] = []
    for i, b in enumerate(blocks):
        meta += (nl if i else []) + b
    if blocks:
        meta += nl
    body = [tok.bos_token_id] + meta + enc(text) + [ids["eot"]]
    return [ids["start_human"]] + body + [ids["end_human"], ids["start_ai"], ids["start_speech"]]


_SENT_END = re.compile(r"(?<=[.!?।॥|\n])\s+")


def split_text(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Sentence chunks of at most ~limit chars; the model is trained on utterances, not essays."""
    text = re.sub(r"[ \t]+", " ", text).strip()
    chunks, cur = [], ""
    for sent in filter(None, (s.strip() for s in _SENT_END.split(text))):
        while len(sent) > limit:                          # one very long sentence: cut at a comma/space
            cut = max(sent.rfind(",", 0, limit), sent.rfind(" ", 0, limit))
            cut = cut if cut > limit // 3 else limit
            chunks.append(sent[: cut + 1].strip())
            sent = sent[cut + 1:].strip()
        if cur and len(cur) + 1 + len(sent) > limit:
            chunks.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        chunks.append(cur)
    return chunks


@dataclass
class Stats:
    ttfa_ms: float | None = None
    audio_samples: int = 0
    tokens: int = 0
    dropped_frames: int = 0
    chunks: int = 0


async def synthesize(req: SynthRequest, rid: str, stats: Stats | None = None) -> AsyncIterator[np.ndarray]:
    """Yield float32 audio at 24 kHz as soon as each piece is decodable."""
    stats = stats or Stats()
    ids = state["ids"]
    speaker = pick_speaker(req.language, req.speaker, req.gender)
    clock = PlaybackClock()
    t0 = time.perf_counter()

    async def vocode(dec: StreamDecoder, frames: list[np.ndarray], final: bool) -> np.ndarray | None:
        plan = dec.plan(len(frames), final, time.perf_counter())
        if plan is None:
            return None
        ws, we, _ = plan
        wav = await asyncio.wrap_future(state["vocoder"].submit(np.stack(frames[ws:we])))
        out = dec.take(wav, plan, time.perf_counter())
        if stats.ttfa_ms is None:
            stats.ttfa_ms = (time.perf_counter() - t0) * 1000
        stats.audio_samples += len(out)
        return out

    for ci, chunk in enumerate(split_text(req.text)):
        prompt = build_prompt(chunk, speaker, req.style)
        # length cap: the speech LM sometimes doesn't stop and babbles on. No normal clip we
        # measured (6 languages, 144 clips) spoke slower than 9 chars/s plus 1.2 s of pauses
        est_s = req.max_seconds or min(30.0, len(chunk) / MIN_CHARS_PER_S + 1.2)
        max_tokens = min(int(est_s * TOKENS_PER_SECOND), state["max_model_len"] - len(prompt))
        params = dict(
            temperature=req.temperature if req.temperature is not None else DEFAULTS["temperature"],
            top_p=req.top_p or DEFAULTS["top_p"], top_k=req.top_k or DEFAULTS["top_k"],
            repetition_penalty=req.repetition_penalty or DEFAULTS["repetition_penalty"],
            max_tokens=max_tokens, seed=req.seed,
            stop_token_ids=[ids["end_speech"], ids["end_ai"], state["tok"].eos_token_id])
        frames = FrameAssembler(ids["audio_base"])
        dec = StreamDecoder(clock, **STREAM)
        gen = lm_tokens(prompt, params)
        try:
            async for toks in gen:
                stats.tokens += len(toks)
                frames.add(toks)
                wav = await vocode(dec, frames.frames, final=False)
                if wav is not None and len(wav):
                    yield wav
                if frames.ended:
                    break
        finally:
            await gen.aclose()                      # aborts the request if we stopped early
        if frames.frames:
            wav = await vocode(dec, frames.frames, final=True)
            if wav is not None and len(wav):
                yield wav
        stats.dropped_frames += frames.dropped
        stats.chunks += 1


class Encoder:
    """float32 @ 24 kHz -> bytes in the requested rate/format, streaming-safe."""

    def __init__(self, sample_rate: int, fmt: str):
        if sample_rate not in (8000, 16000, 22050, 24000, 44100, 48000):
            raise ValueError("sample_rate must be one of 8000, 16000, 22050, 24000, 44100, 48000")
        if fmt not in ("wav", "pcm_s16le", "pcm_f32le"):
            raise ValueError("format must be wav, pcm_s16le or pcm_f32le")
        self.sr, self.fmt = sample_rate, fmt
        self.rs = soxr.ResampleStream(SR, sample_rate, 1, dtype="float32") if sample_rate != SR else None

    def __call__(self, wav: np.ndarray, last: bool = False) -> bytes:
        if self.rs is not None:
            wav = self.rs.resample_chunk(wav, last=last)
        if self.fmt == "pcm_f32le":
            return wav.astype("<f4").tobytes()
        return (np.clip(wav, -1, 1) * 32767).astype("<i2").tobytes()

    def wav_header(self) -> bytes:
        """Header for a WAV of unknown length (players read to EOF)."""
        ch, bits = 1, 32 if self.fmt == "pcm_f32le" else 16
        fmt_tag = 3 if self.fmt == "pcm_f32le" else 1
        byte_rate = self.sr * ch * bits // 8
        return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " +
                struct.pack("<IHHIIHH", 16, fmt_tag, ch, self.sr, byte_rate, ch * bits // 8, bits) +
                b"data" + struct.pack("<I", 0xFFFFFFFF))


def _check_ready():
    if not state["ready"]:
        raise HTTPException(503, "model loading")


# ---------------------------------------------------------------------------- HTTP
@app.get("/health")
async def health():
    if not state["ready"]:
        return JSONResponse({"status": "loading"}, status_code=503)
    return {"status": "ok"}


@app.get("/v1/voices")
async def voices():
    return {"default_voices": {k: {"female": f, "male": m} for k, (f, m) in DEFAULT_VOICES.items()},
            "speakers": ALL_SPEAKERS, "styles": STYLES, "sample_rate": SR}


@app.post("/v1/tts")
async def tts(req: SynthRequest):
    _check_ready()
    if not req.text.strip():
        raise HTTPException(400, "empty text")
    try:
        enc = Encoder(req.sample_rate, req.format)
    except ValueError as e:
        raise HTTPException(400, str(e))
    rid = uuid.uuid4().hex
    media = {"wav": "audio/wav", "pcm_s16le": f"audio/L16;rate={req.sample_rate}"}.get(
        req.format, "application/octet-stream")
    speaker = pick_speaker(req.language, req.speaker, req.gender)

    if req.stream:
        async def body():
            if req.format == "wav":
                yield enc.wav_header()
            async for wav in synthesize(req, rid):
                yield enc(wav)
            tail = enc(np.zeros(0, np.float32), last=True)
            if tail:
                yield tail
        return StreamingResponse(body(), media_type=media,
                                 headers={"X-Sample-Rate": str(req.sample_rate), "X-Speaker": speaker})

    stats = Stats()
    t0 = time.perf_counter()
    pieces = [w async for w in synthesize(req, rid, stats)]
    audio = np.concatenate(pieces) if pieces else np.zeros(0, np.float32)
    latency = (time.perf_counter() - t0) * 1000
    headers = {"X-Sample-Rate": str(req.sample_rate), "X-Speaker": speaker,
               "X-Audio-Seconds": f"{len(audio) / SR:.3f}", "X-Latency-Ms": f"{latency:.1f}",
               "X-TTFA-Ms": f"{stats.ttfa_ms or 0:.1f}", "X-Tokens": str(stats.tokens)}
    if req.format == "wav":
        if req.sample_rate != SR:
            audio = soxr.resample(audio, SR, req.sample_rate)
        buf = io.BytesIO()
        sf.write(buf, audio, req.sample_rate, format="WAV", subtype="PCM_16")
        return Response(buf.getvalue(), media_type=media, headers=headers)
    return Response(enc(audio, last=True), media_type=media, headers=headers)


# ---------------------------------------------------------------------------- WebSocket
@app.websocket("/v1/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    if not state["ready"]:
        await ws.send_text(json.dumps({"type": "error", "message": "model loading"}))
        await ws.close(code=1013)
        return
    lock = asyncio.Lock()
    queue: asyncio.Queue[tuple[str, SynthRequest]] = asyncio.Queue()
    current: dict = {"id": None, "task": None}
    cancelled: set[str] = set()

    async def send_json(msg: dict):
        async with lock:
            await ws.send_text(json.dumps(msg, ensure_ascii=False))

    async def run_one(cid: str, req: SynthRequest):
        enc = Encoder(req.sample_rate, "pcm_f32le" if req.format == "pcm_f32le" else "pcm_s16le")
        stats = Stats()
        t0 = time.perf_counter()
        await send_json({"type": "start", "id": cid, "sample_rate": req.sample_rate, "encoding": enc.fmt,
                         "speaker": pick_speaker(req.language, req.speaker, req.gender)})
        async for wav in synthesize(req, f"{cid}-{uuid.uuid4().hex[:6]}", stats):
            data = enc(wav)
            if data:
                async with lock:
                    await ws.send_bytes(data)
        tail = enc(np.zeros(0, np.float32), last=True)
        if tail:
            async with lock:
                await ws.send_bytes(tail)
        latency = (time.perf_counter() - t0) * 1000
        audio_ms = stats.audio_samples * 1000 / SR
        await send_json({"type": "done", "id": cid, "audio_ms": round(audio_ms, 1),
                         "ttfa_ms": round(stats.ttfa_ms or 0, 1), "latency_ms": round(latency, 1),
                         "rtf": round(latency / audio_ms, 3) if audio_ms else None,
                         "tokens": stats.tokens, "chunks": stats.chunks})

    async def worker():
        while True:
            cid, req = await queue.get()
            if cid in cancelled:
                cancelled.discard(cid)
                continue
            current["id"] = cid
            current["task"] = asyncio.create_task(run_one(cid, req))
            try:
                await current["task"]
            except asyncio.CancelledError:
                if ws.client_state.name != "CONNECTED":
                    raise
                await send_json({"type": "cancelled", "id": cid})
            except Exception as e:  # noqa: BLE001
                log.exception("synthesis %s failed", cid)
                await send_json({"type": "error", "id": cid, "message": str(e)})
            finally:
                current["id"] = current["task"] = None

    worker_task = asyncio.create_task(worker())
    await send_json({"type": "ready", "sample_rate": SR})
    try:
        while True:
            try:
                msg = json.loads(await ws.receive_text())
            except json.JSONDecodeError:
                await send_json({"type": "error", "message": "messages must be JSON"})
                continue
            kind = msg.get("type")
            if kind == "synthesize":
                cid = str(msg.get("id") or uuid.uuid4().hex[:8])
                try:
                    req = SynthRequest(**{k: v for k, v in msg.items() if k not in ("type", "id")})
                    Encoder(req.sample_rate, "pcm_s16le")
                    if not req.text.strip():
                        raise ValueError("empty text")
                except Exception as e:  # noqa: BLE001 - pydantic / value errors
                    await send_json({"type": "error", "id": cid, "message": f"bad request: {e}"})
                    continue
                await queue.put((cid, req))
                await send_json({"type": "queued", "id": cid, "position": queue.qsize()})
            elif kind == "cancel":
                cid = msg.get("id")
                if cid is None:                       # barge-in: drop everything
                    while not queue.empty():
                        queue.get_nowait()
                    if current["task"] is not None:
                        current["task"].cancel()
                elif cid == current["id"] and current["task"] is not None:
                    current["task"].cancel()
                else:
                    cancelled.add(str(cid))
            else:
                await send_json({"type": "error", "message": f"unknown message type {kind!r}"})
    except WebSocketDisconnect:
        pass
    finally:
        if current["task"] is not None:
            current["task"].cancel()
        worker_task.cancel()
