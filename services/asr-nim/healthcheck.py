"""Healthcheck for the ASR NIM (IndicConformer on Riva): up, and warmed up.

The first transcription after a Riva ASR NIM starts is several times slower than the
rest, and in a call that lands on the first utterance. So the first time the server reports
ready (per container start), this sends one short request to each language's model, and
only then reports healthy.

It also restarts the container when Riva dies. A request longer than the models' 16 s
window crashes Riva's inference server while the container keeps running, and Docker
doesn't restart a container for being unhealthy. So after the container has been up,
three failed checks in a row (~30 s) end every process in it, the container exits, and
`restart: unless-stopped` brings it back (~35 s).
"""
import io
import math
import os
import signal
import struct
import sys
import urllib.request
import uuid
import wave

BASE = "http://127.0.0.1:9000/v1"
SR = 16000
LANGUAGES = os.environ.get("ASR_WARM_LANGUAGES", "ml-IN hi-IN kn-IN te-IN ta-IN").split()
FAILS = "/tmp/asr-health-fails"


def warm_wav(seconds: float = 3.0) -> bytes:
    """A voiced-sounding tone (harmonics with a syllable-rate envelope): enough to run the engines."""
    frames = bytearray()
    for i in range(int(SR * seconds)):
        t = i / SR
        env = 0.5 + 0.5 * math.sin(2 * math.pi * 4 * t)
        x = sum(math.sin(2 * math.pi * f * t) / k for k, f in enumerate((140, 280, 420, 700), 1))
        frames += struct.pack("<h", int(6000 * env * x / 2))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def transcribe(audio: bytes, language: str = "hi-IN"):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{language}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"warm.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{BASE}/audio/transcriptions", body,
                                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


def main() -> int:
    # one marker per container start: PID 1's start time changes when the container restarts
    marker = "/tmp/asr-warm-" + open("/proc/1/stat").read().rsplit(")", 1)[1].split()[19]
    try:
        urllib.request.urlopen(f"{BASE}/health/ready", timeout=3).read()
    except OSError:
        if os.path.exists(marker):                      # it was up: Riva died
            fails = (int(open(FAILS).read()) if os.path.exists(FAILS) else 0) + 1
            open(FAILS, "w").write(str(fails))
            if fails >= 3:
                os.kill(-1, signal.SIGKILL)             # every process but PID 1: the container exits
        return 1                                        # still loading, or down
    if os.path.exists(FAILS):
        os.remove(FAILS)
    if os.path.exists(marker):
        return 0
    audio = warm_wav()
    for language in LANGUAGES:          # every language's model, once
        transcribe(audio, language)
    open(marker, "w").close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
