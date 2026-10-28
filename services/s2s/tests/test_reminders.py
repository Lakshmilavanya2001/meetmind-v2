"""Reminders, notifications, SMS and phone verification; the reminder tools; the routes."""
import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from conftest import other, signup
from sqlalchemy import func, select, text, update
from starlette.routing import Mount

from app import db
from app.assistant.tools.base import ToolContext, ToolError
from app.assistant.tools.reminders import TOOLS
from app.reminders import sms as smsmod
from app.reminders.models import Notification, PhoneVerification, Reminder, SmsMessage
from app.reminders.service import send_due_reminders

TOOL = {t.name: t for t in TOOLS}
PHONE = "+919812345678"


@pytest.fixture(autouse=True)
def _routes(monkeypatch):
    """Mount this module's router (web.py includes it in production) before the static files."""
    from app.reminders.api import router
    from app.web import app
    if not any(getattr(r, "path", "") == "/api/reminders" for r in app.router.routes):
        app.include_router(router)
        app.router.routes.sort(key=lambda r: isinstance(r, Mount))        # the "/" static mount stays last
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "SMS_ALLOWED_PREFIXES", "SMS_DAILY_LIMIT_PER_USER", "SMS_DAILY_LIMIT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SMS_DEV_OUTBOX", "true")
    smsmod.set_provider_for_tests(None)
    yield
    smsmod.set_provider_for_tests(None)


def soon(minutes=60):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


async def _me(c):
    j = (await c.get("/api/me")).json()
    return uuid.UUID(j["user"]["id"]), uuid.UUID(j["organization"]["id"])


async def _two(client):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    ben = other(client)
    await signup(ben, "Ben Das", "ben@globex.test", org="Globex")
    return ben


async def _verify(c, phone=PHONE):
    """Save the number and verify it with the code from the dev outbox."""
    assert (await c.post("/api/profile/phone", json={"phoneNumber": phone})).status_code == 200
    r = await c.post("/api/profile/phone/verify/start")
    assert r.status_code == 200, r.text
    body = (await c.get("/api/profile/sms")).json()["messages"][0]["body"]
    code = re.search(r"\b(\d{6})\b", body).group(1)
    r = await c.post("/api/profile/phone/verify", json={"code": code})
    assert r.status_code == 200 and r.json() == {"verified": True}, r.text
    return code


async def _make_due(rid=None):
    async with db.sessionmaker()() as s:
        stmt = update(Reminder).values(due_at=datetime.now(timezone.utc) - timedelta(seconds=5))
        await s.execute(stmt.where(Reminder.id == uuid.UUID(rid)) if rid else stmt)
        await s.commit()


async def test_create_list_cancel_own_only(client):
    ben = await _two(client)
    r = await client.post("/api/reminders", json={"text": "  Send the report ", "dueAt": soon()})
    assert r.status_code == 200, r.text
    rem = r.json()["reminder"]
    assert rem["text"] == "Send the report" and rem["status"] == "PENDING" and rem["sms"] is False and rem["when"]
    assert [x["id"] for x in (await client.get("/api/reminders")).json()["reminders"]] == [rem["id"]]
    assert (await ben.get("/api/reminders")).json()["reminders"] == []
    # in the past, too far ahead, empty, no offset
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert (await client.post("/api/reminders", json={"text": "x", "dueAt": past})).json()["detail"] == "That time has already passed."
    assert (await client.post("/api/reminders", json={"text": "x", "dueAt": soon(60 * 24 * 400)})).status_code == 400
    assert (await client.post("/api/reminders", json={"text": " ", "dueAt": soon()})).status_code == 400
    assert (await client.post("/api/reminders", json={"text": "x", "dueAt": "2030-01-01T10:00:00"})).status_code == 422
    # only the owner cancels
    assert (await ben.delete(f"/api/reminders/{rem['id']}")).status_code == 404
    assert (await client.delete("/api/reminders/not-an-id")).status_code == 404
    assert (await client.delete(f"/api/reminders/{rem['id']}")).json() == {"canceled": True}
    assert (await client.delete(f"/api/reminders/{rem['id']}")).status_code == 404
    assert (await client.get("/api/reminders")).json()["reminders"] == []
    assert (await client.get("/api/reminders?include=all")).json()["reminders"][0]["status"] == "CANCELED"
    await ben.aclose()


async def test_routes_need_a_session(client):
    for method, url in [("GET", "/api/reminders"), ("POST", "/api/reminders"), ("GET", "/api/notifications"),
                        ("POST", "/api/notifications/read"), ("POST", "/api/profile/phone"), ("POST", "/api/profile/phone/verify/start"),
                        ("POST", "/api/profile/phone/verify"), ("PUT", "/api/profile/sms-reminders"), ("GET", "/api/profile/sms")]:
        assert (await client.request(method, url, json={})).status_code == 401, url
    assert (await client.delete(f"/api/reminders/{uuid.uuid4()}")).status_code == 401


