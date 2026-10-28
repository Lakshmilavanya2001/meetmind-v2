"""The assistant's engine, its meeting tools, confirmations, plans and "Hey Mind" by voice, with a scripted
stand-in for the LLM (each reply is popped from a list, in the order the engine asks)."""
import json
from datetime import datetime, timedelta, timezone

from conftest import other, signup
from sqlalchemy import select, text

from app import db
from app.models import Meeting, MeetingParticipant


def route(*steps):
    return {"content": json.dumps({"steps": [{"agent": a, "task": t} for a, t in steps]})}


def calls(*items):
    return {"content": "", "tool_calls": [{"id": f"c{i}-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                                          for i, (name, args) in enumerate(items)]}


def say(t):
    return {"content": t}


def later(days=2, hour=11):
    d = (datetime.now(timezone.utc) + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return d.isoformat(), (d + timedelta(minutes=30)).isoformat()


async def chat(c, message, **kw):
    r = await c.post("/api/assistant/chat", json={"message": message, **kw})
    assert r.status_code == 200, r.text
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


async def team(client):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    code = (await client.get("/api/me")).json()["organization"]["joinCode"]
    ben = other(client)
    await signup(ben, "Ben Das", "ben@acme.test", join_code=code)
    ben_id = (await ben.get("/api/me")).json()["user"]["id"]
    return code, ben, ben_id


async def test_schedule_waits_for_confirm_then_happens_once(client, script):
    _, ben, ben_id = await team(client)
    start, end = later()
    script += [route(("meetings", "Schedule a design review with Ben")),
               calls(("search_users", {"query": "Ben"})),
               calls(("create_meeting", {"title": "Design review", "start_time": start, "end_time": end, "participant_ids": [ben_id]})),
               say("It's waiting for your confirmation.")]
    ev = await chat(client, "Schedule a design review with Ben the day after tomorrow at 11")
    kinds = [e["type"] for e in ev]
    assert kinds[0] == "conversation" and kinds[-1] == "done" and "guard" not in kinds
    conf = next(e for e in ev if e["type"] == "confirmation")
    assert conf["summary"].startswith("Schedule “Design review” for ") and "Ben Das (ben@acme.test)" in conf["summary"]
    async with db.sessionmaker()() as s:
        assert (await s.execute(select(Meeting))).scalars().all() == []          # nothing until Confirm

    assert (await ben.post(f"/api/assistant/actions/{conf['actionId']}", json={"decision": "confirm"})).status_code == 404
    r = await client.post(f"/api/assistant/actions/{conf['actionId']}", json={"decision": "confirm"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["message"]["content"].startswith("✓ Done: Scheduled")
    assert (await client.post(f"/api/assistant/actions/{conf['actionId']}", json={"decision": "confirm"})).status_code == 409
    async with db.sessionmaker()() as s:
        m = (await s.execute(select(Meeting))).scalar_one()
        people = (await s.execute(select(MeetingParticipant).where(MeetingParticipant.meeting_id == m.id))).scalars().all()
        assert m.title == "Design review" and {str(p.user_id) for p in people if p.role == "ATTENDEE"} == {ben_id}
    conv = (await client.get(f"/api/assistant/conversations/{ev[0]['id']}")).json()
    tool = next(i for i in conv["items"] if i["kind"] == "tool" and i["name"] == "create_meeting")
    assert tool["decision"] == "confirmed"
    assert (await ben.get(f"/api/assistant/conversations/{ev[0]['id']}")).status_code == 404


async def test_ambiguous_person_is_refused_until_the_user_picks(client, script):
    code, _, _ = await team(client)
    p1, p2 = other(client), other(client)
    await signup(p1, "Priya Shah", "priya.s@acme.test", join_code=code)
    await signup(p2, "Priya Nair", "priya.n@acme.test", join_code=code)
    p1_id = (await p1.get("/api/me")).json()["user"]["id"]
    start, end = later()
    script += [route(("meetings", "Invite Priya to a sync")),
               calls(("search_users", {"query": "Priya"})),
               calls(("create_meeting", {"title": "Sync", "start_time": start, "end_time": end, "participant_ids": [p1_id]})),
               say("Which Priya do you mean: Priya Shah or Priya Nair?")]
    ev = await chat(client, "Set up a sync with Priya")
    ends = [e for e in ev if e["type"] == "tool_end"]
    assert ends[0]["candidates"] and len(ends[0]["candidates"]) == 2
    assert ends[1]["ok"] is False and "Ambiguous person" in ends[1]["summary"]
    assert not any(e["type"] == "confirmation" for e in ev)

    # The user picks by email: now it may go ahead.
    script += [route(("meetings", "Use Priya Shah")),
               calls(("create_meeting", {"title": "Sync", "start_time": start, "end_time": end, "participant_ids": [p1_id]})),
               say("Waiting for your confirmation.")]
    ev = await chat(client, "priya.s@acme.test please", conversationId=ev[0]["id"])
    assert any(e["type"] == "confirmation" for e in ev)


async def test_claims_without_a_tool_get_a_correction(client, script):
    await team(client)
    script += [route(("meetings", "Schedule a review")), say("I've scheduled the review for tomorrow.")]
    ev = await chat(client, "Schedule a review tomorrow")
    guard = next(e for e in ev if e["type"] == "guard")
    assert "creation or booking" in guard["message"] and "nothing was changed" in guard["message"]


async def test_agents_only_use_their_own_tools(client, script):
    await team(client)
    script += [route(("knowledge", "What was decided?")),
               calls(("create_meeting", {"title": "x", "start_time": later()[0], "end_time": later()[1]})),
               say("I can't schedule meetings.")]
    ev = await chat(client, "What was decided yesterday?")
    end = next(e for e in ev if e["type"] == "tool_end" or e["type"] == "text")
    assert not any(e["type"] == "confirmation" for e in ev)
    convs = (await client.get("/api/assistant/conversations")).json()["conversations"]
    items = (await client.get(f"/api/assistant/conversations/{convs[0]['id']}")).json()["items"]
    assert any(i["kind"] == "tool" and i["summary"] == "Not available to this agent" for i in items), end


async def test_conflicts_are_reported_with_who_is_busy(client, script):
    _, ben, ben_id = await team(client)
    start, end = later(hour=6)
    r = await ben.post("/api/meetings", json={"title": "Ben's 1:1", "scheduledStart": start, "scheduledEnd": end})
    assert r.status_code == 200
    script += [route(("meetings", "Meet Ben")),
               calls(("create_meeting", {"title": "Catch-up", "start_time": start, "end_time": end, "participant_ids": [ben_id]})),
               say("Ben is busy then.")]
    ev = await chat(client, "Catch up with Ben")
    fail = next(e for e in ev if e["type"] == "tool_end")
    assert fail["ok"] is False and "Ben Das already has something" in fail["summary"]


async def test_meeting_tools_respect_roles(client, script):
    _, ben, ben_id = await team(client)
    m = (await client.post("/api/meetings", json={"title": "Asha's meeting", "participantIds": [ben_id], **dict(zip(("scheduledStart", "scheduledEnd"), later()))})).json()["meeting"]
    script += [route(("meetings", "Cancel it")), calls(("cancel_meeting", {"meeting": m["joinCode"]})), say("Only the host can.")]
    ev = await chat(ben, "Cancel Asha's meeting")
    fail = next(e for e in ev if e["type"] == "tool_end")
    assert fail["ok"] is False and "Only the host can cancel" in fail["summary"]
    script += [route(("meetings", "List")), calls(("list_meetings", {"scope": "upcoming"})), say("One meeting.")]
    ev = await chat(ben, "What's coming up?")
    assert next(e for e in ev if e["type"] == "tool_end")["summary"] == "1 meeting(s)"


async def test_plan_runs_every_step_after_one_approval(client, script):
    _, _, ben_id = await team(client)
    start, end = later()
    remind = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    script += [route(("planner", "Plan a review with Ben and remind me")),
               calls(("propose_plan", {"goal": "Review with Ben", "steps": [
                   {"tool": "create_meeting", "args": {"title": "Review", "start_time": start, "end_time": end, "participant_ids": [ben_id]}, "why": "The meeting"},
                   {"tool": "create_reminder", "args": {"text": "Prepare the review", "remind_at": remind}, "why": "Prep", "depends_on": [1]}]})),
               say("Here's the plan.")]
    ev = await chat(client, "Plan a review with Ben and remind me to prepare")
    plan = next(e for e in ev if e["type"] == "plan")["plan"]
    assert plan["status"] == "proposed" and len(plan["steps"]) == 2 and plan["steps"][1]["dependsOn"] == [1]
    r = await client.post(f"/api/assistant/plans/{plan['id']}", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["plan"]["status"] == "completed" and [s["status"] for s in body["plan"]["steps"]] == ["done", "done"]
    assert (await client.post(f"/api/assistant/plans/{plan['id']}", json={"decision": "approve"})).status_code == 409
    async with db.sessionmaker()() as s:
        assert (await s.execute(text("SELECT count(*) FROM meetings"))).scalar() == 1
        assert (await s.execute(text("SELECT count(*) FROM reminders"))).scalar() == 1


async def test_invalid_plan_step_goes_back_to_the_planner(client, script):
    await team(client)
    script += [route(("planner", "Plan")),
               calls(("propose_plan", {"goal": "Bad", "steps": [{"tool": "cancel_meeting", "args": {}, "why": "nope"}]})),
               say("I couldn't make a plan.")]
    ev = await chat(client, "Plan something")
    fail = next(e for e in ev if e["type"] == "tool_end")
    assert fail["ok"] is False and "can't be part of a plan" in fail["summary"]


async def test_hey_mind_confirms_by_voice(client, script):
    from app.assistant.call import CallMemory
    from app.assistant.voice import VoiceAssistant
    _, _, ben_id = await team(client)
    me = (await client.get("/api/me")).json()["user"]["id"]
    m = (await client.post("/api/meetings", json={"title": "Standup"})).json()["meeting"]
    await client.post(f"/api/meetings/{m['joinCode']}/join", json={"speak": "en", "hear": "en"})
    start, end = later()
    script += [route(("meetings", "Schedule a follow-up with Ben")),
               calls(("create_meeting", {"title": "Follow-up", "start_time": start, "end_time": end, "participant_ids": [ben_id]})),
               say("I'll schedule the follow-up with Ben.")]
    va = VoiceAssistant(f"mm-{m['joinCode']}")
    r = await va.handle(None, me, "Asha Rao", "en", "schedule a follow-up with Ben", ["Asha Rao"], CallMemory())
    assert r.expects_answer and "Say yes" in r.text
    voice_prompt = next(x for x in script.seen if x.get("tools"))["messages"][0]["content"]
    assert "VOICE MODE" in voice_prompt and "Standup" in voice_prompt
    async with db.sessionmaker()() as s:
        assert (await s.execute(text("SELECT count(*) FROM meetings WHERE title = 'Follow-up'"))).scalar() == 0
    r = await va.handle(None, me, "Asha Rao", "en", "Yes, go ahead.", ["Asha Rao"], CallMemory())
    assert r.text.startswith("Done: Scheduled") and not r.expects_answer
    async with db.sessionmaker()() as s:
        assert (await s.execute(text("SELECT count(*) FROM meetings WHERE title = 'Follow-up'"))).scalar() == 1
    # Someone not in the meeting (or an anonymous demo room) gets the quick call-only answer.
    script += [say("Nobody has spoken yet.")]
    r = await VoiceAssistant("some-demo-room").handle(None, "guest-abc", "Guest", "en", "what did they say?", [], CallMemory())
    assert r.text == "Nobody has spoken yet."


async def test_hey_mind_and_the_call_panel_share_one_conversation(client, script):
    from app.assistant.call import CallMemory
    from app.assistant.voice import VoiceAssistant
    await team(client)
    me = (await client.get("/api/me")).json()["user"]["id"]
    m = (await client.post("/api/meetings", json={"title": "Standup"})).json()["meeting"]
    await client.post(f"/api/meetings/{m['joinCode']}/join", json={"speak": "en", "hear": "en"})
    script += [route(("general", "hello")), say("Hi there.")]
    ev = await chat(client, "hello", meetingCode=m["joinCode"])               # typed in the call's panel
    panel = ev[0]["id"]
    listed = (await client.get(f"/api/assistant/conversations?meeting={m['joinCode']}")).json()["conversations"]
    assert [c["id"] for c in listed] == [panel]
    assert (await client.get("/api/assistant/conversations?meeting=zzz-zzzz-zzz")).json()["conversations"] == []
    script += [route(("general", "thanks")), say("You're welcome.")]
    r = await VoiceAssistant(f"mm-{m['joinCode']}").handle(None, me, "Asha Rao", "en", "thanks", ["Asha Rao"], CallMemory())
    assert r.conversation == panel and r.text == "You're welcome."             # Hey Mind continued it
    items = (await client.get(f"/api/assistant/conversations/{panel}")).json()["items"]
    assert [i["text"] for i in items if i["kind"] == "user"] == ["hello", "thanks"]
