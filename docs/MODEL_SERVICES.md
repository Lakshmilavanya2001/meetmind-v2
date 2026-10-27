# Using the model services directly

This guide covers calling the three model services on their own: speech
recognition (ASR), translation and speech synthesis (TTS). Use it to build your
own pipeline, test a model in isolation, or integrate one service into another
product. The translated-call demo in this repo is one client of these APIs; you
don't need it to use them.

The models run in NVIDIA NIM containers: ASR is AI4Bharat's IndicConformer models
served by NVIDIA's Riva ASR NIM as custom models, translation runs on the
model-free LLM NIM, and the TTS model's speech LM runs on a second model-free LLM
NIM behind our own audio decoder.

Every example here was run against the live services. Runnable copies are in
[`docs/examples/`](examples/):

| File | Shows |
|---|---|
| [`asr_transcribe.py`](examples/asr_transcribe.py) | transcribe a file with the ASR NIM (HTTP), cutting files longer than 15 s at pauses |
| [`translate.py`](examples/translate.py) | one sentence, streamed (SSE), several at once, and Indian → Indian directly or, with `--pivot`, through English (OpenAI chat API) |
| [`tts.py`](examples/tts.py) | WAV over HTTP, streamed HTTP, streamed WebSocket |
| [`speech_to_speech.py`](examples/speech_to_speech.py) | all three chained: speech file → translated speech file |

Python examples need `pip install requests websockets numpy soundfile`. The
command-line tools `clients/asr_client.py` and `clients/translate_client.py`
also benchmark concurrency (`--bench`).

**Contents**