async def test_worker_sends_each_due_reminder_once(client):
    ben = await _two(client)
    for i in range(3):
        await client.post("/api/reminders", json={"text": f"Thing {i}", "dueAt": soon()})
    later = (await client.post("/api/reminders", json={"text": "Later", "dueAt": soon(120)})).json()["reminder"]
    async with db.sessionmaker()() as s:
        await s.execute(update(Reminder).where(Reminder.text != "Later").values(due_at=datetime.now(timezone.utc) - timedelta(seconds=5)))
        await s.commit()
    # two workers at once: each reminder is claimed by exactly one
    async with db.sessionmaker()() as s1, db.sessionmaker()() as s2:
        a, b = await asyncio.gather(send_due_reminders(s1), send_due_reminders(s2))
        assert a + b == 3
        assert await send_due_reminders(s1) == 0
    async with db.sessionmaker()() as s:
        assert await s.scalar(select(func.count()).select_from(Notification)) == 3
        statuses = dict((await s.execute(select(Reminder.text, Reminder.status))).all())
    assert statuses == {"Thing 0": "SENT", "Thing 1": "SENT", "Thing 2": "SENT", "Later": "PENDING"}
    n = (await client.get("/api/notifications")).json()
    assert n["unread"] == 3 and n["items"][0]["title"] == "Reminder" and n["items"][0]["link"] == "/reminders"
    assert (await ben.get("/api/notifications")).json() == {"items": [], "unread": 0}
    # mark read: some, then all; Ben can't touch Asha's
    first = n["items"][0]["id"]
    await ben.post("/api/notifications/read", json={"ids": [first]})
    await ben.post("/api/notifications/read", json={})
    assert (await client.get("/api/notifications")).json()["unread"] == 3
    await client.post("/api/notifications/read", json={"ids": [first]})
    assert (await client.get("/api/notifications")).json()["unread"] == 2
    await client.post("/api/notifications/read", json={})
    assert (await client.get("/api/notifications")).json()["unread"] == 0
    assert [r["id"] for r in (await client.get("/api/reminders")).json()["reminders"]] == [later["id"]]
    await ben.aclose()


async def test_stuck_claims_are_recovered(client):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    rid = (await client.post("/api/reminders", json={"text": "Stuck", "dueAt": soon()})).json()["reminder"]["id"]
    async with db.sessionmaker()() as s:
        await s.execute(update(Reminder).values(status="SENDING", sent_at=datetime.now(timezone.utc) - timedelta(minutes=6)))
        await s.commit()
        assert await send_due_reminders(s) == 0
        assert (await s.get(Reminder, uuid.UUID(rid), populate_existing=True)).status == "PENDING"


async def test_sms_reminder_through_the_outbox(client):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    # not verified: refused, nothing created
    r = await client.post("/api/reminders", json={"text": "Call mum", "dueAt": soon(), "sms": True})
    assert r.status_code == 400 and "verified mobile number" in r.json()["detail"]
    assert (await client.put("/api/profile/sms-reminders", json={"enabled": True})).status_code == 400
    await _verify(client)
    assert (await client.put("/api/profile/sms-reminders", json={"enabled": True})).json() == {"enabled": True}
    rem = (await client.post("/api/reminders", json={"text": "Call mum", "dueAt": soon()})).json()["reminder"]
    assert rem["sms"] is True                                      # the default setting
    await _make_due(rem["id"])
    async with db.sessionmaker()() as s:
        assert await send_due_reminders(s) == 1
        r = await s.get(Reminder, uuid.UUID(rem["id"]))
        assert r.status == "SENT" and r.sms_status == "outbox"
    msgs = (await client.get("/api/profile/sms")).json()
    assert msgs["provider"] == "outbox" and msgs["phone"]["verified"] is True
    assert msgs["messages"][0]["body"] == "MeetMind reminder: Call mum" and msgs["messages"][0]["status"] == "OUTBOX"
    assert msgs["messages"][0]["to"] == PHONE and msgs["messages"][0]["purpose"] == "reminder"


async def test_sms_off_and_unverified_number_at_delivery(client):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    await _verify(client)
    rem = (await client.post("/api/reminders", json={"text": "Water plants", "dueAt": soon(), "sms": True})).json()["reminder"]
    await client.post("/api/profile/phone", json={"phoneNumber": "+919800000000"})     # changed since: no longer verified
    await _make_due(rem["id"])
    async with db.sessionmaker()() as s:
        assert await send_due_reminders(s) == 1
        assert (await s.get(Reminder, uuid.UUID(rem["id"]))).sms_status == "skipped: number not verified for SMS"


