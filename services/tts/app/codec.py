"""SNAC codes -> 24 kHz audio, incrementally.

Pipeline (from the model card): LM tokens -> SNAC codes -> SNAC quantizer
``from_codes`` -> z_q [B, 768, L] -> fine-tuned Vocos decoder -> waveform.

The quantizer is pointwise in time, but the Vocos ConvNeXt stack sees several
frames either side, so a streamed chunk is decoded together with ``left``/``right``
frames of context and only its own span is kept. ``right`` is lookahead: the LM
must produce that many extra frames before a chunk can be played.
"""
from __future__ import annotations

import collections
import os
import queue
import sys
import threading
import time
from concurrent.futures import Future

import numpy as np
import torch

SR = 24_000
NUM_CODEBOOKS = 7
CODEBOOK_SIZE = 4096
SAMPLES_PER_FRAME = 2048          # 4 latent steps x 512 samples
FRAME_MS = SAMPLES_PER_FRAME * 1000 / SR
LATENT_DIM = 768


class Codec:
    def __init__(self, model_dir: str, snac_dir: str, device: str = "cuda"):
        from snac import SNAC

        sys.path.insert(0, model_dir)
        from vocos.model import VocosDecoder  # shipped with the model

        self.device = torch.device(device)
        # TF32 convs pick shape-dependent algorithms whose ~1e-3 error differs between
        # streamed windows: a ~40 dB noise floor at chunk edges. FP32 makes a streamed
        # decode match a one-shot decode to ~77 dB (right=4) and bit-exact (right>=5).
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        ck = torch.load(os.path.join(model_dir, "vocos", "best.pt"), map_location="cpu", weights_only=True)
        self.vocos = VocosDecoder(latent_dim=LATENT_DIM, **ck["config"]["model"])
        self.vocos.load_state_dict(ck.get("ema") or ck["vocos"])
        self.vocos = self.vocos.eval().to(self.device)
        snac = SNAC.from_pretrained(snac_dir)
        self.quantizer = snac.quantizer.eval().to(self.device)   # only the quantizer is used
        del snac

    @torch.inference_mode()
    def decode_batch(self, windows: list[np.ndarray]) -> np.ndarray:
        """B windows of identical length [n, 7] (raw codes 0..4095) -> float32 [B, n * 2048].

        Equal lengths only: padding would change the edge frames, which a final
        (no lookahead) window keeps.
        """
        a = np.stack(windows).astype(np.int64)                     # B x n x 7
        B, n, _ = a.shape
        c1 = np.empty((B, n * 2), np.int64)
        c1[:, 0::2], c1[:, 1::2] = a[:, :, 1], a[:, :, 4]
        c2 = np.empty((B, n * 4), np.int64)
        c2[:, 0::4], c2[:, 1::4], c2[:, 2::4], c2[:, 3::4] = a[:, :, 2], a[:, :, 3], a[:, :, 5], a[:, :, 6]
        codes = [torch.from_numpy(x).to(self.device) for x in (np.ascontiguousarray(a[:, :, 0]), c1, c2)]
        z_q = self.quantizer.from_codes(codes)
        wav = self.vocos(z_q.float())
        return wav[:, 0].clamp(-1, 1).float().cpu().numpy()

    def decode(self, frames: np.ndarray) -> np.ndarray:
        return self.decode_batch([frames])[0]


