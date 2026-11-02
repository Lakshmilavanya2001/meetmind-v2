"""Lower call latency: the early transcript (taken ~0.2 s into a pause), translations started from it,
and translations spoken phrase by phrase as they stream in."""
import asyncio
from types import SimpleNamespace

import numpy as np

from app import agent, asr
from app.vad import FRAME, SR, Segmenter, SegmenterConfig


class LoudnessVAD:
    """Speech wherever the audio is loud."""

    def new_state(self):
        return {}

    def prob(self, frame, st):
        return 1.0 if np.abs(frame).mean() > 0.1 else 0.0


def speech(s):
    return np.full(int(s * SR) // FRAME * FRAME, 0.5, np.float32)


def silence(s):
    return np.zeros(int(s * SR) // FRAME * FRAME, np.float32)


def test_a_pause_hands_out_the_audio_the_end_will_have():
    seg = Segmenter(LoudnessVAD(), SegmenterConfig(min_silence_ms=500, early=True))
    evs = seg.feed(np.concatenate([speech(1.0), silence(0.7)]))
    assert [e.kind for e in evs] == ["start", "pause", "end"]
    pause, end = evs[1], evs[2]
    assert np.array_equal(pause.audio, end.audio)               # so the end can reuse its transcription


def test_talking_again_after_a_pause_makes_a_new_pause():
    seg = Segmenter(LoudnessVAD(), SegmenterConfig(min_silence_ms=500, early=True))
    evs = seg.feed(np.concatenate([speech(1.0), silence(0.3), speech(0.5), silence(0.7)]))
    assert [e.kind for e in evs] == ["start", "pause", "pause", "end"]
    assert len(evs[1].audio) < len(evs[2].audio)
    assert np.array_equal(evs[2].audio, evs[3].audio)
    # without early transcripts, no pauses
    seg = Segmenter(LoudnessVAD(), SegmenterConfig(min_silence_ms=500))
    assert [e.kind for e in seg.feed(np.concatenate([speech(1.0), silence(0.7)]))] == ["start", "end"]


def _stream(monkeypatch, calls):
    async def fake_transcribe(http, audio, lang):
        calls.append(len(audio))
        await asyncio.sleep(0.01)
        return f"words {len(audio)}", 10.0
    monkeypatch.setattr(asr, "transcribe", fake_transcribe)
    monkeypatch.setattr(asr, "shared_vad", lambda: LoudnessVAD())
    monkeypatch.setattr(asr, "EARLY", True)
    return asr.AsrStream(None, "en", partials=False, min_silence_ms=500)


def pcm16(x):
    return (x * 32767).astype("<i2").tobytes()


async def _events(stream, n):
    out = []
    for _ in range(n):
        out.append(await asyncio.wait_for(stream.events.get(), 2))
    return out


async def test_the_final_reuses_the_early_transcription(monkeypatch):
    calls = []
    s = _stream(monkeypatch, calls)
    s.feed(pcm16(np.concatenate([speech(1.0), silence(0.3)])))   # into the pause: early transcription starts
    await asyncio.sleep(0.05)
    s.feed(pcm16(silence(0.4)))                                    # the pause was the end
    evs = await _events(s, 3)
    assert [e["type"] for e in evs] == ["speech_start", "early", "final"]   # early always comes first
    assert evs[1]["text"] == evs[2]["text"] and evs[2]["early"] is True
    assert len(calls) == 1                                         # one recognition request, not two


async def test_an_early_transcription_is_dropped_when_the_speaker_goes_on(monkeypatch):
    calls = []
    s = _stream(monkeypatch, calls)
    s.feed(pcm16(np.concatenate([speech(1.0), silence(0.3), speech(0.5)])))   # a pause, then more words
    await asyncio.sleep(0.05)
    s.feed(pcm16(silence(0.7)))
    evs = await _events(s, 3)
    types = [e["type"] for e in evs]
    assert types[0] == "speech_start" and types[-1] == "final" and evs[-1]["early"] is True
    whole = len(np.concatenate([speech(1.0), silence(0.3), speech(0.5), silence(0.2)]))
    assert all(e["text"].startswith("words ") and int(e["text"][6:]) >= whole for e in evs[1:])
    assert len(calls) == 1                     # the first pause's request was dropped (here before it even ran)


def test_phrases():
    t = "हैलो प्रिया, क्या हम लॉन्च रिव्यू को शुक्रवार को शिफ्ट कर सकते हैं? ठीक"
    end = agent.next_phrase(t, 0, 2)
    assert t[:end] == "हैलो प्रिया,"
    end2 = agent.next_phrase(t, end, 2)
    assert t[end:end2].strip().endswith("हैं?")
    assert agent.next_phrase(t, end2, 2) is None                   # "ठीक" isn't finished yet
    assert agent.next_phrase("Hello Priya, can", 0, 4) is None    # a comma needs 4 words here
    assert agent.next_phrase("It costs 3.5 lakh", 0, 2) is None    # a decimal point isn't a sentence end
    assert agent.next_phrase("Dr. Sharma will join.", 0, 2) is None


class StreamingTranslator:
    def __init__(self):
        self.asked = []

    async def translate(self, text, src, tgt, on_delta, english=None):
        self.asked.append(text)
        out = f"[{tgt}] {text}"
        half = len(out) // 2
        await on_delta(out[:half])
        await asyncio.sleep(0.01)
        await on_delta(out)
        return out, {"route": "direct"}


def _call():
    call = agent.CallTranslator(SimpleNamespace(name="mm-test-room"))
    call.translator = StreamingTranslator()
    call.sent = []

    async def send(msg, to=None):
        call.sent.append(msg)
    call.send = send

    async def line(*a, **k):
        return None
    call.transcript = SimpleNamespace(line=line, translation=line)
    call.callers = {"ben": agent.Caller("ben", "Ben", "en", "en", "male", "standard"),
                    "asha": agent.Caller("asha", "Asha", "hi", "hi", "female", "standard")}
    call.queues = {"ben": asyncio.Queue()}
    inp = SimpleNamespace(identity="ben", lang="en", echo=SimpleNamespace(echo_score=lambda a, b: 0.0),
                          wall_at=lambda ms: 0.0)
    return call, inp


async def test_a_translation_started_early_is_taken_over_by_the_final():
    call, inp = _call()
    text = "Can we move the review to Friday?"
    await call.on_asr_event(inp, {"type": "early", "segment_id": 1, "run": "r", "text": text})
    await asyncio.sleep(0.05)
    assert not [m for m in call.sent if m.get("type") == "translation"]   # nothing shown before the final
    await call.on_asr_event(inp, {"type": "final", "segment_id": 1, "run": "r", "text": text,
                                  "start_ms": 0, "end_ms": 2000})
    utt = call.queues["ben"].get_nowait()
    assert utt.spec is not None and "hi" in utt.spec.jobs
    assert call.translator.asked == [text]                                  # translated once, early
    assert [m["text"] for m in call.sent if m.get("type") == "translation"] == [f"[hi] {text}"]


async def test_a_different_final_is_translated_afresh():
    call, inp = _call()
    await call.on_asr_event(inp, {"type": "early", "segment_id": 1, "run": "r", "text": "Can we move"})
    await call.on_asr_event(inp, {"type": "final", "segment_id": 1, "run": "r", "text": "Can we move it to Friday?",
                                  "start_ms": 0, "end_ms": 2000})
    utt = call.queues["ben"].get_nowait()
    assert utt.spec is None and not call.specs
    # and the assistant's words are never translated early
    await call.on_asr_event(inp, {"type": "early", "segment_id": 2, "run": "r", "text": "Hey Mind, what's next?"})
    assert not call.specs


async def test_a_translation_is_spoken_phrase_by_phrase():
    call, _ = _call()
    said = []

    class Synth:
        async def say(self, utt, text, lang, voice, playbacks):
            said.append(text)
            for pb in playbacks:
                pb.chunks.put_nowait(None)

    class Out:
        def enqueue(self, utt):
            return agent.Playback(utt)
    call.synth, call.outputs = Synth(), {"asha": Out()}

    class Translator:
        async def translate(self, text, src, tgt, on_delta, english=None):
            for part in ["हैलो", "हैलो प्रिया,", "हैलो प्रिया, क्या हम", "हैलो प्रिया, क्या हम मिल सकते हैं?"]:
                await on_delta(part)
                await asyncio.sleep(0.01)
            return "हैलो प्रिया, क्या हम मिल सकते हैं?", {"route": "direct"}
    call.translator = Translator()
    utt = agent.Utterance("ben:en:r.1", "ben", "Hello Priya, can we meet?", 0.0, 0.0)
    await call.deliver(utt, call.callers["ben"], "hi", [call.callers["asha"]], None)
    await asyncio.sleep(0.05)
    assert said == ["हैलो प्रिया,", "क्या हम मिल सकते हैं?"]
