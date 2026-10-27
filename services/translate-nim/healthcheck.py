"""Healthcheck for the translate NIM (indic-translate): up, and warmed up.

vLLM JIT-compiles its batched-decode kernels on the first concurrent requests, a ~1 s
stall that would otherwise land on the first call. The first time the server answers
after a container start, this sends a few requests one at a time and then a burst of 8
together, and only then reports healthy. Services that wait for translate to be healthy
start after the warm-up.
"""
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "http://127.0.0.1:8000/v1"
MODEL = os.environ.get("NIM_SERVED_MODEL_NAME", "indic-translate")
TEMPLATE = "Translate the following text into {tgt}:\n\n{text}"     # the model's prompt contract


def translate(text: str, tgt: str) -> str:
    body = {"model": MODEL, "temperature": 0, "max_tokens": 64, "stop": ["<turn|>"],
            "messages": [{"role": "user", "content": TEMPLATE.replace("{tgt}", tgt).replace("{text}", text)}]}
    req = urllib.request.Request(f"{BASE}/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def main() -> int:
    try:
        urllib.request.urlopen(f"{BASE}/models", timeout=3).read()
    except OSError:
        return 1                                        # still loading
    # one marker per container start: PID 1's start time changes when the container restarts
    marker = "/tmp/translate-warm-" + open("/proc/1/stat").read().rsplit(")", 1)[1].split()[19]
    if os.path.exists(marker):
        return 0
    for tgt in ("Tamil", "Hindi", "English"):
        translate("नमस्ते, आप कैसे हैं?", tgt)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: translate("आज मौसम बहुत अच्छा है।", "Tamil"), range(8)))
    open(marker, "w").close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