1. [At a glance](#1-at-a-glance)
2. [Reaching the services](#2-reaching-the-services)
3. [Languages](#3-languages)
4. [ASR — speech to text](#4-asr--speech-to-text)
5. [Translate — text to text](#5-translate--text-to-text)
6. [TTS — text to speech](#6-tts--text-to-speech)
7. [Chaining the three](#7-chaining-the-three)
8. [Errors and troubleshooting](#8-errors-and-troubleshooting)
9. [Performance and capacity](#9-performance-and-capacity)
10. [Configuration](#10-configuration)
11. [Licensing](#11-licensing)

---

## 1. At a glance

| | ASR | Translate | TTS |
|---|---|---|---|
| Model | AI4Bharat IndicConformer (120M Conformer-CTC, one per language) | `bodhan-ai/indic-translate` (Gemma 4 E4B) | `bodhan-ai/indic-speak` (Llama-3.2-3B + Vocos) |
| Runs in | NVIDIA Riva ASR NIM, custom models (the `whisper-large-v3:1.5.1` image as the Riva runtime) | NVIDIA model-free LLM NIM 2.0.13 | speech LM: model-free LLM NIM (`tts-lm`); audio decoding and API: our `tts` service |
| Base URL | `http://localhost:8001` (HTTP), `localhost:50051` (gRPC) | `http://localhost:8002/v1` (OpenAI-compatible) | `http://localhost:8003` |
| One-shot | `POST /v1/audio/transcriptions` | `POST /v1/chat/completions` | `POST /v1/tts` |
| Streaming | none: offline, one request per utterance of at most 16 s | `stream: true` (SSE): text deltas out | `WS /v1/stream` or chunked HTTP: audio out |
| Discovery | — (languages in [3](#3-languages)) | `GET /v1/models` | `GET /v1/voices` |
| Health | `GET /v1/health/ready` | `GET /v1/health/ready` | `GET /health` |
| Languages | 5 Indian: Malayalam, Hindi, Kannada, Telugu, Tamil | 22 Indian + English, directly between any two | 22 Indian + English |
| Typical latency | ~25 ms for a 6.5 s utterance | first text ~45 ms, sentence ~0.37 s | first audio ~0.38 s |

Conventions shared by all three:

- **JSON** for HTTP bodies and WebSocket text frames, UTF-8 throughout.
- **Audio** is mono. Send ASR a WAV file (16-bit PCM, 16 kHz), at most 16 s per
  request. TTS
  returns little-endian 16-bit PCM (`pcm_s16le`) unless you ask for 32-bit float
  (`pcm_f32le`).
- **WebSocket binary frames always carry audio** and text frames always carry
  JSON (TTS). Each JSON message has a `type`.
- **Times** are milliseconds (`*_ms`) or seconds (`*_s`).
- **No authentication.** The services listen on `127.0.0.1` only; see the next
  section.

## 2. Reaching the services

The services bind to `127.0.0.1` on the GPU host because they have no
authentication.

| Port | Service |
|---|---|
| 8001 | ASR, HTTP (the NIM's port 9000) |
| 50051 | ASR, Riva gRPC |
| 8002 | Translate, OpenAI API (the NIM's port 8000) |
| 8003 | TTS |
| 8004 | `tts-lm`, the TTS speech LM. The TTS service calls it; you don't need it |

**On the GPU host:** use these ports on `localhost` as in every example.

**From your laptop:** forward the ports over SSH, then use `localhost` exactly
the same way:

```bash
ssh -N -L 8001:localhost:8001 -L 8002:localhost:8002 -L 8003:localhost:8003 ubuntu@10.79.252.124
```

**From other machines or containers:** start the stack with `BIND_ADDR=0.0.0.0`
(`BIND_ADDR=0.0.0.0 docker compose up -d asr translate tts-lm tts`) only on a
trusted network or behind a proxy that adds authentication. Containers in the
same compose project use the container ports: `http://asr:9000`,
`http://translate:8000/v1` and `http://tts:8003`.

Check that all three are up. The NIMs answer `/v1/health/ready` with 200 once
the model is loaded (503 before that); the TTS service answers `/health` with
`200 {"status": "ok"}` once it is warmed up (503 before that). The ASR container
reports healthy in `docker compose ps` only after its healthcheck has sent one
warm-up request to each language's model (about 35 s after it starts).

```bash
curl -s localhost:8001/v1/health/ready; echo; curl -s localhost:8002/v1/health/ready; echo; curl -s localhost:8003/health
```

```
{"object":"health.response","message":"ready","status":"ready"}
{"object":"health.response","message":"ready","status":"ready"}
{"status":"ok"}
```

Start, stop and inspect them from the repo root:

```bash
docker compose up -d asr translate tts-lm tts
```

```bash
docker compose logs -f asr
```

## 3. Languages

| Code | Language | ASR (IndicConformer) | Translate (target name) | TTS voices (female / male) | TTS quality |
|---|---|:-:|---|---|---|
| `as` | Assamese | ✗ | `Assamese` | Prastuti / Ankur | production |
| `bn` | Bengali | ✗ | `Bengali` | Ishita / Sourav | production |
| `brx` | Bodo | ✗ | `Bodo` | Gwrbw / Sansuma | preview |
| `doi` | Dogri | ✗ | `Dogri` | Preeti / Sham | preview |
| `gu` | Gujarati | ✗ | `Gujarati` | Dhara / Parth | production |
| `hi` | Hindi | `hi-IN` | `Hindi` | Kavya / Amit | production |
| `kn` | Kannada | `kn-IN` | `Kannada` | Deepika / Adarsh | production |
| `kok` | Konkani | ✗ | `Konkani` | Anjali / Sandeep | preview |
| `ks` | Kashmiri | ✗ | `Kashmiri (Perso-Arabic script)` | Zoon / Ishfaq | preview |
| `mai` | Maithili | ✗ | `Maithili` | Vaidehi / Madhukar | preview |
| `ml` | Malayalam | `ml-IN` | `Malayalam` | Lakshmi / Kiran | production |
| `mni` | Manipuri | ✗ | `Manipuri (Meitei script)` | Thoibi / Chaoba | preview |
| `mr` | Marathi | ✗ | `Marathi` | Anagha / Chinmay | production |
| `ne` | Nepali | ✗ | `Nepali` | Srijana / Sagar | preview |
| `or` | Odia | ✗ | `Odia` | Itishree / Akash | production |
| `pa` | Punjabi | ✗ | `Punjabi` | Kaur / Manpreet | production |
| `sa` | Sanskrit | ✗ | `Sanskrit` | Bharati / Aryaman | preview |
| `sat` | Santali | ✗ | `Santali` | Phulmani / Sibu | preview |
| `sd` | Sindhi | ✗ | `Sindhi (Devanagari script)` | Moomal / Rano | preview |
| `ta` | Tamil | `ta-IN` | `Tamil` | Anitha / Arun | production |
| `te` | Telugu | `te-IN` | `Telugu` | Sravani / Vamsi | production |
| `ur` | Urdu | ✗ | `Urdu` | Saba / Zaid | production |
| `en` | English | ✗ | `English` | Kavya / Amit | production |

- **ASR** takes the full Riva language code shown (`ml-IN`). The code picks the
  model; there is no detection and no English. A plain `ml`, or a language with no
  model, returns `404 {"detail": "Model not found for language ml"}`. More
  languages can be added ([4.4](#44-adding-a-language)).
- **Translate** takes the English language name in its prompt (see
  [5.1](#51-the-request-format)). For Kashmiri, Manipuri and Sindhi the name spells
  out the script, and that selects the script the translation is written in. Santali
  comes out in Ol Chiki. `PROMPT_NAME` in `services/s2s/app/languages.py` maps the
  codes to these names.
- **TTS** takes the short code or a FLORES code (`tam_Taml`).

---

## 4. ASR — speech to text

AI4Bharat's IndicConformer: one 120M-parameter Conformer-CTC model per language,
trained on Indian speech. Five are deployed: Malayalam, Hindi, Kannada, Telugu and
Tamil. They run in NVIDIA's Riva ASR NIM as custom models, converted the way NVIDIA
documents it (nemo2riva, riva-build, riva-deploy; see [4.4](#44-adding-a-language)).
The deployment is **offline**: each request transcribes a finished piece of audio of
at most 16 s. There is no streaming endpoint and no partial results. Output is
unpunctuated text in the language's script.

Character error rate on synthesized test sentences (3–5 per language, read by the
TTS service). Those are cleaner than real calls, so expect higher error rates in
practice; the comparison between the two models is the useful part:

| Language | IndicConformer on Riva | Whisper Large v3 NIM (the previous ASR) |
|---|---|---|
| Malayalam | 0.0% | 91.7% (it wrote Malayalam in Gurmukhi script) |
| Hindi | 0.0% | 16.5% |
| Kannada | 0.0% | 10.7% |
| Telugu | 1.0% | 17.3% |
| Tamil | 0.7% | 8.1% |

Riva's output is identical to the same models run in NeMo.

### 4.1 Transcribe — `POST /v1/audio/transcriptions`

A multipart form:

| Field | Meaning |
|---|---|
| `file` | the audio: WAV, 16-bit PCM, mono, 16 kHz; **at most 16 s** |
| `language` | `ml-IN`, `hi-IN`, `kn-IN`, `te-IN` or `ta-IN` |

```bash
curl -s localhost:8001/v1/audio/transcriptions -F language=ta-IN -F file=@tamil.wav
```

```json
{"text": "வணக்கம் இன்று நாம் கணித வகுப்பைத் தொடங்குகிறோம் தயவு செயய்து உங்கள் புத்தகங்களைத் திறந்து முதல் பாடத்தைப் படியுங்கள் "}
```

The text can end with a space; strip it.

**At most 16 s per request.** The models read a whole utterance in one pass (Riva's
offline mode with a single 16 s window; Riva's chunked mode dropped the first word
of utterances in Tamil, Telugu and Kannada). A longer request crashes Riva's
inference server: 15.5 s worked, 17 s took it down. The container's healthcheck
then restarts it (back in 55 s when tested), but every caller is without ASR in
the meantime. Cut longer audio at pauses before sending it.

Python ([`examples/asr_transcribe.py`](examples/asr_transcribe.py)) does that cut:
files longer than 15 s are split at the quietest 30 ms before each 15 s mark.

```bash
python docs/examples/asr_transcribe.py tamil.wav --language ta
```

```
வணக்கம் இன்று நாம் கணித வகுப்பைத் தொடங்குகிறோம் தயவு செயய்து உங்கள் புத்தகங்களைத் திறந்து முதல் பாடத்தைப் படியுங்கள்

9.3 s of audio in 1 request(s), 20 ms
```

A 46.5 s file went through as `46.5 s of audio in 4 request(s), 85 ms`.
`clients/asr_client.py` (single file and `--bench`) refuses files over 15.5 s.

The NIM also serves the Riva gRPC API on port 50051, the one NVIDIA's
`nvidia-riva-client` package uses. This repo uses HTTP only; the gRPC path
hasn't been tested here.

### 4.2 Live audio

With no streaming endpoint, cut live audio into utterances yourself and send each
one when it ends. The call agent does it this way
(`services/s2s/app/asr.py`, `vad.py`):

1. A voice activity detector (Silero VAD, ONNX on CPU) runs on the 16 kHz mic
   audio, in 32 ms frames.
2. An utterance starts after 200 ms of speech and ends after a 500 ms pause.
   200 ms of audio is kept on either side, and a monologue is cut every 15 s, so no
   request exceeds the 16 s limit.
3. Each finished utterance goes to `/v1/audio/transcriptions` as a WAV, with the
   speaker's language code. Finals are passed on in utterance order.
4. For live captions, the utterance so far is re-transcribed every 0.5 s
   (`S2S_ASR_PARTIALS=1`, on by default). A request takes ~25 ms and the NIM
   batches concurrent ones, so captions don't hold up the final.
5. Room echo: the agent cancels the translated speech it plays out of each mic
   (WebRTC AEC3), but sends the cancelled audio to ASR only while playout is
   actually leaking into that mic (a strong correlation in the last 20 s).
   Otherwise ASR gets the untouched mic: AEC3 turned down the start of speech that
   overlapped playback, and IndicConformer then dropped the first word.

Measured in the two-caller test, the final transcript arrives ~0.50 s after the
speaker stops: that is the 0.5 s VAD pause; ASR adds ~20 ms.

### 4.3 Tips

- **Use the full language code** (`hi-IN`). The code selects the model.
- **Keep each request under 16 s.** Cut at pauses; see 4.1.
- **Gate it with a VAD**, as the call agent does: send speech, not long silences.
- **Concurrency is fine.** Each model batches up to 4 requests; 16 sent at once
  finish in ~0.2 s (p50). See [9](#9-performance-and-capacity).
- **The first request after a start is slower.** The healthcheck sends one to each
  language's model before the container reports healthy (~35 s after start).

### 4.4 Adding a language

The other 17 Indian languages have IndicConformer checkpoints too
(`ai4bharat/indicconformer_stt_<lang>_hybrid_ctc_rnnt_large`). Each is gated on
Hugging Face:

1. Accept the checkpoint's terms on Hugging Face with the account whose `HF_TOKEN`
   is in `.env`.
2. Build all the languages you want, in one run (it makes one package):

   ```bash
   services/asr-nim/build/build.sh ml hi kn te ta bn
   ```

3. Restart the ASR service (`docker compose up -d asr`). Add the language to
   `ASR_LANGUAGES` in `services/s2s/app/languages.py` (and rebuild `s2s-agent`) and
   to `ASR_WARM_LANGUAGES` in the healthcheck.

What the build does, per language: `make_ctc.py` turns the checkpoint (a hybrid
RNNT/CTC model with output heads for all 22 languages) into a plain NeMo CTC model
with that language's 256 tokens; NVIDIA's `nemo2riva` exports it; `riva-build`
makes an offline, single-window pipeline with NeMo's own mel features and an fp32
TensorRT engine (the fp16 engine of this model outputs only blanks); `riva-deploy`
writes the model repository, packed as `nim-export/asr-indic/custom_model.tar.gz`,
which the NIM loads at start (`NIM_EXPORT_PATH`). It needs the GPU and about 2 GB
of disk per language while it runs. The five deployed languages use 10–12 GB of GPU
memory together.

---

## 5. Translate — text to text

Indic Translate (`bodhan-ai/indic-translate`) is a Gemma 4 E4B model fine-tuned by
Bodhan AI for the 22 scheduled Indian languages and English. It translates
**directly between any two of them**, Indian ↔ Indian included, in one call.
Going through English is optional ([5.4](#54-many-at-once-and-indian--indian)).

The service is NVIDIA's model-free LLM NIM 2.0.13 (vLLM 0.28) serving the local
checkpoint. It speaks the **OpenAI chat completions API**, so any OpenAI client
works.

### 5.1 The request format

From the model's prompt contract
(`models/indic-translate/scripts/prompt_contract.py`):

- **Exactly one user message:** the instruction, a blank line, then the text:
  `"Translate the following text into {Target}:\n\n{text}"`.
- **No system message.** One changes the prompt the model was trained on; the
  output still reads fluently but is measurably worse.
- **Name only the target language.** The model infers the source.
- **Greedy decoding:** `temperature: 0`, with `stop: ["<turn|>"]` (the model's
  end-of-turn marker) so the reply can't run on past the translation.
- **`model: "indic-translate"`** (from `GET /v1/models`).

Target names are the names in the Translate column of the table in
[3](#3-languages): `English`, `Hindi`, `Tamil`, `Manipuri (Meitei script)`, …
`Hindi (Roman)` and `Hindi (code-mixed)` work too: "I will call you tomorrow at
ten in the morning." came back as "Main aapko kal subah das baje phone karunga."
and "मैं तुम्हें कल सुबह ten बजे call करूँगा।".

Text from speech recognition needs an end mark first; see [5.5](#55-tips).

### 5.2 One request — `POST /v1/chat/completions`

```bash
curl -s localhost:8002/v1/chat/completions -H 'content-type: application/json' -d '{"model": "indic-translate", "temperature": 0, "stop": ["<turn|>"], "max_tokens": 64, "messages": [{"role": "user", "content": "Translate the following text into Tamil:\n\nHow are you?"}]}'
```

```json
{"id": "chatcmpl-af1610e07f740952", "object": "chat.completion", "model": "indic-translate",
 "choices": [{"index": 0, "message": {"role": "assistant", "content": "எப்படி இருக்கிறீர்கள்?"},
              "finish_reason": "stop"}],
 "usage": {"prompt_tokens": 21, "completion_tokens": 7, "total_tokens": 28}}
```

(trimmed; the server adds more fields, mostly `null`)

- The translation is `choices[0].message.content`.
- `finish_reason` is `length` if `max_tokens` cut the translation short. Indian
  scripts can take 2–3× the tokens of the source (Manipuri in Meitei script: 97
  tokens for a 48-character English sentence); the examples cap generation at
  `2 × characters + 32`.

With the OpenAI Python SDK (`pip install openai`), here straight from Hindi to
Kannada:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8002/v1", api_key="unused")
stream = client.chat.completions.create(
    model="indic-translate", temperature=0, stop=["<turn|>"], max_tokens=128, stream=True,
    messages=[{"role": "user", "content": "Translate the following text into Kannada:\n\n"
                                          "मैं कल सुबह दस बजे आपको फ़ोन करूँगा।"}],
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

```
ನಾನು ನಾಳೆ ಬೆಳಿಗ್ಗೆ ಹತ್ತು ಗಂಟೆಗೆ ನಿಮಗೆ ಫೋನ್ ಮಾಡುತ್ತೇನೆ.
```

### 5.3 Streamed (Server-Sent Events)

Add `"stream": true`. The response is `text/event-stream`, one JSON chunk per
`data:` line, and ends with `data: [DONE]`:

```
data: {"id": "chatcmpl-a541...", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, ...}]}
data: {"id": "chatcmpl-a541...", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": ""}, ...}]}
data: {"id": "chatcmpl-a541...", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "న"}, ...}]}
data: {"id": "chatcmpl-a541...", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "మస్తే"}, ...}]}
...
data: {"id": "chatcmpl-a541...", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "్నారు?"}, "finish_reason": "stop", ...}]}
data: [DONE]
```

Concatenate `choices[0].delta.content` (the first few chunks carry an empty
string). The call agent shows these pieces as live captions.

### 5.4 Many at once, and Indian → Indian

There is no batch endpoint. Send requests **concurrently**: the server batches
them on the GPU, so 8 parallel requests take about as long as one
([`examples/translate.py`](examples/translate.py)):

```bash
python docs/examples/translate.py "नमस्ते, आप कैसे हैं?" --pivot
```

```
[one]    Hello, how are you?   (7 tokens, finish_reason stop)
[stream] Hello, how are you?
[many]   नमस्ते, आप कैसे हैं?  ->  Hello, how are you?
[many]   कल सुबह दस बजे मैं आपको फ़ोन करूँगा।  ->  I will call you tomorrow at ten in the morning.
[many]   मेरी ट्रेन एक घंटा देर से चल रही है।  ->  My train is running an hour late.
[direct] வணக்கம், நீங்கள் எப்படி இருக்கிறீர்கள்?
[pivot]  வணக்கம், எப்படி இருக்கிறீர்கள்?   [via English: Hello, how are you?]
```

Between two Indian languages, translate **directly**. Going through English
works too, but costs a second call and can lose the register:

| | Direct (default) | Through English (optional) |
|---|---|---|
| Path | source → target, one call | source → English → target, two calls |
| Latency (one 7–12-word sentence, p50) | 0.26 s | 0.45 s |
| Register | kept: Hindi "क्या आप मेरे साथ चलेंगे?" stayed formal in Kannada ("ನೀವು … ಬರ್ತೀರಾ?") | can flatten: through "will you go with me?" it became the informal "ನೀನು … ಬರ್ತೀಯಾ?" |

The call agent translates directly. With `S2S_PIVOT_THROUGH_ENGLISH=1` in its
environment it goes through English instead, and one English step per sentence
serves every listener language (`route()` in `services/s2s/app/agent.py`).

On a live Malayalam ↔ Kannada call, replayed (36 utterances that could be
judged), indic-translate, direct, got 15 right, 14 partly right and 7 wrong, and
made nothing up. sarvam-translate, the model it replaced, through English, got
15 / 8 / 13, with 6 made-up outputs or commentary instead of a translation. The
model card scores English ↔ Indian only; judge it on your own language pairs.

### 5.5 Tips

- **End speech-recognition text like a sentence.** ASR output has no
  punctuation, and without a sentence end the model often swaps names and
  places: "നാളെ പാലക്കാട് പോകാം" came back in English as "let's go to new york
  tomorrow", and with a trailing "." as "Let's go to Palakkad tomorrow." Into Hindi, Delhi,
  Mumbai and Shimla showed up the same way. Add "।" for Assamese, Bengali, Bodo,
  Dogri, Hindi, Maithili, Nepali, Odia, Punjabi and Sanskrit (`as bn brx doi hi
  mai ne or pa sa`) and "." for the other languages, unless the text already
  ends in `.`, `?`, `!`, `।` or `॥`. The call agent does this in
  `TranslateClient.sentence` (`services/s2s/app/agent.py`).
- **Respell words left in the source script.** About 5% of Indian → Indian
  outputs keep a word, often a name, in the source script. Kannada "ನಾನು ನಾಳೆ
  ಮೈಸೂರಿಗೆ ಹೋಗುತ್ತೇನೆ." came back in Malayalam as "ഞാൻ നാളെ മൈസൂರಿಗೆ
  പോകുന്നു." Unicode lays out the Brahmic scripts (Devanagari to Malayalam)
  letter for letter at the same offsets, so the call agent respells those
  letters in the target script (`TranslateClient.respell`); here it gives
  "മൈസൂരിഗെ".
- **Send sentences or short paragraphs.** The server runs with a 4,096-token
  window (prompt + output), although the model is validated up to 32,768. Raise
  `--max-model-len` in `docker-compose.yml` for longer documents.
- **Don't name the source language** in the text, and don't add a system
  message.
- **Translation is greedy**, so the same input gives the same output.
- **`GET /v1/metrics`** exposes vLLM's Prometheus metrics (queue length, KV-cache
  use, token counts).

---

## 6. TTS — text to speech

Indic Speak: a Llama-3.2-3B speech language model emits SNAC audio codes, and a
fine-tuned Vocos vocoder turns them into 24 kHz audio. There are about 50
studio voices; any voice can read any language, best in its native one. It
reads numbers, units and English words inside Indian-language sentences in the
surrounding voice.

**How it runs.** The speech LM runs in NVIDIA's model-free LLM NIM (`tts-lm`,
port 8004): the TTS service sends it the prompt as token ids on the OpenAI
completions API and streams the generated audio-token ids back
(`return_token_ids`). Turning those tokens into sound is not something a NIM
does, so the TTS service (`indic-tts-decoder` image: Python + CUDA torch) decodes
SNAC codes with Vocos on the GPU in streamed windows and serves the API below.
You only talk to the TTS service.

### 6.1 One request — `POST /v1/tts`

| Field | Default | Meaning |
|---|---|---|
| `text` | (required) | what to say, any supported language or a mix |
| `language` | — | picks the default voice for that language (`ta`, `tam_Taml`). The model reads any script regardless |
| `gender` | `female` | `female` / `male` default voice for `language` |
| `speaker` | — | a voice by name (`Arun`, `Kavya`, … from `/v1/voices`); overrides `language`/`gender`. An unknown name doesn't error, it just sounds generic |
| `style` | `""` | delivery hint: `news`, `Customer Care`, `educational lecture`, `happy`, `sad`, … (preview quality) |
| `format` | `wav` | `wav`, `pcm_s16le` or `pcm_f32le` |
| `sample_rate` | `24000` | `8000`, `16000`, `22050`, `24000` (native), `44100`, `48000` |
| `stream` | `false` | `true`: chunked response while generating |
| `temperature`, `top_p`, `top_k`, `repetition_penalty` | 0.6, 0.9, 50, 1.2 | sampling (the model card's production settings) |
| `seed` | random | fixes sampling so the same text and seed sound identical |
| `max_seconds` | auto | cap per sentence chunk (≤ 60). Default: characters ÷ 9 + 1.2 s, ≤ 30 s (no normal clip we measured spoke slower; `TTS_MIN_CHARS_PER_S` changes the 9) |

```bash
curl -s localhost:8003/v1/tts -H 'content-type: application/json' -d '{"text": "வணக்கம், எப்படி இருக்கிறீர்கள்?", "language": "ta", "gender": "male"}' -o hello.wav
```

Non-streamed responses carry these headers:

| Header | Meaning |
|---|---|
| `X-Speaker` | voice used |
| `X-Sample-Rate` | output rate |
| `X-Audio-Seconds` | audio length |
| `X-Latency-Ms` | total synthesis time |
| `X-TTFA-Ms` | time until the first audio was ready |
| `X-Tokens` | speech tokens generated (82 ≈ 1 s) |

**Streamed HTTP** (`"stream": true`) returns audio as it's generated:
- with `format: pcm_s16le` / `pcm_f32le`, raw samples;
- with `format: wav`, a WAV header with unknown length (players read to the
  end), then samples.

The response carries `X-Sample-Rate` and `X-Speaker` headers. First audio
arrives after ~0.4 s.

```python
import requests
body = {"text": "नमस्ते, आज मौसम अच्छा है।", "language": "hi", "format": "pcm_s16le", "sample_rate": 16000, "stream": True}
with requests.post("http://localhost:8003/v1/tts", json=body, stream=True) as r:
    for chunk in r.iter_content(chunk_size=None):
        player.write(chunk)          # 16-bit mono PCM at 16 kHz, playable immediately
```

All three ways, from [`examples/tts.py`](examples/tts.py):

```bash
python docs/examples/tts.py "வணக்கம், எப்படி இருக்கிறீர்கள்?" --language ta --out hello_ta.wav
```

```
[http]   1.963 s of audio by Anitha in 1694.4 ms -> hello_ta.wav_http.wav
[stream] first audio after 384 ms, 2.30 s total -> hello_ta.wav_http_stream.wav
[ws]     speaker Anitha, 24000 Hz pcm_s16le
[ws]     first audio after 371 ms, 2.22 s audio, real-time factor 0.863 -> hello_ta.wav_ws.wav
```

### 6.2 WebSocket — `WS /v1/stream`

Keep one connection per output channel, such as one per listener in a call.
Requests on a socket are synthesized **one at a time, in order**, so a sequence
of sentences plays in sequence.

| Client message | Meaning |
|---|---|
| `{"type": "synthesize", "id": "u1", "text": "...", "language": "ta", ...}` | queue a request; same fields as HTTP except `stream` (`format` is `pcm_s16le` or `pcm_f32le`) |
| `{"type": "cancel"}` | **barge-in**: stop the current request and drop all queued ones |
| `{"type": "cancel", "id": "u1"}` | cancel one request. The current one answers `cancelled`; a queued one is dropped without a message, so it never gets `start` or `done` |

| Server message | Fields |
|---|---|
| `ready` | `sample_rate` (native, 24000) |
| `queued` | `id`, `position` |
| `start` | `id`, `sample_rate`, `encoding`, `speaker`: binary audio frames for this request follow |
| *binary frames* | audio of the most recent `start` |
| `done` | `id`, `audio_ms`, `ttfa_ms`, `latency_ms`, `rtf` (generation time ÷ audio length), `tokens`, `chunks` |
| `cancelled` | `id`: the request that was being spoken stopped (queued requests dropped by a cancel send nothing) |
| `error` | `message`, `id` if it belongs to a request |

```python
import asyncio, json, websockets

async def speak(texts):
    async with websockets.connect("ws://localhost:8003/v1/stream", max_size=None) as ws:
        await ws.recv()                                             # ready
        for i, t in enumerate(texts):
            await ws.send(json.dumps({"type": "synthesize", "id": str(i), "text": t,
                                      "language": "hi", "sample_rate": 16000}))
        finished = 0
        async for msg in ws:
            if isinstance(msg, bytes):
                player.write(msg)                                   # PCM16, 16 kHz, mono
            elif json.loads(msg)["type"] in ("done", "error", "cancelled"):
                finished += 1
                if finished == len(texts):
                    break
```

In a browser, decode each binary chunk to `Float32` and schedule it to start
right when the previous one ends, so playback is gapless.

### 6.3 How streaming works, and what it means for you

- **Time to first audio is ~0.38 s.** Each audio chunk is decoded with 341 ms of
  lookahead, so streamed audio is effectively identical to a one-shot decode
  (≈ 77 dB SNR, no clicks between chunks).
- **Chunks start at 85 ms and grow up to 341 ms.** The server tracks how much
  audio your player has buffered and sends single frames while the buffer is
  low. Start playback as soon as the first chunk arrives.
- **Generation runs ~1.2× faster than real time** for a single stream (real-time
  factor 0.84), rising to RTF ~1.0 at 4–8 concurrent streams. Above that, audio
  arrives slower than it plays (see [9](#9-performance-and-capacity)).

### 6.4 Tips

- **Punctuate.** The model takes pauses and intonation from punctuation; put
  commas where a speaker would breathe.
- **Long text is fine.** It's split into chunks of ≤ 300 characters at sentence
  boundaries and spoken back to back. For conversational use, send sentence
  by sentence anyway: first audio arrives sooner.
- **Numbers, units and English terms** are read in the surrounding language, so
  you needn't spell them out.
- **Telephony:** request `sample_rate: 8000` instead of downsampling yourself.
  The server's resampler is streaming-safe.
- **Repeatability:** set `seed` for identical audio across calls. Without it,
  every rendition varies slightly.

---

## 7. Chaining the three

The shortest end-to-end path, file in and file out
([`examples/speech_to_speech.py`](examples/speech_to_speech.py)): the file
through ASR in pieces of at most 15 s, the text split into sentences, each given
an end mark (`end_sentence`) and translated straight into the target language,
then each spoken.

```bash
python docs/examples/speech_to_speech.py hindi.wav --from hi --to te --out telugu.wav
```

```
नमस्ते आज हम गणित की कक्षा सुरू करेंगे कृपया अपनी पुस्तके खोलिए और पहला पाठ पढ़िय
  -> నమస్తే, ఈ రోజు మనం గణిత తరగతిని ప్రారంభిద్దాం. దయచేసి మీ పుస్తకాలు తెరిచి మొదటి పాఠం చదవండి.

ASR 21 ms, translate 526 ms, TTS 7126 ms -> telugu.wav (8.7 s)
```

The ASR output has no punctuation, so the sentence split finds one sentence here,
and `end_sentence` adds a "।" before it goes to translate.

For live audio, wire them together per utterance:

1. Cut the mic audio into utterances of at most 15 s with a VAD
   ([4.2](#42-live-audio)) and send each finished one to ASR.
2. End the transcript with "।" or "." ([5.5](#55-tips)) and send it to translate
   with `stream: true`, one call straight into the listener's language. You can
   show the streamed `delta.content` pieces as captions. Respell any word left in
   the source script ([5.5](#55-tips)).
3. When the stream ends (`data: [DONE]`), send the full translation to TTS as a
   `synthesize` request, once per language even if several listeners hear it, and
   copy the audio to each of them. Each listener plays utterances in order.
4. Play TTS binary frames as they arrive.
5. Optionally, when the VAD hears the listener start talking, send TTS
   `{"type": "cancel"}` to stop playback (barge-in).

`services/s2s/app/agent.py` does this for calls of any size. In the two-caller test
with indic-translate (direct), the listener heard the translation 1.39 s (Hindi →
English) and 1.59 s (Hindi → Tamil) after the speaker stopped; for Tamil → Hindi,
speech started playing at 1.05–1.10 s. For Hindi → Tamil, the final transcript came at
0.52 s (the VAD pause), the translation was done at 0.96 s and speech started at
1.35 s. In a three-person call every listener heard theirs within 1.55–1.88 s. With
sarvam-translate through English the same turns took 1.33 s and 1.83 s.

---

## 8. Errors and troubleshooting

**HTTP status codes**

| Code | Meaning |
|---|---|
| `400` | TTS: a bad value (`sample_rate must be one of …`, empty text). Translate: a malformed chat request, or prompt + `max_tokens` over the 4,096-token window |
| `404` | ASR: no model for that language code (`Model not found for language ml`): use one of the five full codes (`ml-IN`) |
| `422` | malformed request body (missing field, wrong type); `detail` says which |
| `500` | ASR: the request had more than 16 s of audio. It crashed Riva's server, and all ASR requests fail (`Socket closed`, `failed to connect`) until the container restarts itself, about a minute later |
| `502` / `503` | model still loading after a restart; retry once `/v1/health/ready` (TTS: `/health`) returns 200 |

**WebSocket close codes** (TTS)

| Code | Meaning |
|---|---|
| `1013` | model still loading; reconnect after `/health` is 200 |

Errors inside an open TTS stream arrive as `{"type": "error", ...}` messages, and
the connection stays usable.

**Common problems**

| Symptom | Likely cause |
|---|---|
| ASR returns 500, then every ASR request fails for a minute | a request over 16 s; cut audio at pauses ([4.1](#41-transcribe--post-v1audiotranscriptions)) |
| ASR returns 404 | a plain or unknown language code; use `ml-IN`, `hi-IN`, `kn-IN`, `te-IN` or `ta-IN` |
| First word of utterances missing in calls | echo cancellation turning down speech that overlaps playback; the agent applies it only while playout leaks into the mic ([4.2](#42-live-audio)) |
| Translation in the wrong language | the prompt names the wrong target or names the source too, or has a system message; use the format in [5.1](#51-the-request-format) and the exact name from [3](#3-languages) |
| Names or places swapped (Palakkad → New York) | the text has no sentence end; add "।" or "." ([5.5](#55-tips)) |
| A word in the translation is in another script | the model kept a source word; respell it ([5.5](#55-tips)) |
| Translation cut off | `finish_reason: "length"`: raise `max_tokens` or split the text |
| TTS audio has gaps when many play at once | more than ~6–8 concurrent voices; see capacity below |
| TTS voice sounds generic | `speaker` name not in `/v1/voices` |
| First request after a restart is slow | the service was not healthy yet; wait for the health check |
| Connection refused from another machine | services bind to localhost; use the SSH tunnel in [2](#2-reaching-the-services) |

Logs: `docker compose logs -f asr|translate|tts-lm|tts`.

---

## 9. Performance and capacity

Measured on one NVIDIA L40S running all of them together:

| Service | Single request | Under load |
|---|---|---|
| ASR | 6.5 s utterance in ~25 ms | 4 / 8 / 16 concurrent: p50 61 / 105 / 194 ms; ~43 utterances/s (the Whisper NIM managed 2.6) |
| Translate (Indian → Indian, direct, 10–15-word sentences) | first text 45 ms, sentence 0.37 s | 8 concurrent: sentence 0.26 s, 19.5 sentences/s; 32: 0.32 s, 67.6 sentences/s; 64: 0.43 s, 104 sentences/s |
| TTS | first audio 0.38 s, RTF 0.84 | 4 voices: first audio 0.42 s, RTF 0.96, no stalls; 8 voices: RTF 1.05, stalls up to 0.4 s |

**TTS sets the capacity:** about 6–7 simultaneous real-time voices. ASR (~43
utterances per second) and translation have large headroom.

GPU memory (46 GB L40S): translate NIM 16.9 GB, ASR NIM (five languages) 10.2 GB,
`tts-lm` NIM 12.4 GB, TTS decoder 0.9 GB: 40.3 GB in all.

Re-run the benchmarks with `clients/asr_client.py --language hi-IN --bench`,
`clients/translate_client.py --bench` and `clients/tts_stream.py --bench` (see the
main README).

## 10. Configuration

Set in `docker-compose.yml`, then apply with `docker compose up -d <service>`.
The LLM NIMs take vLLM flags in the service's `command:` list, passed straight
through.

| Service | Setting | Default | Effect |
|---|---|---|---|
| ASR | `NIM_DISABLE_MODEL_DOWNLOAD` | `true` | serve the custom models instead of downloading the image's own (Whisper) |
| ASR | `NIM_EXPORT_PATH` | `/opt/nim/export` (`./nim-export/asr-indic`) | where the NIM finds `custom_model.tar.gz`, built by `services/asr-nim/build/build.sh` |
| ASR | `ASR_WARM_LANGUAGES` | `ml-IN hi-IN kn-IN te-IN ta-IN` | models the healthcheck warms before reporting healthy |
| Translate | `--kv-cache-memory-bytes` | `3221225472` | KV cache (3 GiB, 24,111 tokens). The NIM's vLLM reserves too little at a GPU share, so it is pinned |
| Translate | `--max-model-len` | `4096` | prompt + output tokens per request |
| Translate | `--max-num-seqs` | `64` | concurrent sequences |
| Translate | `--max-num-batched-tokens` | `1024` | prefill chunk size. Live requests are short, and the activation peak (logits over a 262k-token vocabulary) is what fills the memory budget |
| Translate | `--enable-prefix-caching`, `--language-model-only`, `--trust-remote-code` | set | prefix caching reuses the KV cache of repeated prompt prefixes; `--language-model-only` loads only the text model, not the vision and audio towers |
| `tts-lm` | `--kv-cache-memory-bytes` | `5368709120` | KV cache (5 GiB, 46,800 tokens) |
| `tts-lm` | `--max-model-len` | `4096` | must match `MAX_MODEL_LEN` of the TTS service |
| `tts-lm` | `--max-num-seqs` | `32` | concurrent voices |
| TTS | `TTS_LM_URL` | `http://tts-lm:8000/v1` | where the speech LM runs |
| TTS | `TTS_CONTEXT_RIGHT` | `4` | vocoder lookahead frames (2 = ~140 ms faster, small artifacts) |
| TTS | `TTS_REPETITION_PENALTY`, `TTS_TEMPERATURE`, `TTS_TOP_P`, `TTS_TOP_K` | 1.2, 0.6, 0.9, 50 | default sampling |
| TTS | `TTS_MAX_CHUNK_CHARS` | `300` | long-text chunk size |
| Call agent | `S2S_ASR_PARTIALS` | `1` | live captions of the speaker's words (re-transcribes the utterance so far) |
| Call agent | `S2S_ASR_PARTIAL_INTERVAL_S` | `0.5` | how often captions are refreshed |
| Call agent | `S2S_VAD_MIN_SILENCE_MS` | `500` | pause that ends an utterance |
| Call agent | `S2S_PIVOT_THROUGH_ENGLISH` | `0` | `1`: between two Indian languages, translate through English (two calls; the English step is shared by every listener language) |
| all | `BIND_ADDR` | `127.0.0.1` | host address the ports bind to |
| LLM NIMs | `LOCAL_NIM_CACHE` | `./nim-cache` | where the translate and `tts-lm` NIMs keep compile caches (`<dir>/translate`, `<dir>/tts-lm`; writable by uid 1000) |

## 11. Licensing

- **The NIM containers** (the Riva ASR NIM image used as the runtime for the ASR
  models, and the model-free LLM NIM) are NVIDIA software. Pulling them needs an
  NGC key, and production use needs an NVIDIA AI Enterprise licence. The ASR NIM
  container is governed by the NVIDIA AI Product Agreement.
- **IndicConformer** checkpoints are MIT licensed (AI4Bharat's model cards).
- **indic-translate** and **indic-speak** are under the **Indic Open Model
  License v1.0** (Bodhan AI / AI4Bharat):
  - Self-hosting them for your own product is free, with attribution ("Built
    with … from Bodhan AI / AI4Bharat").
  - Exposing them as an API that **other companies call directly** needs Bodhan
    AI's written approval, unless you are a nonprofit, government or academic
    user.
  - The license forbids robocalls, voice phishing, and voice clones or deepfakes
    of real people without consent.
- indic-translate is built on Gemma 4 E4B, and indic-speak uses Llama 3.2 and
  SNAC; their licenses apply too.
