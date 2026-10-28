"""Housekeeping (expiry, retention), deleting a transcript, the readiness check and the page routes."""
from datetime import datetime, timedelta, timezone

from conftest import other, signup
from sqlalchemy import text, update

from app import db, maintenance, readiness
from app.models import Meeting, TranscriptSegment


async def _meeting_with_lines(client, ended_days_ago=None):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    code = (await client.get("/api/me")).json()["organization"]["joinCode"]
    ben = other(client)
    await signup(ben, "Ben Das", "ben@acme.test", join_code=code)
    ben_id = (await ben.get("/api/me")).json()["user"]["id"]
    m = (await client.post("/api/meetings", json={"title": "Review", "participantIds": [ben_id]})).json()["meeting"]
    now = datetime.now(timezone.utc)
    async with db.sessionmaker()() as s:
        mid = (await s.execute(text("SELECT id FROM meetings WHERE join_code = :c"), {"c": m["joinCode"]})).scalar()
        for i in range(3):
            s.add(TranscriptSegment(meeting_id=mid, speaker_name="Asha", language="en", text=f"line {i}", started_at=now, ended_at=now))
        vals = {"transcript_updated_at": now, "started_at": now - timedelta(hours=1)}
        if ended_days_ago is not None:
            vals |= {"status": "ENDED", "ended_at": now - timedelta(days=ended_days_ago)}
        await s.execute(update(Meeting).where(Meeting.id == mid).values(**vals))
        await s.commit()
    return m["joinCode"], ben, mid


async def _lines(mid):
    async with db.sessionmaker()() as s:
        return (await s.execute(text("SELECT count(*) FROM transcript_segments WHERE meeting_id = :m"), {"m": mid})).scalar()


async def test_only_the_host_deletes_a_transcript(client):
    code, ben, mid = await _meeting_with_lines(client)
    page = (await client.get(f"/api/meetings/{code}/transcript")).json()
    assert page["meeting"]["canDelete"] and len(page["segments"]) == 3
    assert (await ben.get(f"/api/meetings/{code}/transcript")).json()["meeting"]["canDelete"] is False
    assert (await ben.delete(f"/api/meetings/{code}/transcript")).status_code == 403
    assert await _lines(mid) == 3
    r = await client.delete(f"/api/meetings/{code}/transcript")
    assert r.status_code == 200 and r.json() == {"deleted": 3}
    assert await _lines(mid) == 0
    async with db.sessionmaker()() as s:
        assert (await s.execute(text("SELECT count(*) FROM audit_log WHERE action = 'transcript.delete'"))).scalar() == 1


async def test_retention_deletes_only_old_ended_meetings(client, monkeypatch):
    _, _, old = await _meeting_with_lines(client, ended_days_ago=40)
    async with db.sessionmaker()() as s:
        assert (await maintenance.run(s)).get("transcripts") is None      # not set: kept forever
    assert await _lines(old) == 3
    monkeypatch.setenv("TRANSCRIPT_RETENTION_DAYS", "30")
    async with db.sessionmaker()() as s:
        mid_recent = (await s.execute(text("SELECT id FROM meetings WHERE id <> :m LIMIT 1"), {"m": old})).scalar()
        assert mid_recent is None
        assert (await maintenance.run(s))["transcripts"] == 1
    assert await _lines(old) == 0


async def test_recent_meetings_keep_their_transcript(client, monkeypatch):
    _, _, mid = await _meeting_with_lines(client, ended_days_ago=5)
    monkeypatch.setenv("TRANSCRIPT_RETENTION_DAYS", "30")
    async with db.sessionmaker()() as s:
        await maintenance.run(s)
    assert await _lines(mid) == 3


async def test_housekeeping_expires_sessions_and_confirmations(client):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    me = (await client.get("/api/me")).json()
    async with db.sessionmaker()() as s:
        conv = (await s.execute(text("INSERT INTO agent_conversations (organization_id, user_id, title) VALUES (:o, :u, 't') RETURNING id"),
                                {"o": me["organization"]["id"], "u": me["user"]["id"]})).scalar()
        await s.execute(text("INSERT INTO agent_actions (conversation_id, organization_id, user_id, tool_name, status, created_at) "
                             "VALUES (:c, :o, :u, 'create_meeting', 'PENDING_CONFIRMATION', now() - interval '20 minutes'), "
                             "(:c, :o, :u, 'create_meeting', 'PENDING_CONFIRMATION', now())"),
                        {"c": conv, "o": me["organization"]["id"], "u": me["user"]["id"]})
        await s.execute(text("INSERT INTO sessions (token_hash, user_id, organization_id, expires_at) VALUES ('old', :u, :o, now() - interval '1 day')"),
                        {"o": me["organization"]["id"], "u": me["user"]["id"]})
        await s.commit()
        done = await maintenance.run(s)
        assert done["confirmations"] == 1 and done["sessions"] == 1
        statuses = sorted((await s.execute(text("SELECT status FROM agent_actions"))).scalars())
        assert statuses == ["EXPIRED", "PENDING_CONFIRMATION"]
    assert (await client.get("/api/me")).status_code == 200                # the live session is untouched


async def test_readiness_reports_up_and_down_without_addresses(client, monkeypatch):
    monkeypatch.setenv("ASR_URL", "http://127.0.0.1:9")                    # nothing listens
    r = await client.get("/health/ready")
    body = r.json()
    assert r.status_code == 503 and body["status"] == "degraded"
    assert body["checks"]["database"] == "up" and body["checks"]["speech_recognition"] == "down"
    assert "127.0.0.1" not in r.text
    r = await readiness.check()
    assert set(r["checks"]) >= {"database", "livekit", "call_agent"}


async def test_signed_in_pages_redirect_to_login(client):
    for path in ("/assistant", "/bookings", "/reminders", "/settings", "/dashboard", "/search"):
        r = await client.get(path, follow_redirects=False)
        assert r.status_code in (302, 307) and r.headers["location"].startswith("/login"), path
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    for path in ("/assistant", "/bookings", "/reminders", "/settings"):
        r = await client.get(path)
        assert r.status_code == 200 and "<html" in r.text, path
