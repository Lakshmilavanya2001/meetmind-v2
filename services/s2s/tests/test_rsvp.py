"""Invitation replies (accept / decline) and the assistant's join_meeting, respond_to_invitation and search_transcripts."""
from datetime import datetime, timezone

from conftest import other, signup
from sqlalchemy import text
from test_assistant import calls, chat, later, route, say, team
from test_transcripts import fake_vector

from app import db
from app.models import TranscriptSegment


async def _scheduled(client, ben_id, hour=11):
    start, end = later(hour=hour)
    m = (await client.post("/api/meetings", json={"title": "Planning", "participantIds": [ben_id],
                                                  "scheduledStart": start, "scheduledEnd": end})).json()["meeting"]
    return m["joinCode"], start, end


async def test_accept_and_decline_tell_the_host(client):
    _, ben, ben_id = await team(client)
    code, _, _ = await _scheduled(client, ben_id)
    mine = {m["joinCode"]: m for m in (await client.get("/api/meetings")).json()["meetings"]}
    assert mine[code]["myRsvp"] == "ACCEPTED"                             # the host
    assert (await ben.get("/api/meetings")).json()["meetings"][0]["myRsvp"] == "PENDING"
    assert (await client.post(f"/api/meetings/{code}/rsvp", json={"response": "accept"})).status_code == 409
    r = await ben.post(f"/api/meetings/{code}/rsvp", json={"response": "decline"})
    assert r.status_code == 200 and r.json() == {"rsvp": "DECLINED"}
    people = {p["displayName"]: p["rsvp"] for p in (await client.get(f"/api/meetings/{code}")).json()["participants"]}
    assert people == {"Asha Rao": "ACCEPTED", "Ben Das": "DECLINED"}
    notes = (await client.get("/api/notifications")).json()
    assert any("Ben Das declined “Planning”" in n["title"] for n in notes["items"])
    assert (await ben.post(f"/api/meetings/{code}/rsvp", json={"response": "maybe"})).status_code == 422


async def test_only_invited_people_reply_and_not_to_cancelled_meetings(client):
    code_org, ben, ben_id = await team(client)
    cara = other(client)
    org_code = (await client.get("/api/me")).json()["organization"]["joinCode"]
    await signup(cara, "Cara Iyer", "cara@acme.test", join_code=org_code)
    code, _, _ = await _scheduled(client, ben_id)
    assert (await cara.post(f"/api/meetings/{code}/rsvp", json={"response": "accept"})).status_code == 404
    await client.post(f"/api/meetings/{code}/end", json={})               # never started: cancelled
    assert (await ben.post(f"/api/meetings/{code}/rsvp", json={"response": "accept"})).status_code == 409


async def test_a_declined_meeting_is_not_busy_time(client, script):
    _, ben, ben_id = await team(client)
    code, start, end = await _scheduled(client, ben_id, hour=6)          # 11:30 in Kolkata, a weekday

    async def ben_free():
        script.extend([route(("meetings", "When am I free")),
                       calls(("find_free_slots", {"range_start": start, "range_end": end, "duration_minutes": 30})), say("OK.")])
        ev = await chat(ben, "Am I free then?")
        return next(e for e in ev if e["type"] == "tool_end")["summary"]
    assert await ben_free() == "No common free time"                     # invited, not yet replied: busy
    await ben.post(f"/api/meetings/{code}/rsvp", json={"response": "decline"})
    assert await ben_free() == "1 free slot(s)"                          # declined: free again


async def test_assistant_joins_replies_and_searches(client, script, monkeypatch):
    from app import embeddings

    async def embed(http, texts, kind="passage"):
        return [fake_vector(t) for t in texts]
    monkeypatch.setattr(embeddings, "embed", embed)
    monkeypatch.setattr(embeddings, "configured", lambda: True)
    _, ben, ben_id = await team(client)
    code, _, _ = await _scheduled(client, ben_id)
    script += [route(("meetings", "Join link and accept")),
               calls(("join_meeting", {"meeting": code}), ("respond_to_invitation", {"meeting": code, "response": "accept"})),
               say("Here's the link; you're going.")]
    ev = await chat(ben, "Give me the link for Planning and accept it")
    ends = [e for e in ev if e["type"] == "tool_end"]
    assert ends[0]["ok"] and ends[0]["link"] == f"/m/{code}" and ends[1]["summary"] == "Accepted"
    assert (await ben.get("/api/meetings")).json()["meetings"][0]["myRsvp"] == "ACCEPTED"

    async with db.sessionmaker()() as s:
        mid = (await s.execute(text("SELECT id FROM meetings WHERE join_code = :c"), {"c": code})).scalar()
        now = datetime.now(timezone.utc)
        s.add(TranscriptSegment(meeting_id=mid, speaker_name="Asha", language="en", text="The Whitefield budget is approved at forty lakh.",
                                started_at=now, ended_at=now))
        await s.execute(text("UPDATE meetings SET transcript_updated_at = now(), started_at = now() WHERE id = :m"), {"m": mid})
        await s.commit()
        from app import search
        from app.models import Meeting
        await search.index_meeting(s, None, await s.get(Meeting, mid))
    script += [route(("knowledge", "Find budget talk")), calls(("search_transcripts", {"query": "Whitefield budget"})), say("Found it.")]
    ev = await chat(ben, "Where did we talk about the Whitefield budget?")
    end = next(e for e in ev if e["type"] == "tool_end")
    assert end["ok"] and end["summary"] == "1 matching passage(s)"
    cara = other(client)
    org_code = (await client.get("/api/me")).json()["organization"]["joinCode"]
    await signup(cara, "Cara Iyer", "cara@acme.test", join_code=org_code)
    script += [route(("knowledge", "Find budget talk")), calls(("search_transcripts", {"query": "Whitefield budget"})), say("Nothing.")]
    ev = await chat(cara, "Where did we talk about the Whitefield budget?")
    assert next(e for e in ev if e["type"] == "tool_end")["summary"] == "0 matching passage(s)"   # not her meeting
