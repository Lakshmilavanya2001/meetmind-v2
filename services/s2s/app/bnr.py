"""NVIDIA Maxine BNR (background noise removal) over gRPC, for a caller's microphone before speech
detection: traffic, fans and office noise otherwise reach the voice detector and the recogniser.

Bidirectional stream ``nvidia.ai4m.bnr.v1.BNR/EnhanceAudio``: the first request carries the config
(intensity 0-1), then 10 ms chunks of 48 kHz mono float32 audio go in and cleaned chunks of the
same format come out, ~13 ms later plus 40 ms of model lookahead (measured on our server).

The messages are tiny, so they're encoded by hand (no generated code):
  EnhanceAudioRequest  { bytes audio_stream_data = 1; EnhanceAudioConfig config = 2; }   (oneof)
  EnhanceAudioConfig   { optional float intensity_ratio = 1; }
  EnhanceAudioResponse { bytes audio_stream_data = 1; EnhanceAudioConfig config = 2; }  (oneof)

If the stream fails, the original audio is passed through (the caller is never lost) and the
stream is reopened a few seconds later.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import struct
from typing import Callable

import grpc
import numpy as np

log = logging.getLogger("s2s.bnr")

BNR_RATE = 48_000
BNR_CHUNK = BNR_RATE // 100           # 10 ms
BNR_URL = os.environ.get("BNR_GRPC_URL", "").strip().removeprefix("grpc://")
BNR_INTENSITY = min(1.0, max(0.0, float(os.environ.get("BNR_INTENSITY", "1") or 1)))
BNR_MAX_STREAMS = max(1, int(os.environ.get("BNR_MAX_STREAMS", "8") or 8))
METHOD = "/nvidia.ai4m.bnr.v1.BNR/EnhanceAudio"

active_streams = 0                    # BNR streams open in this process (each is GPU work)


def available() -> bool:
    return bool(BNR_URL)


def _varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def encode_config(intensity: float) -> bytes:
    inner = b"\x0d" + struct.pack("<f", intensity)            # field 1, 32-bit float
    return b"\x12" + _varint(len(inner)) + inner              # field 2, length-delimited


def encode_audio(samples: np.ndarray) -> bytes:
    data = samples.astype("<f4").tobytes()
    return b"\x0a" + _varint(len(data)) + data                # field 1, length-delimited


def decode_audio(buf: bytes) -> np.ndarray | None:
    """The audio in a response (field 1), or None for a config echo or anything else."""
    i = 0
    while i < len(buf):
        tag = buf[i]
        i += 1
        length = shift = 0
        while True:
            b = buf[i]
            i += 1
            length |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        if tag == 0x0A:
            body = buf[i:i + length - (length % 4)]
            return np.frombuffer(body, dtype="<f4")
        if tag & 7 != 2:
            return None
        i += length
    return None


def int16_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def float_to_int16(f: np.ndarray) -> bytes:
    return np.clip(np.round(f * 32768.0), -32768, 32767).astype("<i2").tobytes()


class BnrStream:
    """One caller's noise-removal stream. ``push`` 48 kHz mono 16-bit audio in any chunk size;
    cleaned audio comes out through ``on_audio`` (16-bit bytes). While BNR is unavailable,
    ``on_audio`` gets the original audio at once."""

    def __init__(self, on_audio: Callable[[bytes], None], target: str = BNR_URL, intensity: float = BNR_INTENSITY):
        self.on_audio, self.target, self.intensity = on_audio, target, intensity
        self.pending = b""
        self.queue: asyncio.Queue[bytes | None] | None = None
        self.passthrough = True
        self.failures = 0
        self.closed = False
        self.task: asyncio.Task | None = None
        self.counted = False

    @property
    def cleaning(self) -> bool:
        return not self.passthrough

    def start(self):
        global active_streams
        if active_streams >= BNR_MAX_STREAMS:
            log.warning("%d noise-removal streams already running; passing this caller through", BNR_MAX_STREAMS)
            return
        active_streams += 1
        self.counted = True
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        while not self.closed:
            self.queue = asyncio.Queue(maxsize=200)            # 2 s of audio; older audio is dropped
            try:
                async with grpc.aio.insecure_channel(self.target) as channel:
                    call = channel.stream_stream(METHOD, request_serializer=lambda b: b, response_deserializer=lambda b: b)

                    async def requests():
                        yield encode_config(self.intensity)
                        while True:
                            item = await self.queue.get()
                            if item is None:
                                return
                            yield item

                    self.passthrough = False
                    self.pending = b""
                    async for resp in call(requests()):
                        f = decode_audio(resp)
                        if f is not None and len(f):
                            self.on_audio(float_to_int16(f))
                        self.failures = 0
                if self.closed:
                    return
                raise RuntimeError("stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - fall back to the original audio, then retry
                self.passthrough = True
                self.failures += 1
                if not self.closed:
                    log.warning("noise removal unavailable (%s); passing the original audio through", e)
                    await asyncio.sleep(min(30, 2 * self.failures))

    def push(self, pcm: bytes):
        if self.closed:
            return
        if self.passthrough or self.queue is None:
            self.on_audio(pcm)
            return
        data = self.pending + pcm
        whole = len(data) - (len(data) % (BNR_CHUNK * 2))
        for k in range(0, whole, BNR_CHUNK * 2):
            chunk = encode_audio(int16_to_float(data[k:k + BNR_CHUNK * 2]))
            try:
                self.queue.put_nowait(chunk)
            except asyncio.QueueFull:                       # BNR fell behind: drop the oldest
                self.queue.get_nowait()
                self.queue.put_nowait(chunk)
        self.pending = data[whole:]

    async def close(self):
        global active_streams
        self.closed = True
        if self.queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(None)
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        if self.counted:
            active_streams -= 1
            self.counted = False

