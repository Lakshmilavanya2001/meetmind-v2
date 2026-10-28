"""Meetings: create, schedule, invite, join (the call token), end; who may do what."""
from datetime import datetime, timedelta, timezone

from conftest import jwt_claims, other, signup
from sqlalchemy import update

from app import db
from app.meetings import end_abandoned
from app.models import Meeting


async def _org(client):
    """Asha (owner) and Ben (member) in Acme; Zed in Globex. Returns the extra clients."""
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    code = (await client.get("/api/me")).json()["organization"]["joinCode"]
    ben, zed = other(client), other(client)
    await signup(ben, "Ben Das", "ben@acme.test", join_code=code)
    await signup(zed, "Zed Other", "zed@globex.test", org="Globex")
    ben_id = (await ben.get("/api/me")).json()["user"]["id"]
    zed_id = (await zed.get("/api/me")).json()["user"]["id"]
    return ben, zed, ben_id, zed_id


async def test_start_now_and_join_with_a_token_for_the_room(client):
    ben, zed, ben_id, _ = await _org(client)
    m = (await client.post("/api/meetings", json={"title": "Standup"})).json()["meeting"]
    assert m["status"] == "SCHEDULED" and m["myRole"] == "HOST" and len(m["joinCode"]) == 12
    r = await client.post(f"/api/meetings/{m['joinCode']}/join", json={"speak": "hi", "hear": "en", "voice": "male", "noise": "studio"})
    assert r.status_code == 200, r.text
    j = r.json()
    claims = jwt_claims(j["token"])
    me = (await client.get("/api/me")).json()["user"]
    assert claims["sub"] == me["id"] and claims["name"] == "Asha Rao"
    assert claims["video"]["room"] == f"mm-{m['joinCode']}" and claims["video"]["roomJoin"] is True
    assert claims["attributes"]["speak"] == "hi" and claims["attributes"]["hear"] == "en" and claims["attributes"]["role"] == "HOST"
    assert claims["attributes"]["noise"] in ("studio", "standard")          # studio only when BNR is set up
    assert claims["roomConfig"]["agents"][0]["agentName"]
    assert (await client.get(f"/api/meetings/{m['joinCode']}")).json()["meeting"]["status"] == "LIVE"
    # Ben (same org) joins by link and becomes an attendee; his preferences are remembered.
    assert (await ben.post(f"/api/meetings/{m['joinCode']}/join", json={"speak": "en", "hear": "hi"})).status_code == 200
    got = (await ben.get(f"/api/meetings/{m['joinCode']}")).json()
    assert got["meeting"]["myRole"] == "ATTENDEE" and got["myPreferences"]["speak"] == "en"
    assert {p["displayName"] for p in got["participants"]} == {"Asha Rao", "Ben Das"}
    await ben.aclose()
    await zed.aclose()


async def test_other_organisations_cant_see_or_join(client):
    ben, zed, _, zed_id = await _org(client)
    m = (await client.post("/api/meetings", json={"title": "Private"})).json()["meeting"]
    assert (await zed.get(f"/api/meetings/{m['joinCode']}")).status_code == 404
    assert (await zed.post(f"/api/meetings/{m['joinCode']}/join", json={"speak": "en", "hear": "en"})).status_code == 404
    assert (await client.post("/api/meetings", json={"title": "x", "participantIds": [zed_id]})).status_code == 400
    assert (await client.post(f"/api/meetings/{m['joinCode']}/invite", json={"participantIds": [zed_id]})).status_code == 400
    await ben.aclose()
    await zed.aclose()


async def test_scheduling_and_invites(client):
    ben, zed, ben_id, _ = await _org(client)
    start = datetime.now(timezone.utc) + timedelta(days=1)
    body = {"title": "Review", "scheduledStart": start.isoformat(), "scheduledEnd": (start + timedelta(minutes=30)).isoformat(), "participantIds": [ben_id]}
    m = (await client.post("/api/meetings", json=body)).json()["meeting"]
    assert m["scheduledStart"]
    assert [x["title"] for x in (await ben.get("/api/meetings")).json()["meetings"]] == ["Review"]      # Ben was invited
    bad = {**body, "scheduledEnd": start.isoformat()}
    assert (await client.post("/api/meetings", json=bad)).status_code == 422
    past = {**body, "scheduledStart": (start - timedelta(days=2)).isoformat(), "scheduledEnd": (start - timedelta(days=2) + timedelta(minutes=30)).isoformat()}
    assert (await client.post("/api/meetings", json=past)).status_code == 400
    assert (await client.post("/api/meetings", json={**body, "scheduledEnd": None})).status_code == 422
    # only the host or a co-host invites
    assert (await ben.post(f"/api/meetings/{m['joinCode']}/invite", json={"participantIds": [ben_id]})).status_code == 403
    await ben.aclose()
    await zed.aclose()


async def test_ending(client):
    ben, zed, ben_id, _ = await _org(client)
    m = (await client.post("/api/meetings", json={"title": "Sync", "participantIds": [ben_id]})).json()["meeting"]
    code = m["joinCode"]
    assert (await client.post(f"/api/meetings/{code}/end")).json()["meeting"]["status"] == "CANCELLED"   # never started
    m2 = (await client.post("/api/meetings", json={"title": "Sync 2", "participantIds": [ben_id]})).json()["meeting"]
    await client.post(f"/api/meetings/{m2['joinCode']}/join", json={"speak": "en", "hear": "en"})
    assert (await ben.post(f"/api/meetings/{m2['joinCode']}/end")).status_code == 403
    assert (await client.post(f"/api/meetings/{m2['joinCode']}/end")).json()["meeting"]["status"] == "ENDED"
    assert (await ben.post(f"/api/meetings/{m2['joinCode']}/join", json={"speak": "en", "hear": "en"})).status_code == 409
    assert (await client.post(f"/api/meetings/{m2['joinCode']}/join", json={"speak": "xx", "hear": "en"})).status_code == 400
    await ben.aclose()
    await zed.aclose()


async def test_meetings_whose_room_closed_are_ended(client):
    ben, zed, _, _ = await _org(client)
    m = (await client.post("/api/meetings", json={"title": "Left"})).json()["meeting"]
    await client.post(f"/api/meetings/{m['joinCode']}/join", json={"speak": "en", "hear": "en"})
    async with db.sessionmaker()() as s:
        await s.execute(update(Meeting).values(started_at=datetime.now(timezone.utc) - timedelta(minutes=10)))
        await s.commit()
        assert await end_abandoned(s, {f"mm-{m['joinCode']}"}) == 0       # the room is still there
        assert await end_abandoned(s, set()) == 1                         # gone: over
    assert (await client.get(f"/api/meetings/{m['joinCode']}")).json()["meeting"]["status"] == "ENDED"
    await ben.aclose()
    await zed.aclose()


async def test_pages_need_a_session(client):
    r = await client.get("/dashboard", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/login"
    r = await client.get("/m/abc-defg-hij", follow_redirects=False)
    assert r.headers["location"] == "/login?next=/m/abc-defg-hij"
    assert (await client.post("/api/token", json={"name": "x", "room": "r", "speak": "en", "hear": "en"})).status_code == 404
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    assert (await client.get("/dashboard")).status_code == 200
