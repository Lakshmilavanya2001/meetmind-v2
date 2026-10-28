"""Saved transcripts, summaries and cited questions, with stand-ins for the LLM and the embedding model."""
import hashlib
import json
import re
import time

import numpy as np
import pytest
from conftest import other, signup
from sqlalchemy import select, update

from app import db, embeddings, search, transcripts
from app.assistant import llm
from app.models import Meeting, MeetingSummary, TranscriptSegment
from app.transcript_store import RoomTranscript


def fake_vector(t: str) -> list[float]:
    """Bag of words hashed into 2048 dimensions: texts sharing words are close."""
    v = np.zeros(2048)
    for w in re.findall(r"\w+", t.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 2048] += 1
    n = np.linalg.norm(v)
    return (v / n if n else v + 1e-3).tolist()


@pytest.fixture
def models(monkeypatch):
    """Embeddings and LLM stand-ins; `replies` holds what the LLM answers next."""
    replies = []

    async def embed(http, texts, kind="passage"):
        return [fake_vector(t) for t in texts]

    async def chat(http, messages, **kw):
        return {"content": json.dumps(replies.pop(0)) if replies else "{}"}
    monkeypatch.setattr(embeddings, "embed", embed)
    monkeypatch.setattr(embeddings, "configured", lambda: True)
    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(llm, "configured", lambda: True)
    return replies


async def _setup(client):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    code = (await client.get("/api/me")).json()["organization"]["joinCode"]
    ben, cara, zed = other(client), other(client), other(client)
    await signup(ben, "Ben Das", "ben@acme.test", join_code=code)
    await signup(cara, "Cara Iyer", "cara@acme.test", join_code=code)      # same org, not in the meeting
    await signup(zed, "Zed Other", "zed@globex.test", org="Globex")
    ben_id = (await ben.get("/api/me")).json()["user"]["id"]
    m = (await client.post("/api/meetings", json={"title": "Launch review", "participantIds": [ben_id]})).json()["meeting"]
    await client.post(f"/api/meetings/{m['joinCode']}/join", json={"speak": "en", "hear": "en"})
    return m["joinCode"], ben, cara, zed, ben_id


async def _say(code, lines, saving=True):
    async with db.sessionmaker()() as s:
        await s.execute(update(Meeting).where(Meeting.join_code == code).values(save_transcript=saving))
        await s.commit()
    rt = RoomTranscript(f"mm-{code}")
    t = time.time()
    ids = []
    for i, (who, uid, text) in enumerate(lines):
        ids.append(await rt.line(f"u{i}", uid or "test-caller", who, "en", text, t + i * 5, t + i * 5 + 4))
    return rt, ids


async def test_only_the_host_turns_saving_on_and_everyone_sees_it(client):
    code, ben, cara, zed, _ = await _setup(client)
    assert (await ben.post(f"/api/meetings/{code}/transcript", json={"enabled": True})).status_code == 403
    assert (await client.post(f"/api/meetings/{code}/transcript", json={"enabled": True})).json() == {"saveTranscript": True}
    assert (await ben.get(f"/api/meetings/{code}")).json()["meeting"]["saveTranscript"] is True
    for c in (ben, cara, zed):
        await c.aclose()


async def test_the_agent_saves_lines_only_while_saving_is_on(client):
    code, ben, cara, zed, ben_id = await _setup(client)
    rt, ids = await _say(code, [("Ben Das", ben_id, "Not saved yet.")], saving=False)
    assert ids == [None]
    rt, ids = await _say(code, [("Ben Das", ben_id, "We ship on Friday."), ("Asha Rao", None, "Agreed.")])
    assert all(ids)
    await rt.translation("u0", "hi", "हम शुक्रवार को शिप करेंगे।")
    assert await rt.line("u0", ben_id, "Ben Das", "en", "We ship on Friday.", time.time(), time.time()) is None   # same utterance once
    d = (await client.get(f"/api/meetings/{code}/transcript?lang=hi")).json()
    assert [(s["speaker"], s["text"], s["translation"]) for s in d["segments"]] == [
        ("Ben Das", "We ship on Friday.", "हम शुक्रवार को शिप करेंगे।"), ("Asha Rao", "Agreed.", None)]
    for c in (ben, cara, zed):
        await c.aclose()


async def test_only_participants_read_a_transcript(client):
    code, ben, cara, zed, ben_id = await _setup(client)
    await _say(code, [("Ben Das", ben_id, "Secret plan.")])
    assert (await ben.get(f"/api/meetings/{code}/transcript")).status_code == 200      # invited
    assert (await cara.get(f"/api/meetings/{code}/transcript")).status_code == 403     # same org, not in it
    assert (await zed.get(f"/api/meetings/{code}/transcript")).status_code == 404      # another org
    for c in (ben, cara, zed):
        await c.aclose()


