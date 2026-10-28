"""Talking to the assistant on its page: recordings in, voice-mode replies, and "yes" confirming by voice."""
import io
import json
import wave

import numpy as np
from conftest import signup
from sqlalchemy import text
from test_assistant import calls, chat, later, route, say, team

from app import asr, db, voices


def wav(seconds=1.0, rate=16000, channels=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.sin(np.arange(int(seconds * rate)) / 8) * 8000).astype("<i2").tobytes() * channels)
    return buf.getvalue()


async def test_recordings_are_checked_then_transcribed(client, monkeypatch):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    heard = {}

    async def transcribe(http, pcm, lang):
        heard.update(n=len(pcm), lang=lang)
        return "what meetings do I have tomorrow", 42.0
    monkeypatch.setattr(asr, "transcribe", transcribe)
    post = lambda body, lang="en": client.post(f"/api/assistant/transcribe?lang={lang}", content=body,  # noqa: E731
                                               headers={"content-type": "audio/wav"})
    r = await post(wav(1.5), "hi")
    assert r.status_code == 200 and r.json() == {"text": "what meetings do I have tomorrow", "ms": 42}
    assert heard == {"n": 24000, "lang": "hi"}
    assert (await post(b"not audio")).status_code == 400
    assert (await post(wav(1, rate=48000))).status_code == 400                 # the page sends 16 kHz
    assert (await post(wav(1), "fr")).status_code == 400
    assert (await post(wav(31))).status_code == 413
    assert (await post(wav(0.1))).json() == {"text": "", "ms": 0}             # a click, not speech


async def test_speech_routes_need_a_session_and_magpie(client, monkeypatch):
    assert (await client.post("/api/assistant/transcribe", content=wav(1))).status_code == 401
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    monkeypatch.setattr(voices, "MAGPIE_TTS_URL", "")
    assert (await client.post("/api/assistant/speak", json={"text": "Hello"})).status_code == 503


async def test_voice_mode_replies_and_a_spoken_yes_confirms(client, script):
    _, _, ben_id = await team(client)
    start, end = later()
    script += [route(("meetings", "Schedule a review with Ben")),
               calls(("create_meeting", {"title": "Review", "start_time": start, "end_time": end, "participant_ids": [ben_id]})),
               say("I'll schedule the review with Ben.")]
    ev = await chat(client, "schedule a review with Ben", voice=True, language="en")
    assert any(e["type"] == "confirmation" for e in ev)
    assert "VOICE MODE" in next(x for x in script.seen if x.get("tools"))["messages"][0]["content"]
    assert not any(e["type"] == "text" and "Please confirm" in e["text"] for e in ev)   # no page-only filler in voice
    cid = ev[0]["id"]

    seen_before = len(script.seen)
    ev = await chat(client, "Yes, go ahead.", conversationId=cid, voice=True)
    decided = next(e for e in ev if e["type"] == "decided")
    assert decided["decision"] == "confirm" and decided["ok"] and decided["text"].startswith("✓ Done: Scheduled")
    assert len(script.seen) == seen_before                                     # decided without the model
    async with db.sessionmaker()() as s:
        assert (await s.execute(text("SELECT count(*) FROM meetings WHERE title = 'Review'"))).scalar() == 1

    # Typed "yes" (not voice) goes to the agents as usual; with nothing pending, a spoken "no" does too.
    script += [route(("general", "yes")), say("Okay.")]
    ev = await chat(client, "no", conversationId=cid, voice=True)
    assert not any(e["type"] == "decided" for e in ev) and json.dumps(ev).count("Okay.") == 1
