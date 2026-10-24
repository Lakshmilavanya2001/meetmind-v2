"""Use the translate service (sarvam-translate on an NVIDIA NIM, OpenAI-compatible API):
one sentence, streamed, several at once, and Indian -> Indian through English.

    pip install requests
    python translate.py "नमस्ते, आप कैसे हैं?" --to English

Every request follows the model card: a system message naming the target language
("Translate the text below to Tamil."), the text as the user message, greedy decoding.
The model infers the source language. Its open checkpoint translates English <-> the
22 Indian languages; between two Indian languages, go through English (two calls).
Targets are plain language names: English, Hindi, Tamil, Manipuri, ...
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor

import requests

URL = "http://localhost:8002/v1/chat/completions"


def request(text, tgt, stream=False):
    return {"model": "sarvam-translate", "temperature": 0, "stream": stream,
            "max_tokens": min(1024, 2 * len(text) + 32),          # cap runaway generations
            "messages": [{"role": "system", "content": f"Translate the text below to {tgt}."},
                         {"role": "user", "content": text}]}


def translate(text, tgt):
    r = requests.post(URL, json=request(text, tgt), timeout=60)
    r.raise_for_status()
    return r.json()


def one(text, tgt):
    res = translate(text, tgt)
    print(f"[one]    {res['choices'][0]['message']['content'].strip()}   "
          f"({res['usage']['completion_tokens']} tokens, finish_reason {res['choices'][0]['finish_reason']})")


def streamed(text, tgt):
    """Server-Sent Events: text arrives as it is generated."""
    with requests.post(URL, json=request(text, tgt, stream=True), stream=True, timeout=60) as r:
        r.raise_for_status()
        print("[stream] ", end="", flush=True)
        for line in r.iter_lines(decode_unicode=True):
            if line.startswith("data: ") and line != "data: [DONE]":
                print(json.loads(line[6:])["choices"][0]["delta"].get("content") or "", end="", flush=True)
        print()


def many(texts, tgt):
    """Concurrent requests share one batch on the GPU: the way to translate a list."""
    with ThreadPoolExecutor(8) as pool:
        for src, res in zip(texts, pool.map(lambda t: translate(t, tgt), texts)):
            print(f"[many]   {src}  ->  {res['choices'][0]['message']['content'].strip()}")


def pivot(text, tgt):
    """Source -> English -> target: two calls."""
    english = translate(text, "English")["choices"][0]["message"]["content"].strip()
    out = translate(english, tgt)["choices"][0]["message"]["content"].strip()
    print(f"[pivot]  {out}   [via English: {english}]")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("text")
    p.add_argument("--to", default="English", help="target language name, e.g. English, Tamil, Hindi")
    p.add_argument("--pivot-to", default="Tamil", help="Indian target for the through-English example")
    a = p.parse_args()
    one(a.text, a.to)
    streamed(a.text, a.to)
    many([a.text, "कल सुबह दस बजे मैं आपको फ़ोन करूँगा।", "मेरी ट्रेन एक घंटा देर से चल रही है।"], a.to)
    pivot(a.text, a.pivot_to)
