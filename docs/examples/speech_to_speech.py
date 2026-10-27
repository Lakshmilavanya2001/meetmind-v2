"""The three services chained by hand: speech in one language -> speech in another.

    pip install requests numpy
    python speech_to_speech.py hindi.wav --from hi --to ta --out tamil.wav

ASR (IndicConformer on the Riva NIM, in pieces of at most 15 s) -> translate (indic-translate
NIM, each sentence straight into the target language) -> TTS (each sentence) -> one WAV.
For live audio, the LiveKit call stack in this repo does this in real time.
"""
import argparse
import io
import re
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from asr_transcribe import LANGUAGES as ASR_CODES, pieces, wav_bytes   # noqa: E402 (same folder)

ASR, TRANSLATE, TTS = "http://localhost:8001", "http://localhost:8002/v1", "http://localhost:8003"
# ISO code -> the target-language name indic-translate's prompt takes
NAMES = {"as": "Assamese", "bn": "Bengali", "brx": "Bodo", "doi": "Dogri", "en": "English", "gu": "Gujarati",
         "hi": "Hindi", "kn": "Kannada", "kok": "Konkani", "ks": "Kashmiri (Perso-Arabic script)",
         "mai": "Maithili", "ml": "Malayalam", "mni": "Manipuri (Meitei script)", "mr": "Marathi",
         "ne": "Nepali", "or": "Odia", "pa": "Punjabi", "sa": "Sanskrit", "sat": "Santali",
         "sd": "Sindhi (Devanagari script)", "ta": "Tamil", "te": "Telugu", "ur": "Urdu"}
DANDA = {"as", "bn", "brx", "doi", "hi", "mai", "ne", "or", "pa", "sa"}      # sentences end with "।"


def end_sentence(text, src):
    """ASR writes no punctuation, and without a sentence end the model often swaps names and
    places ("നാളെ പാലക്കാട് പോകാം" came back as "let's go to new york tomorrow")."""
    text = " ".join(text.split())
    if text and text[-1] not in ".?!।॥":
        text += "।" if src in DANDA else "."
    return text


def translate(text, tgt):
    """One request to the translate NIM's OpenAI-compatible chat API, in the model's prompt format."""
    body = {"model": "indic-translate", "temperature": 0, "stop": ["<turn|>"], "max_tokens": 2 * len(text) + 32,
            "messages": [{"role": "user", "content": f"Translate the following text into {NAMES[tgt]}:\n\n{text}"}]}
    r = requests.post(f"{TRANSLATE}/chat/completions", json=body, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


p = argparse.ArgumentParser()
p.add_argument("file")
p.add_argument("--from", dest="src", required=True, choices=sorted(ASR_CODES), help="spoken language of the file")
p.add_argument("--to", dest="tgt", required=True, choices=sorted(NAMES), help="language to speak the result in")
p.add_argument("--gender", default="female", choices=["female", "male"])
p.add_argument("--out", default="translated.wav")
a = p.parse_args()

t0 = time.perf_counter()
with wave.open(a.file) as w:
    sr = w.getframerate()
    audio = np.frombuffer(w.readframes(w.getnframes()), np.int16)
heard = []
for piece in pieces(audio, sr):                     # the ASR takes at most 16 s per request
    r = requests.post(f"{ASR}/v1/audio/transcriptions", data={"language": ASR_CODES[a.src]},
                      files={"file": ("audio.wav", wav_bytes(piece, sr), "audio/wav")}, timeout=60)
    r.raise_for_status()
    heard.append(r.json()["text"].strip())
asr = {"text": " ".join(heard)}
t1 = time.perf_counter()

# sentence by sentence, as the TTS model is trained on utterances; each one ended like a
# sentence and translated straight into the target language (one call, any two languages)
sentences = [x.strip() for x in re.split(r"(?<=[.!?।])\s+", asr["text"]) if x.strip()]
with ThreadPoolExecutor(8) as pool:
    translations = list(pool.map(lambda u: translate(end_sentence(u, a.src), a.tgt), sentences))
t2 = time.perf_counter()

pcm, sr = b"", 24000
for text in translations:
    r = requests.post(f"{TTS}/v1/tts", json={"text": text, "language": a.tgt, "gender": a.gender,
                                             "format": "wav", "sample_rate": sr}, timeout=300)
    r.raise_for_status()
    with wave.open(io.BytesIO(r.content)) as w:
        pcm += w.readframes(w.getnframes()) + b"\0\0" * (sr // 4)   # 250 ms gap between utterances
t3 = time.perf_counter()

with wave.open(a.out, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(pcm)

for src, dst in zip(sentences, translations):
    print(f"{src}\n  -> {dst}")
print(f"\nASR {(t1 - t0) * 1000:.0f} ms, translate {(t2 - t1) * 1000:.0f} ms, TTS {(t3 - t2) * 1000:.0f} ms "
      f"-> {a.out} ({len(pcm) / 2 / sr:.1f} s)")