async def test_phone_verification(client, monkeypatch):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    uid, _ = await _me(client)
    sent: list[tuple[str, str]] = []

    async def fake_send(to, body):
        sent.append((to, body))
        return "SENT", "SM123"
    smsmod.set_provider_for_tests(smsmod.SmsProvider("twilio", fake_send))     # a real provider: codes are masked in the record

    assert (await client.post("/api/profile/phone/verify/start")).json()["detail"] == "Add your mobile number in your profile first."
    assert (await client.post("/api/profile/phone", json={"phoneNumber": "98123"})).status_code == 400
    r = await client.post("/api/profile/phone", json={"phoneNumber": "+91 98123-45678"})
    assert r.json()["phoneNumber"] == PHONE and r.json()["verified"] is False
    assert (await client.post("/api/profile/phone/verify/start")).status_code == 200
    code = re.search(r"\d{6}", sent[-1][1]).group(0)
    async with db.sessionmaker()() as s:
        v = await s.get(PhoneVerification, uid)
        assert v.phone == PHONE and code not in v.code_hash and len(v.code_hash) == 64
        stored = (await s.execute(select(SmsMessage.body))).scalar_one()
        assert code not in stored and "••••••" in stored
    # wrong codes: five attempts, then the code is spent (even the right one)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    assert (await client.post("/api/profile/phone/verify", json={"code": "12"})).status_code == 400
    for _ in range(5):
        r = await client.post("/api/profile/phone/verify", json={"code": wrong})
        assert r.status_code == 400 and r.json()["detail"] == "That code isn't right."
    r = await client.post("/api/profile/phone/verify", json={"code": code})
    assert r.status_code == 429 and r.json()["detail"] == "Too many wrong codes. Send a new one."
    # a new code works
    await client.post("/api/profile/phone/verify/start")
    code = re.search(r"\d{6}", sent[-1][1]).group(0)
    assert (await client.post("/api/profile/phone/verify", json={"code": code})).json() == {"verified": True}
    from app.security import reset_rate_limits
    reset_rate_limits()                                                        # 3 starts per 10 minutes
    assert (await client.post("/api/profile/phone/verify/start")).json()["alreadyVerified"] is True
    assert (await client.put("/api/profile/sms-reminders", json={"enabled": True})).status_code == 200
    async with db.sessionmaker()() as s:
        assert await s.get(PhoneVerification, uid) is None
    # changing the number un-verifies it and turns SMS reminders off; the same number changes nothing
    st = (await client.post("/api/profile/phone", json={"phoneNumber": PHONE})).json()
    assert st["verified"] is True and st["smsReminders"] is True
    st = (await client.post("/api/profile/phone", json={"phoneNumber": "+919811111111"})).json()
    assert st["verified"] is False and st["smsReminders"] is False and st["verifiedAt"] is None
    # an outbox-verified number never counts for the real provider
    smsmod.set_provider_for_tests(None)
    await _verify(client, "+919822222222")
    smsmod.set_provider_for_tests(smsmod.SmsProvider("twilio", fake_send))
    assert (await client.get("/api/profile/sms")).json()["phone"]["verified"] is False


async def test_codes_per_number_and_start_rate_limit(client, monkeypatch):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    await client.post("/api/profile/phone", json={"phoneNumber": PHONE})
    for _ in range(3):
        assert (await client.post("/api/profile/phone/verify/start")).status_code == 200
    assert (await client.post("/api/profile/phone/verify/start")).status_code == 429          # 3 per 10 minutes
    monkeypatch.setenv("SMS_VERIFY_STARTS_PER_10MIN", "50")
    from app.security import reset_rate_limits
    reset_rate_limits()
    for _ in range(2):
        assert (await client.post("/api/profile/phone/verify/start")).status_code == 200
    r = await client.post("/api/profile/phone/verify/start")                                  # 5 codes a day per number
    assert r.status_code == 429 and "Too many codes" in r.json()["detail"]


async def test_disallowed_country_and_no_provider(client, monkeypatch):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    uid, _ = await _me(client)
    await client.post("/api/profile/phone", json={"phoneNumber": "+14155550100"})
    r = await client.post("/api/profile/phone/verify/start")
    assert r.status_code == 400 and r.json()["detail"] == "Text messages to this country aren't enabled on this server."
    async with db.sessionmaker()() as s:
        res = await smsmod.send_sms(s, uid, "+14155550100", "hi", "reminder")
        assert not res.ok and res.error == "This country isn't enabled for text messages."
    monkeypatch.setenv("SMS_ALLOWED_PREFIXES", "+91, +1")
    assert (await client.post("/api/profile/phone/verify/start")).status_code == 200
    monkeypatch.delenv("SMS_DEV_OUTBOX")
    r = await client.post("/api/profile/phone/verify/start")
    assert r.status_code == 503 and r.json()["detail"] == "Text messages aren't set up on this server."
    r = await client.post("/api/reminders", json={"text": "x", "dueAt": soon(), "sms": True})
    assert r.status_code == 400 and "aren't set up" in r.json()["detail"]
    assert (await client.get("/api/profile/sms")).json()["provider"] is None


