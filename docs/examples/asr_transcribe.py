"""Transcribe an audio file with the ASR service: AI4Bharat IndicConformer models served by
NVIDIA's Riva ASR NIM (HTTP API, one request per piece of audio).

    pip install requests numpy
    python asr_transcribe.py recording.wav --language ta

Send 16-bit PCM WAV, mono, 16 kHz. Each request may hold at most 16 s of audio: the models
read a whole utterance in one pass, and a longer request crashes the server (it restarts
itself within a minute). Longer files are cut at the quietest point before each 15 s mark.
"""
import argparse
import io
import time
import wave

import numpy as np
import requests

ASR = "http://localhost:8001"
LANGUAGES = {"hi": "hi-IN", "kn": "kn-IN", "ml": "ml-IN", "ta": "ta-IN", "te": "te-IN"}


def pieces(audio: np.ndarray, sr: int, max_s: float = 15.0, search_s: float = 3.0):
    """Cut int16 audio into pieces of at most max_s, each at the quietest 30 ms in the
    last search_s before the limit (a pause, usually)."""
    start, frame = 0, int(0.03 * sr)
    while len(audio) - start > max_s * sr:
        lo, hi = start + int((max_s - search_s) * sr), start + int(max_s * sr)
        energy = [np.abs(audio[i:i + frame].astype(np.int32)).sum() for i in range(lo, hi - frame, frame)]
        cut = lo + int(np.argmin(energy)) * frame + frame // 2
        yield audio[start:cut]
        start = cut
    yield audio[start:]


def wav_bytes(audio: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(audio.tobytes())
    return buf.getvalue()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("file", help="16-bit PCM WAV, mono (16 kHz is ideal)")
    p.add_argument("--language", required=True, choices=sorted(LANGUAGES))
    a = p.parse_args()

    with wave.open(a.file) as w:
        sr = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    t0 = time.perf_counter()
    texts = []
    for piece in pieces(audio, sr):
        r = requests.post(f"{ASR}/v1/audio/transcriptions", data={"language": LANGUAGES[a.language]},
                          files={"file": ("audio.wav", wav_bytes(piece, sr), "audio/wav")}, timeout=60)
        r.raise_for_status()
        texts.append(r.json()["text"].strip())
    print(" ".join(texts))
    print(f"\n{len(audio) / sr:.1f} s of audio in {len(texts)} request(s), {(time.perf_counter() - t0) * 1000:.0f} ms")
