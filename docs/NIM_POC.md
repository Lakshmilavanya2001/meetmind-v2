# indic-translate on NVIDIA NIM: proof of concept

Tested 2026-10-24 on the L40S, with ASR and TTS running alongside as usual.

**Status:** adopted the same day, then superseded the same day. The stack now runs
Whisper Large v3 (speech NIM) and sarvamai/sarvam-translate instead of IndicConformer and
indic-translate (see the README). What this test established still holds: the
model-free LLM NIM serves a local checkpoint unchanged, it needs the KV-cache size
pinned, and it needs a warm-up burst. sarvam-translate and indic-speak's speech LM both
run that way now. The `translate-vllm` fallback and the custom service compared below
were removed; their code is in `archive/previous-setup-2026-10-24.tar.gz`.

**Result:** the model-free LLM NIM serves our local `bodhan-ai/indic-translate`
checkpoint unchanged. On a 24-sentence set its outputs are identical to our
vLLM service, and latency is the same within noise. No conversion step is
needed: the NIM is vLLM underneath and Gemma 4 is a supported architecture.

## Setup

| | Our translate service | NIM |
|---|---|---|
| Image | `indic-translate:0.1` (vLLM 0.31.0, CUDA 12.9) | `nvcr.io/nim/nvidia/model-free-nim:2.0.13` (vLLM 0.28.0, torch 2.13, CUDA 13.0, transformers 5.15.1) |
| API | `POST /v1/translate`, `WS /v1/stream` (ours) | OpenAI `POST /v1/chat/completions` |
| Engine flags | bf16, max-model-len 4096, max-num-seqs 64, max-num-batched-tokens 1024, prefix caching, language-model-only | the same, passed straight through |
| Prompt | the model card's prompt contract, one user turn | the same text; the NIM applies the model's own chat template |
| Decoding | greedy, stop `<turn|>` | greedy, stop `<turn|>` |

## Results

Measured client-side with [clients/nim_compare.py](../clients/nim_compare.py):
24 sentences, 7 source languages, Indic and English targets, streamed. Raw
outputs are in `testdata/out/nim_poc/`.

| | Our service | NIM |
|---|---|---|
| Identical outputs | — | **24 / 24** |
| First text, one request at a time (median) | 59 ms | 59 ms |
| Full sentence, one request at a time (median) | 203 ms | 203 ms |
| 8 concurrent: sentence median / throughput | 249 ms / 16.3 per s | 252 ms / 16.2 per s |
| 32 concurrent: sentence median / throughput | 312 ms / 54.3 per s | 296 ms / 56.6 per s |
| GPU memory | 23 GB | 21 GB (with the KV cache pinned, see below) |
| Startup to ready | ~30 s | 88–104 s |

## Differences to handle before using it for calls

1. **KV cache size.** With the same `--gpu-memory-utilization 0.50`, the NIM's
   vLLM 0.28 reserved only ~1.5 GiB of KV cache (6,010 tokens, room for about
   one and a half 4k-token requests) against 7.23 GiB in our service. It still
   handled the test's short sentences, but long inputs or high concurrency
   would queue. Pin the size explicitly instead:
   `--kv-cache-memory-bytes 7766566912` (131,555 tokens).
2. **No warm-up.** The first burst of 8 concurrent requests after startup
   stalled ~1.1 s while batched-decode kernels compiled. Our service sends that
   burst itself at startup; in front of a NIM, send a warm-up burst before
   taking calls.
3. **Different API.** The NIM speaks only the OpenAI API. The S2S agent uses our
   `WS /v1/stream` and asks the service for the pivot route. Switching means
   pointing the agent's `TranslateClient` at streaming chat completions (pivot
   becomes two calls), or keeping our FastAPI as a thin front over the NIM.
4. **Image size.** An 11.8 GB download that takes 43.8 GB on disk here (Docker's
   containerd store keeps the compressed and unpacked copies).
5. **Driver.** NIM 2.0.13 is built for CUDA 13 and needs driver 580 or newer
   (this host has 580.178).
6. **Licensing.** Pulling needs an NGC key. Production use of a NIM needs an
   NVIDIA AI Enterprise licence, on top of the model's own licence terms.

## Reproduce

This is how the comparison was run, with the custom service still on `:8002`
and the NIM on `:8000`. The custom service's compose entry has since been
replaced by the NIM.

```bash
docker login nvcr.io -u '$oauthtoken'        # password: the NGC key (NVIDIA_API_KEY in .env)
docker pull nvcr.io/nim/nvidia/model-free-nim:2.0.13
.venv/bin/python clients/nim_compare.py service --out testdata/out/nim_poc/baseline_vllm.json
docker compose stop translate                  # the GPU can't hold both
mkdir -p ~/nim_cache
docker run -d --name nim-translate-poc --gpus all --shm-size 2g -u $(id -u):0 \
  -p 127.0.0.1:8000:8000 \
  -v ~/nim_cache:/opt/nim/.cache \
  -v $PWD/models/indic-translate:/models/indic-translate:ro \
  -e NIM_MODEL_PATH=/models/indic-translate -e NIM_SERVED_MODEL_NAME=indic-translate \
  nvcr.io/nim/nvidia/model-free-nim:2.0.13 \
  --dtype bfloat16 --max-model-len 4096 --kv-cache-memory-bytes 7766566912 --max-num-seqs 64 \
  --max-num-batched-tokens 1024 --enable-prefix-caching --language-model-only --trust-remote-code
# wait for: curl -sf http://127.0.0.1:8000/v1/health/ready
.venv/bin/python clients/nim_compare.py nim --out testdata/out/nim_poc/nim.json
.venv/bin/python clients/nim_compare.py diff testdata/out/nim_poc/baseline_vllm.json testdata/out/nim_poc/nim.json
docker rm -f nim-translate-poc && docker compose up -d translate
```