async def test_daily_caps(client, monkeypatch):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    uid, _ = await _me(client)
    monkeypatch.setenv("SMS_DAILY_LIMIT_PER_USER", "2")
    async with db.sessionmaker()() as s:
        for _ in range(2):
            assert (await smsmod.send_sms(s, uid, PHONE, "hi", "reminder")).ok
        res = await smsmod.send_sms(s, uid, PHONE, "hi", "reminder")
        assert not res.ok and res.error == "Daily text-message limit reached for this account."
        monkeypatch.setenv("SMS_DAILY_LIMIT", "2")
        res = await smsmod.send_sms(s, None, PHONE, "hi", "summary")
        assert not res.ok and res.error == "Daily text-message limit reached for this server."
        # failed sends are recorded and don't count
        assert await s.scalar(select(func.count()).select_from(SmsMessage).where(SmsMessage.status == "FAILED")) == 2


async def _ctx(client, s) -> ToolContext:
    uid, oid = await _me(client)
    cid = (await s.execute(text("INSERT INTO agent_conversations (organization_id, user_id, title) VALUES (:o, :u, 'Chat') RETURNING id"),
                           {"o": oid, "u": uid})).scalar_one()
    await s.commit()
    return ToolContext(s=s, user_id=uid, org_id=oid, user_name="Asha Rao", user_email="asha@acme.test", timezone="Asia/Kolkata",
                       role="OWNER", conversation_id=cid, meeting_code=None, now=datetime.now(timezone.utc))


async def test_tools(client):
    ben = await _two(client)
    assert TOOL["create_reminder"].kind == "sensitive" and TOOL["list_reminders"].kind == "read" and TOOL["cancel_reminder"].kind == "sensitive"
    assert set(TOOL["create_reminder"].spec()["function"]["parameters"]["properties"]) == {"text", "remind_at", "sms"}
    async with db.sessionmaker()() as s:
        t = await _ctx(client, s)
        args = TOOL["create_reminder"].args.model_validate({"text": "Send the deck", "remind_at": "2099-01-01T15:00:00+05:30"})
        with pytest.raises(ToolError) as e:   # too far ahead
            await TOOL["create_reminder"].preflight(t, args)
        due = (datetime.now(timezone.utc) + timedelta(days=1)).replace(second=0, microsecond=0)
        args = TOOL["create_reminder"].args.model_validate({"text": "Send the deck", "remind_at": due.isoformat()})
        sentence = await TOOL["create_reminder"].preflight(t, args)
        local = due.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
        assert sentence == f"Remind you on {local:%a} {local.day} {local:%b %Y, %H:%M}: “Send the deck”"
        assert await s.scalar(select(func.count()).select_from(Reminder)) == 0      # the preflight creates nothing
        sms_args = TOOL["create_reminder"].args.model_validate({"text": "x", "remind_at": due.isoformat(), "sms": True})
        with pytest.raises(ToolError) as e:
            await TOOL["create_reminder"].preflight(t, sms_args)
        assert "verified mobile number" in e.value.message
        out = await TOOL["create_reminder"].run(t, args)
        assert out.summary.startswith("Reminder · ") and out.data["text"] == "Send the deck" and out.link == "/reminders"
        listed = await TOOL["list_reminders"].run(t, TOOL["list_reminders"].args())
        assert listed.summary == "1 upcoming reminder(s)" and listed.data["reminders"][0]["id"] == out.data["id"]
        cancel = TOOL["cancel_reminder"].args(reminder_id=out.data["id"])
        assert (await TOOL["cancel_reminder"].preflight(t, cancel)).startswith("Cancel the reminder “Send the deck” (")
        assert (await TOOL["cancel_reminder"].run(t, cancel)).summary == "Reminder cancelled"
        with pytest.raises(ToolError) as e:
            await TOOL["cancel_reminder"].preflight(t, cancel)
        assert e.value.code == "NOT_FOUND" and e.value.message == "No upcoming reminder with that id."
        # someone else's reminder is invisible to the tools
        bens = (await ben.post("/api/reminders", json={"text": "Ben's", "dueAt": soon()})).json()["reminder"]["id"]
        with pytest.raises(ToolError):
            await TOOL["cancel_reminder"].preflight(t, TOOL["cancel_reminder"].args(reminder_id=bens))
        with pytest.raises(ToolError):
            await TOOL["cancel_reminder"].run(t, TOOL["cancel_reminder"].args(reminder_id=bens))
    assert len((await ben.get("/api/reminders")).json()["reminders"]) == 1
    await ben.aclose()
