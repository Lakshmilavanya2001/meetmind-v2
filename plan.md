# Plan: Live Indic ↔ Indic Call Translation

## 1. Goal

Two people on a phone call speak different Indian languages (e.g. Hindi ↔ Tamil).
Each person hears the other in their own language, with low delay.

**Targets**

| Metric | Target |
|---|---|
| End-of-speech → start of translated audio | ≤ 2.0 s (stretch: 1.5 s) |
| Translation quality (chrF++) | Within 2 points of best offline route per pair |
| ASR word error rate on real call audio | ≤ 20% for major languages |
| Concurrent calls per GPU | Measure in Phase 3, target ≥ 10 |

**Languages:** start with 6 (Hindi, Bengali, Tamil, Telugu, Marathi, Gujarati), then grow toward the 22 scheduled languages.

---

## 2. Architecture

Each call runs **two pipelines in parallel** (A→B and B→A).

```
Speaker A audio (8/16 kHz)
  │
  ├─► VAD (Silero)            – cut speech into utterances at pauses
  ├─► ASR (IndicConformer)    – streaming speech → text, source language
  ├─► Text normalize          – punctuation, numbers, code-mixed English words
  ├─► Router                  – pick "direct" or "pivot" per language pair
  │      direct: IndicTrans2 indic-indic
  │      pivot : IndicTrans2 indic-en  → en-indic
  ├─► IndicProcessor          – pre/post-processing (required for IndicTrans2)
  ├─► TTS (Indic Parler-TTS / IndicF5) – stream audio out
  │
  └─► Speaker B hears translation (+ optional text on screen)
```

### Model choices

| Stage | Primary | Fallback / alternative |
|---|---|---|
| VAD | Silero VAD | WebRTC VAD |
| ASR | AI4Bharat IndicConformer | IndicWhisper, Sarvam Saarika (API) |
| NMT direct | `ai4bharat/indictrans2-indic-indic-dist-320M` | `…-indic-indic-1B` |
| NMT pivot | `indictrans2-indic-en-dist-200M` + `indictrans2-en-indic-dist-200M` | 1B variants |
| TTS | AI4Bharat Indic Parler-TTS | IndicF5, Sarvam Bulbul (API) |

### Routing rule (direct vs pivot)

Direct translation is faster but, for some pairs, worse than going through English.
Decide **per language pair from measured scores**, not by assumption:

```python
ROUTES = {
    ("hin_Deva", "tam_Taml"): "direct",
    ("ben_Beng", "guj_Gujr"): "pivot",
    # filled from Phase 1 evaluation
}
route = ROUTES.get((src, tgt), "pivot")   # default to the higher-quality path
```

---

## 3. Latency budget (per utterance)

| Stage | Budget |
|---|---|
| VAD end-of-speech detection | 300 ms |
| ASR final result | 300 ms |
| NMT (direct ≈ 150 ms, pivot ≈ 300 ms) | 300 ms |
| TTS time to first audio | 500 ms |
| Network / telephony | 300 ms |
| **Total** | **≈ 1.7 s** |

---

## 4. Phases

### Phase 0 – Setup (week 1)
- [ ] GPU box (1× A10/L4 or better), Python 3.10+, CUDA
- [ ] Download models; install `IndicTransToolkit`, `nemo_toolkit` (for IndicConformer), `ctranslate2`
- [ ] Collect test data: 100+ real or realistic call clips per language, including Hinglish-style code-mixing

### Phase 1 – Offline quality baseline (weeks 2–3)
- [ ] ASR: measure WER per language on test clips
- [ ] NMT: for each language pair, compare **direct vs pivot** on the same sentences (chrF++, plus native-speaker spot checks)
- [ ] Check common setup errors: IndicProcessor used, correct language tags, beam size 4–5, sentence-level input
- [ ] Output: `ROUTES` table + quality report

**Done when:** every target pair has a chosen route backed by numbers.

### Phase 2 – One-direction prototype (weeks 4–5)
- [ ] File in → translated audio file out (A→B only)
- [ ] Add streaming: VAD chunking + streaming ASR
- [ ] Log per-stage timings for every utterance

**Done when:** a 1-minute Hindi clip plays back as Tamil with per-stage timings logged.

### Phase 3 – Speed work (weeks 6–7)
- [ ] Convert NMT to **CTranslate2** (int8 / float16)
- [ ] Use distilled models; beam size 2–3 for live mode
- [ ] Keep all models loaded; batch utterances across calls
- [ ] Stream TTS output (start playback before full sentence is synthesized)
- [ ] Overlap stages: start NMT as soon as ASR finalizes a sentence

**Done when:** p50 ≤ 1.7 s and p95 ≤ 2.5 s end-to-end on the test set.

### Phase 4 – Two-way live call (weeks 8–9)
- [ ] Run A→B and B→A pipelines together
- [ ] Telephony / WebRTC integration (e.g. SIP via FreeSWITCH/Asterisk, or WebRTC for an app demo)
- [ ] Echo handling: don't re-translate the TTS audio coming back on the line
- [ ] Optional: show live transcript + translation on screen

**Done when:** two people hold a 5-minute call in different languages.

### Phase 5 – Hardening (week 10+)
- [ ] Load test: concurrent calls per GPU
- [ ] Fallbacks: if a stage fails or is slow, fall back to API (Sarvam / Bhashini) or text-only
- [ ] Monitoring: per-stage latency, error rates, per-language quality samples
- [ ] Add more languages; re-run Phase 1 evaluation for each new pair

---

## 5. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Direct NMT quality lower for some pairs | Per-pair routing; pivot by default |
| ASR errors on noisy 8 kHz phone audio | Fine-tune / test on telephony audio; noise suppression before ASR |
| Code-mixed speech (English words inside Indic) | Normalize + keep English terms as-is; test on real speech |
| Cutting sentences too early breaks meaning | Tune VAD pause length (≈ 400–700 ms); translate full phrases only |
| Delay feels awkward in conversation | Play a short tone/filler; show live text while audio is generated |
| Low-resource languages (Bodo, Santali, Dogri…) | Mark as "beta"; use pivot + API fallback; collect data |
| Privacy of call audio | Self-host in India; no audio stored by default; get user consent |

---

## 6. Open questions

- Telephony or app-only? (Decides SIP vs WebRTC in Phase 4.)
- Should the original voice be heard faintly under the translation?
- Voice style: one fixed TTS voice per language, or match speaker gender?
- Is any English intermediate text needed (logs, summaries)? If yes, pivot route gives it for free.