class VocoderBatcher:
    """One GPU thread for every stream's vocoder calls. Requests that arrive within
    ``max_wait_ms`` of each other are grouped by window length and decoded together
    (2.1 ms for one window, ~0.8 ms per window in a batch of 8+ on an L40S)."""

    def __init__(self, codec: Codec, max_batch: int = 32, max_wait_ms: float = 2.0):
        self.codec, self.max_batch, self.max_wait = codec, max_batch, max_wait_ms / 1000
        self.q: queue.Queue[tuple[np.ndarray, Future]] = queue.Queue()
        threading.Thread(target=self._run, name="vocoder", daemon=True).start()

    def submit(self, window: np.ndarray) -> Future:
        fut: Future = Future()
        self.q.put((window, fut))
        return fut

    def _run(self):
        while True:
            jobs = [self.q.get()]
            deadline = time.perf_counter() + self.max_wait
            while len(jobs) < self.max_batch:
                timeout = deadline - time.perf_counter()
                if timeout <= 0:
                    break
                try:
                    jobs.append(self.q.get(timeout=timeout))
                except queue.Empty:
                    break
            groups: dict[int, list] = collections.defaultdict(list)
            for w, fut in jobs:
                groups[len(w)].append((w, fut))
            for group in groups.values():
                try:
                    out = self.codec.decode_batch([w for w, _ in group])
                    for (_, fut), wav in zip(group, out):
                        fut.set_result(wav)
                except Exception as e:  # noqa: BLE001 - hand the error to every waiting stream
                    for _, fut in group:
                        if not fut.done():
                            fut.set_exception(e)


class FrameAssembler:
    """Collects LM token ids into validated 7-token SNAC frames."""

    def __init__(self, audio_base: int):
        self.base = audio_base
        self.hi = audio_base + NUM_CODEBOOKS * CODEBOOK_SIZE
        self.offsets = audio_base + np.arange(NUM_CODEBOOKS) * CODEBOOK_SIZE
        self.partial: list[int] = []
        self.frames: list[np.ndarray] = []
        self.ended = False            # saw a non-audio token (end of speech)
        self.dropped = 0

    def add(self, token_ids) -> None:
        for t in token_ids:
            if self.ended:
                return
            if not (self.base <= t < self.hi):
                self.ended = True
                return
            self.partial.append(t)
            if len(self.partial) == NUM_CODEBOOKS:
                f = np.asarray(self.partial, np.int64) - self.offsets
                self.partial = []
                if np.all((f >= 0) & (f < CODEBOOK_SIZE)):
                    self.frames.append(f)
                else:
                    self.dropped += 1   # a code in the wrong frame position; the reference drops these too


class PlaybackClock:
    """Estimates how much audio the listener has buffered, assuming playback starts
    the moment the first audio is sent. Shared by every text chunk of a request."""

    def __init__(self):
        self.start: float | None = None
        self.sent_ms = 0.0

    def buffered_ms(self, now: float) -> float:
        return 0.0 if self.start is None else self.sent_ms - (now - self.start) * 1000

    def sent(self, n_samples: int, now: float):
        if self.start is None:
            self.start = now
        self.sent_ms += n_samples * 1000 / SR


class StreamDecoder:
    """Decides when to vocode the next chunk of one LM generation.

    The very first frame of a request goes out as soon as it has its lookahead.
    After that, while the listener has less than ``low_water_ms`` buffered every
    ready frame is sent at once (no stalls); with more slack it waits to send
    ``max_chunk`` frames per vocoder call (less GPU work).
    """

    def __init__(self, clock: PlaybackClock, left: int = 8, right: int = 4, max_chunk: int = 4,
                 low_water_ms: float = 200):
        self.clock = clock
        self.left, self.right = left, right
        self.max_chunk, self.low_water = max_chunk, low_water_ms
        self.emitted = 0

    def plan(self, n_frames: int, final: bool, now: float) -> tuple[int, int, int] | None:
        """-> (window_start, window_end, emit_to) frame indices, or None to wait."""
        target = n_frames if final else n_frames - self.right
        ready = target - self.emitted
        if ready <= 0:
            return None
        if not final:
            if self.clock.start is None:
                target = self.emitted + 1           # smallest possible first chunk
            elif ready < self.max_chunk and self.clock.buffered_ms(now) >= self.low_water:
                return None
        ws = max(0, self.emitted - self.left)
        we = n_frames if final else min(n_frames, target + self.right)
        return ws, we, target

    def take(self, wav: np.ndarray, plan: tuple[int, int, int], now: float) -> np.ndarray:
        ws, _, target = plan
        out = wav[(self.emitted - ws) * SAMPLES_PER_FRAME: (target - ws) * SAMPLES_PER_FRAME]
        self.clock.sent(len(out), now)
        self.emitted = target
        return out
