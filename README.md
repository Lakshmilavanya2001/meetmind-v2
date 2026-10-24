# Indic speech model services

A real-time Indic speech-to-speech call translator. Speech recognition, translation
and speech synthesis models run on one GPU, chained by a call orchestrator built on
self-hosted LiveKit: everyone on a call speaks their own language and hears the others
in the language they choose (tested with two and three people). See
[Translated calls](#translated-calls-livekit) for the call stack.

The models run in **NVIDIA NIM containers** wherever NVIDIA supports them. The one
piece no NIM covers, turning indic-speak's audio tokens into sound, is our own
container.

**Architecture diagram:** [docs/architecture.html](docs/architecture.html) (open it in a
browser; also published at https://claude.ai/artifact/PUtZvT23wvBJFamux4WsYE). It shows
every component and what flows between them, and a to-scale timeline of one sentence from
the speaker stopping to the listener hearing the translation.

| Service | Model | Port | Runtime | API |
|---|---|---|---|---|
| `asr` | AI4Bharat IndicConformer, one 120M Conformer-CTC per language ([`ai4bharat/indicconformer_stt_<lang>_hybrid_ctc_rnnt_large`](https://huggingface.co/ai4bharat/indicconformer_stt_ml_hybrid_ctc_rnnt_large)): Malayalam, Hindi, Kannada, Telugu, Tamil | 8001 (HTTP), 50051 (gRPC) | NVIDIA Riva ASR NIM, custom models (nemo2riva → riva-build, TensorRT) | `POST /v1/audio/transcriptions`, one request per utterance of ≤16 s |
| `translate` | [`sarvamai/sarvam-translate`](https://huggingface.co/sarvamai/sarvam-translate) (Gemma 3 4B, 22 Indian languages + English) | 8002 | NVIDIA model-free LLM NIM 2.0.13 (vLLM 0.28) | OpenAI chat completions, SSE: text deltas out |
| `tts-lm` | [`bodhan-ai/indic-speak`](https://huggingface.co/bodhan-ai/indic-speak)'s speech LM (Llama-3.2-3B emitting audio tokens) | 8004 | NVIDIA model-free LLM NIM 2.0.13 | OpenAI completions, token ids in and out |
| `tts` | indic-speak's audio decoder (SNAC + Vocos) and the TTS API (22 Indian languages + English) | 8003 | our image: Python + CUDA torch 2.13 | WebSocket / chunked HTTP: PCM out |

All four share one NVIDIA L40S (46 GB) and use 36.3 GB together. On disk, the
Riva NIM image takes 37 GB (it is the Whisper Large v3 NIM image, used only as the Riva
runtime) and the model-free NIM image 43.8 GB (one copy, used by both `translate` and
`tts-lm`).

History, all on 2026-10-24: the first setup ran IndicConformer in our own ONNX Runtime
container and Bodhan AI indic-translate (code in
`archive/previous-setup-2026-10-24.tar.gz`; [docs/NIM_POC.md](docs/NIM_POC.md) showed the
model-free NIM serves a local checkpoint unchanged). Then ASR moved to NVIDIA's Whisper
Large v3 NIM, which wrote Malayalam in Gurmukhi script (~92% character errors), so
IndicConformer came back, this time inside NVIDIA's Riva NIM.

```
indicspeechModels/
├── docker-compose.yml        # model services + call stack, started in order
├── models/                   # weights (mounted read-only at /models): sarvam-translate, indic-speak, snac_24khz
├── nim-cache/                # the LLM NIMs' caches (compile caches, runtime manifests)
├── nim-export/asr-indic/     # the ASR NIM's model repository (custom_model.tar.gz, built)
├── services/tts/             # indic-speak audio decoder + TTS API (Dockerfile + app/)
├── services/asr-nim/         # ASR NIM healthcheck (warm-up, self-restart) + build/ (IndicConformer → Riva)
├── services/translate-nim/   # healthcheck + warm-up for the translate NIM
├── services/s2s/             # call orchestrator: LiveKit agent worker (incl. VAD) + demo web app
├── services/{livekit,proxy}/ # LiveKit server config, HTTPS proxy (Caddy) config
├── clients/                  # clients, benchmarks, call tests
├── archive/                  # the previous setup's code
├── build/asr-nim/            # scratch space of the ASR build
└── testdata/                 # sample clips + outputs
```

## Quick start

The NIM images come from NVIDIA's registry. Log in once, with the NGC API key
(`NVIDIA_API_KEY` in `.env`) as the password:

```bash
docker login nvcr.io -u '$oauthtoken'
```

The LLM NIMs keep their caches in `nim-cache/` (NVIDIA's `LOCAL_NIM_CACHE` pattern). The
folders must be writable by uid 1000, the user the NIM containers run as:

```bash
mkdir -p nim-cache/translate nim-cache/tts-lm
```

The ASR NIM loads a model repository built from AI4Bharat's checkpoints. Each one is gated
on Hugging Face: accept its terms (for example
https://huggingface.co/ai4bharat/indicconformer_stt_ml_hybrid_ctc_rnnt_large) with the
account whose `HF_TOKEN` is in `.env`, then build (about 3 minutes per language, on the GPU):

```bash
services/asr-nim/build/build.sh ml hi kn te ta
```

```bash
docker compose up -d --build
```

```bash
docker compose ps
```

The first start takes about 4 minutes, mostly the LLM NIMs compiling CUDA graphs; the
ASR NIM is ready in ~35 s. Services start one after another and report `healthy` only after their
warm-up: the ASR and translate healthchecks send warm-up requests themselves
([services/asr-nim/healthcheck.py](services/asr-nim/healthcheck.py),
[services/translate-nim/healthcheck.py](services/translate-nim/healthcheck.py)), and the
`tts` service warms `tts-lm` up when it starts.

Client tools run from the project venv (`.venv`):

```bash
.venv/bin/python clients/asr_client.py testdata/sample_hindi.wav --language hi-IN
```

```bash
.venv/bin/python clients/translate_client.py --text "नमस्ते, आप कैसे हैं?" --tgt en
```

```bash
.venv/bin/python clients/tts_stream.py --text "வணக்கம்" --language ta --out ta.wav
```

The model services bind to `127.0.0.1` because their APIs have no
authentication. Set `BIND_ADDR=0.0.0.0` only behind something that adds auth.
The call stack reaches them on localhost. The only ports on the network are the
call demo's: `8443` (HTTPS page and LiveKit signaling, token-gated), `7881/tcp`
and `50000-50200/udp` (WebRTC media).

Language codes: ASR takes Riva codes (`ml-IN`, `hi-IN`, …), TTS short ISO codes (`hi`,
`ta`, `en`, …), translate language names (`Hindi`, `Tamil`, `English`, …).

---

> **Using the models directly:** [docs/MODEL_SERVICES.md](docs/MODEL_SERVICES.md) is the
> complete guide to the model APIs (every parameter, error and tip), with tested
> examples in [docs/examples/](docs/examples/). The sections below are a summary.

## ASR — `http://host:8001/v1/audio/transcriptions`

AI4Bharat IndicConformer models served by NVIDIA's Riva ASR NIM, one model per language,
deployed the way NVIDIA documents custom models (see
[Implementation notes](#implementation-notes)). Each request transcribes a finished piece
of audio; the call agent cuts live audio into utterances with a VAD first (see
[Per utterance](#translated-calls-livekit)).

```bash
curl -s localhost:8001/v1/audio/transcriptions -F language=hi-IN -F file=@testdata/sample_hindi.wav
```

```json
{"text":"नमस्ते आज हम गणित की कक्षा सुरू करेंगे कृपया अपनी पुस्तके खोलिए और पहला पाठ पढ़िय "}
```

- `file`: 16-bit PCM WAV, mono, 16 kHz. **At most 16 s per request**: the models read
  the whole utterance in one pass, and a longer request crashes Riva's inference server.
  The container then restarts itself (back in ~55 s), but every caller loses speech
  recognition meanwhile. `docs/examples/asr_transcribe.py` cuts longer files at pauses.
- `language`: `ml-IN`, `hi-IN`, `kn-IN`, `te-IN` or `ta-IN`. Plain `hi` gives
  `404 Model not found for language hi`. There is no English and no auto-detection.
- More languages: accept the checkpoint's terms on Hugging Face, add the language to
  `services/asr-nim/build/build.sh`'s arguments, rebuild, restart `asr`.
- Also: gRPC on `50051` (the Riva ASR API, e.g. `nvidia-riva-client`; not tested here),
  `GET /v1/health/ready`.

## Translate — `http://host:8002/v1/chat/completions`

sarvam-translate on NVIDIA's model-free LLM NIM, an OpenAI-compatible server, so any
OpenAI client works. The request follows the model card: the target language in a
system message, the text as the user message.

```json
{"model": "sarvam-translate", "stream": true, "temperature": 0, "max_tokens": 256,
 "messages": [{"role": "system", "content": "Translate the text below to English."},
              {"role": "user", "content": "कल सुबह दस बजे मैं आपको फ़ोन करूँगा।"}]}
```

- Target names are plain language names: `Hindi`, `Tamil`, `Manipuri`, `English`, …
  (`PROMPT_NAME` in `services/s2s/app/languages.py` maps the ISO codes). The model
  infers the source language.
- The open checkpoint translates **English ↔ the 22 Indian languages**. Between two
  Indian languages the model card says to go through English: two calls, source →
  English, then English → target. The call agent does that automatically.
- `stream: true` returns Server-Sent Events whose `choices[0].delta.content` pieces
  make up the translation, ending with `data: [DONE]`.
- Concurrent requests share one batch on the GPU; there is no batch endpoint.

Also: `GET /v1/models`, `GET /v1/health/ready`, `GET /v1/metrics` (Prometheus).

## TTS — `ws://host:8003/v1/stream`

| client → server | |
|---|---|
| `{"type": "synthesize", "id": "u1", "text": "...", "language": "ta", "gender": "female", "sample_rate": 16000}` | queued; one at a time per socket, in order |
| `{"type": "cancel"}` | barge-in: stop the current request and drop the queue |
| `{"type": "cancel", "id": "u1"}` | cancel one request |

The server answers `{"type": "queued", "id", "position"}`, then
`{"type": "start", "id", "sample_rate", "encoding", "speaker"}`, then **binary
frames of PCM s16le**. A frame always belongs to the most recent `start`. It ends
with `{"type": "done", "id", "audio_ms", "ttfa_ms", "latency_ms", "rtf", "tokens"}`
(or `cancelled` / `error`).

Optional fields: `speaker` (any name from `GET /v1/voices`; the default is the
language's recommended female/male voice), `style` (`"news"`, `"Customer Care"`,
`"happy"`, …; preview quality per the model card), `sample_rate` (24000 native,
16000 / 8000 resampled), `format` (`pcm_s16le` | `pcm_f32le`), `temperature`,
`top_p`, `top_k`, `repetition_penalty`, `seed`, `max_seconds`.

The service splits long text into sentence chunks of ≤300 characters and streams
them back to back.

HTTP: `POST /v1/tts` with the same fields plus `format` (`wav` | `pcm_s16le` |
`pcm_f32le`) and `stream`. Non-streaming responses carry `X-TTFA-Ms`,
`X-Latency-Ms`, `X-Audio-Seconds` headers.

**How it runs:** the speech LM is in the `tts-lm` NIM. The `tts` service sends it the
prompt as token ids on `/v1/completions` (`return_token_ids: true`, the model card's
sampling: temperature 0.6, top-p 0.9, repetition penalty 1.2) and streams the
generated audio-token ids back. It turns them into sound itself (SNAC codes →
Vocos), on the GPU, in streamed windows.

---

## Measured performance

These are measurements on one L40S running all the services at once. Clients ran
on the same host and were driven in real time.

**ASR.** Each request is the same 6.5 s Hindi clip.

| concurrent requests | latency p50 | max | throughput |
|---|---|---|---|
| 1 | 25 ms | — | — |
| 4 | 61 ms | 95 ms | 42 clips/s |
| 8 | 105 ms | 183 ms | 44 clips/s |
| 16 | 194 ms | 367 ms | 44 clips/s |

Character error rate on our TTS test clips (3–5 sentences per language; TTS speech is
cleaner than a real caller, so treat these as a floor, not a forecast):

| | Malayalam | Hindi | Kannada | Telugu | Tamil |
|---|---|---|---|---|---|
| IndicConformer on Riva | 0.0% | 0.0% | 0.0% | 1.0% | 0.7% |
| Whisper Large v3 NIM (before) | 91.7% | 16.5% | 10.7% | 17.3% | 8.1% |

The Riva results are identical to the same models run in NeMo.

**Translate** (sarvam-translate). Sentences are ~15 words, Indian language →
English, each request its own streamed HTTP call.

| concurrency | first text p50 | full sentence p50 | throughput |
|---|---|---|---|
| 1 | 25 ms | 201 ms | — |
| 8 | 39 ms | 202 ms | 36 sentences/s |
| 32 | 79–94 ms | 242 ms | 106–111 sentences/s |
| 64 | 165–174 ms | 335 ms | 147 sentences/s |

Between two Indian languages it is two calls; Hindi → Tamil took ~0.35 s for a
sentence.

**TTS.** Each stream is one sentence of 7–9 s of audio. The real-time factor
(RTF) is generation time divided by audio length, so above 1.0 the listener
eventually hears gaps.

| concurrent streams | first audio p50 | RTF | playback stalls |
|---|---|---|---|
| 1 | 376 ms | 0.84 | none |
| 4 | 418 ms | 0.96 | none |
| 8 | 422 ms | 1.05 | up to 425 ms |

These match the earlier in-process vLLM setup (380 ms, 0.84 for one stream).

**Capacity.** TTS is the limit: about 6–7 simultaneous real-time voices. Because callers
take turns speaking, that covers more calls than voices. Speech recognition handles ~44
utterances a second, so it is no longer a constraint.

Re-run the benchmarks:

```bash
.venv/bin/python clients/asr_client.py testdata/sample_hindi.wav --language hi-IN --bench --concurrency 1 4 8 16
```

```bash
.venv/bin/python clients/translate_client.py --bench --concurrency 1 8 32 64
```

```bash
.venv/bin/python clients/tts_stream.py --bench --concurrency 1 4 8
```

## Translated calls (LiveKit)

```
browser A ──mic──┐                                   ┌──► "translation-A" ──► browser A
                 ▼                                   │     (only A may subscribe)
          LiveKit server (self-hosted SFU) ◄────► s2s-agent ──► ASR → translate → TTS
                 ▲                                   │     (model services above)
browser B ──mic──┘                                   └──► "translation-B" ──► browser B
```

| Service | What it is | Listens on |
|---|---|---|
| `livekit` | `livekit/livekit-server:v1.13.9`, WebRTC media server | signaling `127.0.0.1:7880`; media UDP `50000-50200`, TCP `7881` on `PUBLIC_HOST` |
| `s2s-agent` | LiveKit agent worker (`services/s2s/app/agent.py`); joins every room | outbound only |
| `s2s-web` | token API + demo page (`services/s2s/app/web.py`, `static/`) | `127.0.0.1:8090` |
| `proxy` | Caddy, HTTPS with its own CA, in front of the page and `/rtc` | `8443` |

These four services run with host networking (WebRTC needs real addresses) and
need no GPU. `.env` holds `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` and
`PUBLIC_HOST` (the address browsers use, now `10.79.252.124`).

**Try it:** open `https://10.79.252.124:8443` on two devices, or two browsers
with different mics. Pick a name, your language and the language to hear the
other person in, check your mic, and join; the **Invite** button copies the room
link for the other person. In the call, each person's ring pulses with their
voice, and each caption shows its state (listening, translating, playing) and
its delay. **Settings** changes languages, voice, mic and speaker devices,
volumes, and whether captions show the English step and the original words. The certificate comes
from Caddy's local CA, so browsers warn once. To remove the warning, trust its
root certificate:

```bash
docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

On a machine that can't reach `10.79.252.124` directly, testing needs that
address routable. Reaching the call from the internet needs a TURN server and a
real certificate; neither is set up yet.

**Language choice.** Each caller has three participant attributes: `speak` (the
languages with an ASR model: Malayalam, Hindi, Kannada, Telugu, Tamil), `hear` (the 22
Indian languages plus English) and `voice` (female | male, the voice the *other* person hears them
in). They're set at join time and can be changed mid-call from the page. The agent
restarts that caller's speech recognition or retargets translation on the fly. If a
listener's `hear` equals the speaker's `speak`, nothing is translated and the page
plays the speaker's own voice. Otherwise the page plays only the translation; an
"original voice" slider can mix the speaker's own voice back in.

**Translation route.** Fixed by the model: a turn with English on either side is one
call; between two Indian languages the agent goes through English (`route()` in
`agent.py`). That English step runs once per sentence and serves every listener
language, and it is the translation for anyone who hears English. Captions show the English step ("via English") when the page's setting
is on. Going through English can lose register: once, a formal "you" came back as
the informal "உனக்கு" in Tamil.

**Per utterance:** the agent takes the caller's mic (16 kHz; echo-cancelled when playout
leaks into it, see below), runs Silero VAD on it (`services/s2s/app/vad.py`, CPU) and
sends each finished utterance to the ASR NIM (`services/s2s/app/asr.py`). On each final
transcript, it translates into every listener language at the same time (streaming the
text as captions), and each language goes to its listeners' TTS sockets as soon as it is
ready; TTS plays utterances in order. Each listener's track only lets that listener
subscribe (publisher-side permissions). Every listener gets their own synthesized voice,
so one person speaking in a call of N people means N−1 voices at once: TTS (6–7
real-time voices) keeps calls up to about 5–7 people smooth.
Transcripts, translations and per-utterance latency go to every caller on the
`captions` data topic: `transcript`, `translation`, `metrics` and `status`
messages, see `agent.py`.

Live captions of the speaker's own words while they talk (`S2S_ASR_PARTIALS=1`, on):
the utterance so far is re-transcribed every half second, ~25 ms per request.

**Measured** with two simulated callers in real time (`clients/call_test.py`). Times are
from the moment the speaker stops talking:

| | Hindi → Tamil (6.5 s, via English) | Tamil → Hindi (via English) | Hindi → English |
|---|---|---|---|
| final transcript | 0.50 s (the VAD pause; ASR adds ~20 ms) | 0.50 s | 0.52 s |
| translation complete | 1.19 s | 0.81 s | 0.78 s |
| translated speech starts (agent) | 1.58 s | 1.19 s | 1.15 s |
| translated speech heard (other caller, after WebRTC) | 1.83 s | — | 1.33 s |

With the Whisper NIM, the same turns were heard at 2.16 s and 1.79 s. Most of what is
left is the 0.5 s pause that ends an utterance (`S2S_VAD_MIN_SILENCE_MS`), the two
translation calls between Indian languages (~0.6 s), and TTS until the first audio
(~0.4 s). The browser test (`clients/browser_test.js`, two headless Chromium callers with
WAV files as microphones) showed the same captions and 1.1–1.6 s delays on the real page.

```bash
docker run --rm --network host --env-file .env -v $PWD/clients:/clients -v $PWD/testdata:/td indic-s2s:0.1 python /clients/call_test.py
```

**Three people** (`clients/call_test3.py`): Asha (Hindi), Bala (Tamil) and Chitra
(Malayalam; the clip is our own TTS) each take a turn, each hearing their own language;
then Bala switches to English. Times are when each listener heard the translation, from
the moment the speaker stopped:

| Speaker | First listener | Second listener |
|---|---|---|
| Asha (Hindi) | Bala (Tamil) 1.92 s | Chitra (Malayalam) 1.89 s |
| Chitra (Malayalam) | Asha (Hindi) 1.87 s | Bala (Tamil) 2.05 s |
| Asha (Hindi) | Bala (English) 1.74 s | Chitra (Malayalam) 2.24 s |

Before the languages ran in parallel with a shared English step, the second listener
waited for the first: 3.28 s and 3.12 s in the same turns. Every listener receives only
their own translated track.

```bash
docker run --rm --network host --env-file .env -v $PWD/clients:/clients -v $PWD/testdata:/td indic-s2s:0.1 python /clients/call_test3.py
```

**Echo protection.** A browser's echo cancellation removes only its own speaker's sound.
When two callers sit in the same room, or one plays the call loudly enough to beat
the browser, translated speech leaks into a microphone, gets transcribed and translated
again, and bounces between the two sides indefinitely. The worker prevents this in
two layers:

- It cancels everything it plays into the room out of every microphone before
  speech recognition (WebRTC AEC3 through LiveKit's `AudioProcessingModule`, using a
  playout timeline as the reference). A coarse delay estimator extends the canceller
  from about 0.4 s to 1.5 s of echo path. AEC3 always runs, but speech recognition gets
  its output only while playout is actually leaking into that mic (a strong correlation
  in the last 20 s); otherwise it gets the mic untouched. AEC3 turns down the start of
  speech that overlaps playback even when nothing leaks, and that cost IndicConformer
  the first word of such utterances ("வணக்கம்" in the call test).
- Each finished utterance gets an echo score: how closely its loudness follows the
  playout. Echo scores 0.79–0.99, people talking over the playout 0.39–0.66. An
  utterance at 0.72 or above is not translated, and neither is one above 0.5 that
  repeats words the room just played. The page shows it dimmed as "echo, not translated".

`clients/echo_test.py` reproduces the loop with two simulated callers whose microphones
hear the room. With protection off, one sentence turned into 7 bouncing utterances. With
it on, there was no loop for echo paths of 0.2–1.0 s at up to equal loudness, and real
speech over the playout was still translated. Rerun on the current setup: no loop; the
agent saw the leak within seconds (correlation 0.95–0.98), and the two fragments that got
through before that were dropped as echo. `S2S_ECHO_CANCEL=0` turns the
protection off. `S2S_LOG_TEXT=1` (on) writes each transcript, its echo score and each
translation to `docker compose logs s2s-agent`; turn it off where call content must not
be logged.

**Restarts and crashes.** LiveKit dispatches the translator only when a room is
created, so a worker restart or crash mid-call would leave the room without one.
The web app's watchdog checks every 5 s and dispatches a new translator to any
room that has callers but no translator for 8 s. The worker also leaves calls
within 5 s of being told to stop (`S2S_DRAIN_TIMEOUT_S`). Measured on a restart
during a call: about 10-15 s without translation, then exactly one translator back.

**Not done yet:** phone calls (LiveKit SIP + an Indian SIP trunk), TURN/TLS for
internet use, and a barge-in policy. Today a translation keeps playing if the
listener starts talking.

## Implementation notes

These are the non-obvious decisions. Each one was measured, not assumed.

**NVIDIA deployment.** Every model runs in an NVIDIA NIM container, configured the way
NVIDIA documents it: custom ASR models through `nemo2riva` → `riva-build` →
`riva-deploy` and the NIM's `NIM_DISABLE_MODEL_DOWNLOAD` / `NIM_EXPORT_PATH`, local
checkpoints through the model-free LLM NIM's `NIM_MODEL_PATH`, a host cache folder per LLM
NIM at `/opt/nim/.cache`, the standard ports and health endpoints (`/v1/health/ready`).
Two things NIMs don't do are ours: the VAD that turns a live call into utterances (in the
agent), and the SNAC + Vocos audio decoder for indic-speak (the `tts` service).
indic-speak has no Riva or speech-NIM path: Riva TTS only runs NVIDIA's own architectures.

**IndicConformer into Riva (`services/asr-nim/build/`).** Each step was needed:

- *One language per model.* AI4Bharat's checkpoints are hybrid RNNT/CTC models whose
  CTC head covers all 22 languages' tokens (22 × 256 + blank, "multi-softmax"), which
  stock NeMo can't load and Riva can't serve. `make_ctc.py` keeps the encoder and that
  language's 256 rows plus the blank, and saves a standard `EncDecCTCModelBPE`. The
  output is identical to the original model's CTC branch. (Each language's checkpoint is
  its own fine-tune: the Malayalam one's Hindi rows output nothing.)
- *`nemo2riva --max-dim 3200`.* Its default trace length overruns the encoder's position
  table.
- *fp32 TensorRT* (`--nn.use_trt_fp32`). The fp16 engine output only blanks, even with
  NeMo's own features fed straight into it.
- *Whole-utterance offline mode with NeMo's features* (`--streaming=False`, one 16 s
  chunk without padding, `--feature_extractor_type=torch --torch_feature_type=nemo`).
  Riva's default chunked mode with its default features dropped the first word in
  Tamil, Telugu and Kannada. The cost is the 16 s limit per request. riva-build doesn't
  package the mel filterbank file this featurizer reads, so the build makes it.
- *Batch 4.* At batch 16 each fp32 engine reserved ~2.1 GB of working memory, and five
  didn't fit next to the LLMs.

**Speech recognition (`services/s2s/app/asr.py`).** Each caller's stream emits the same
events a streaming ASR would (`speech_start`, `partial`, `final` with stream
timestamps), so echo scoring and latency tracking are unchanged. Finals go out in
utterance order. Utterances are cut at 15 s in a monologue so finals keep coming, and
so no request reaches the 16 s limit. `import silero_vad` pulls in torch, so the agent
loads the package's ONNX file directly and runs it with CPU ONNX Runtime. The first
request after the ASR NIM starts is slow, so its healthcheck warms every language's
model before reporting healthy.

**Riva streaming.** Riva can also stream (partial results while someone talks, its own
end-of-speech detection), but it wouldn't shorten the delay here: whole-utterance
recognition takes ~25 ms, and a final transcript has to wait for the pause either way.
Live captions come from re-transcribing the utterance so far.

**Translate.** The NIM's vLLM 0.28 reserves very little KV cache at a GPU share
(~1.5 GiB at 0.50 for indic-translate), so the size is pinned with
`--kv-cache-memory-bytes` (4 GiB, 18,507 tokens for Gemma 3 4B). Its first burst of
concurrent requests stalls ~1 s while batched-decode kernels compile, which is why the
healthcheck sends one before reporting healthy.

**TTS (`services/tts/app/`)**

- *Speech LM on the NIM.* vLLM's completions API takes a list of token ids as the
  prompt and, with `return_token_ids`, streams the generated ids back, so the service's
  prompt layout and token → frame logic didn't change. First audio and RTF matched the
  in-process engine. KV cache pinned at 5 GiB (46,800 tokens).
- *Exact streaming vocoder.* Each chunk is vocoded with 8 frames of left context
  and 4 frames (341 ms) of lookahead. That matches a one-shot decode to ~77 dB
  SNR. Two frames of lookahead would save ~140 ms but leave ~30 dB errors (clicks)
  at chunk edges (`TTS_CONTEXT_RIGHT`).
- *No TF32 in the vocoder.* PyTorch's default TF32 convolutions add a
  shape-dependent ~40 dB noise floor that differs between streamed windows.
  FP32 removes it at the same speed.
- *Adaptive chunks.* The server tracks how much audio the listener has buffered.
  Below 200 ms it sends every frame as soon as it's ready; above that it batches
  up to 4 frames per vocoder call. Concurrent streams' vocoder calls are batched
  together on the GPU.
- Sampling follows the model card's production config (temperature 0.6, top-p 0.9,
  repetition penalty 1.2). Generation length is capped from the text length so a
  runaway generation can't babble for 30 s.

## Operations

- **GPU budget** (46 GB L40S): ASR NIM 11.8 GB (five fp32 engines), translate 11.2 GB,
  tts-lm 12.4 GB, tts decoder 0.9 GB: 36.3 GB in all. The `dsx-explorer` compose project
  (`/home/ubuntu/DigitalTwin/deploy`) stays stopped.
- **Startup order** is asr → translate → tts-lm → tts → s2s-agent
  (`depends_on: service_healthy`). Each engine sizes its memory from what is free when
  it starts, so they start one at a time.
- **The ASR NIM starts in ~35 s** (it unpacks `custom_model.tar.gz`, 1.6 GB, then warms
  each language). **It restarts itself if Riva dies:** a request over 16 s crashes Riva's
  inference server while the container keeps running, and Docker doesn't restart
  unhealthy containers, so after three failed checks the healthcheck ends the
  container's processes and `restart: unless-stopped` brings it back (measured: 55 s).
- **Adding ASR languages:** accept the checkpoint's terms on Hugging Face, run
  `services/asr-nim/build/build.sh` with all the languages you want (it builds one
  package), add them to `ASR_LANGUAGES` in `services/s2s/app/languages.py` and
  `ASR_WARM_LANGUAGES` in `services/asr-nim/healthcheck.py`, then restart `asr` and
  rebuild `s2s-agent`.
- **Logs:** `docker compose logs -f asr` (or `translate`, `tts-lm`, `tts`, `s2s-agent`).
- **Disk:** the root volume is at 90% (32 GB free). Docker keeps both the compressed and
  the unpacked copy of each image, so an image takes ~3× its download size.
  `docker builder prune` reclaims build cache after rebuilds.
- **Model files** in `models/` (including `indicconformer-<lang>/`) were downloaded with
  the HF token in `.env`; the token is not passed to the serving containers.

## Licensing

- **NIM containers** (the Riva ASR NIM image, the model-free NIM) are NVIDIA software:
  pulling needs an NGC key, and production use needs an NVIDIA AI Enterprise licence. The
  ASR image is governed by the NVIDIA AI Product Agreement (shown in its startup log).
- **IndicConformer** checkpoints are MIT (AI4Bharat's model cards).
- **sarvam-translate** is GPL-3.0 (its model card).
- **indic-speak** is under the **Indic Open Model License v1.0** (Bodhan AI / AI4Bharat).
  Self-hosting for your own product is free, with attribution ("Built with … from
  Bodhan AI / AI4Bharat"). Running it as an API that **other companies call directly**
  needs Bodhan AI's written approval, unless you are a nonprofit, government or academic
  user. The license also forbids robocalls and voice cloning of real people without
  consent. It also depends on Llama 3.2 and SNAC, whose licenses apply too.