async def test_summary_keeps_only_grounded_items_and_finds_owners(client, models):
    code, ben, cara, zed, ben_id = await _setup(client)
    await _say(code, [("Ben Das", ben_id, "I'll send the report by Friday."), ("Asha Rao", None, "Then we launch on the 20th.")])
    models.append({"summary": "The team agreed to launch on the 20th.",
                   "decisions": [{"text": "Launch on the 20th", "lines": [2]}, {"text": "Invented decision", "lines": [99]}],
                   "action_items": [{"task": "Send the report", "owner": "Ben", "due": "Friday", "lines": [1]},
                                    {"task": "Uncited task", "owner": "", "due": "", "lines": []}]})
    s = (await client.post(f"/api/meetings/{code}/summary")).json()["summary"]
    assert s["summary"] == "The team agreed to launch on the 20th."
    assert [d["text"] for d in s["decisions"]] == ["Launch on the 20th"]
    assert [(a["task"], a["owner"], a["ownerUserId"]) for a in s["actionItems"]] == [("Send the report", "Ben", ben_id)]
    assert (await cara.post(f"/api/meetings/{code}/summary")).status_code == 403
    for c in (ben, cara, zed):
        await c.aclose()


async def test_ended_meetings_get_a_summary_and_an_index(client, models):
    code, ben, cara, zed, ben_id = await _setup(client)
    await _say(code, [("Ben Das", ben_id, "The Whitefield upgrade report is due on Friday.")])
    await client.post(f"/api/meetings/{code}/end")
    models.append({"summary": "Report due Friday.", "decisions": [], "action_items": []})
    async with db.sessionmaker()() as s:
        await transcripts.background(s, None)
        m = (await s.execute(select(Meeting).where(Meeting.join_code == code))).scalar_one()
        assert (await s.get(MeetingSummary, m.id)).summary == "Report due Friday."
        assert m.indexed_at is not None
    for c in (ben, cara, zed):
        await c.aclose()


def test_chunks_are_timestamped_lines_with_overlap():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)
    segs = [TranscriptSegment(id=i, speaker_name="Ben", text="word " * 60, started_at=t0 + timedelta(seconds=30 * i)) for i in range(1, 7)]
    chunks = search.chunk_lines(segs, t0)
    assert len(chunks) > 1 and all(len(c[0]) <= 900 + 400 for c in chunks)
    assert chunks[0][0].startswith("[00:30] Ben: word")
    assert chunks[1][1] == chunks[0][2]                    # one line of overlap


async def test_answers_cite_only_real_quotes_from_meetings_you_were_in(client, models):
    code, ben, cara, zed, ben_id = await _setup(client)
    await _say(code, [("Ben Das", ben_id, "The Whitefield upgrade report is due on Friday afternoon."),
                      ("Asha Rao", None, "Good, and the launch moves to the twentieth.")])
    # Zed's organisation has a meeting about the same thing: never visible to Asha.
    zm = (await zed.post("/api/meetings", json={"title": "Globex"})).json()["meeting"]["joinCode"]
    await zed.post(f"/api/meetings/{zm}/join", json={"speak": "en", "hear": "en"})
    await _say(zm, [("Zed Other", None, "The Whitefield upgrade report is due Monday.")])
    async with db.sessionmaker()() as s:
        for c in (code, zm):
            await search.index_meeting(s, None, (await s.execute(select(Meeting).where(Meeting.join_code == c))).scalar_one())
    models.append({"answer": "It's due on Friday afternoon.", "citations": [
        {"excerpt": 1, "quote": "report is due on Friday afternoon"},
        {"excerpt": 1, "quote": "this sentence was never said in the meeting"}]})
    r = (await client.post("/api/ask", json={"question": "When is the Whitefield upgrade report due?"})).json()
    assert r["answer"] == "It's due on Friday afternoon."
    assert [(c["speaker"], c["meeting"]["joinCode"]) for c in r["citations"]] == [("Ben Das", code)]
    assert r["citations"][0]["time"] == "00:00"
    # Cara (same org, not in the meeting) finds nothing; nor does a made-up citation.
    models.append({"answer": "Friday.", "citations": [{"excerpt": 1, "quote": "report is due on Friday afternoon"}]})
    assert (await cara.post("/api/ask", json={"question": "When is the Whitefield upgrade report due?"})).json()["citations"] == []
    assert models, "Cara's search found no excerpts, so the model wasn't even asked"
    models.clear()
    models.append({"answer": "Friday.", "citations": [{"excerpt": 1, "quote": "completely invented words here"}]})
    r = (await client.post("/api/ask", json={"question": "When is the report due?", "meetingCode": code})).json()
    assert r["citations"] == [] and r["answer"] == search.NOT_ENOUGH
    for c in (ben, cara, zed):
        await c.aclose()
