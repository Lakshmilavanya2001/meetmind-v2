"""Meetings: start one now or schedule it, invite people from the organisation, join (a LiveKit token
for the meeting's room, with the translator requested), end for everyone.

Who may do what:
- any member of the organisation can create a meeting, and open or join one of its meetings by link;
- the host and co-hosts invite people and end the meeting;
- someone removed from a meeting can't join it again.
"""
from __future__ import annotations

import os
import secrets
import string
from datetime import datetime, timedelta, timezone

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from livekit import api
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import bnr, voices
from .accounts import Ctx, audit, current, get_db
from .languages import HEAR, SPEAK
from .models import Meeting, MeetingParticipant, Membership, User
from .security import rate_limit

router = APIRouter()
API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")
LIVEKIT_HTTP = os.environ.get("LIVEKIT_URL", "http://127.0.0.1:7880").replace("ws", "http", 1)
AGENT_NAME = os.environ.get("S2S_AGENT_NAME", "indic-s2s")
NOISE_MODES = ("off", "standard", "enhanced", "studio")
MODERATORS = ("HOST", "COHOST")


def new_join_code() -> str:
    """abc-defg-hij, like a meeting link."""
    pick = lambda n: "".join(secrets.choice(string.ascii_lowercase) for _ in range(n))  # noqa: E731
    return f"{pick(3)}-{pick(4)}-{pick(3)}"


def meeting_view(m: Meeting, me: MeetingParticipant | None = None, host: User | None = None) -> dict:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {"id": str(m.id), "title": m.title, "joinCode": m.join_code, "status": m.status,
            "scheduledStart": iso(m.scheduled_start), "scheduledEnd": iso(m.scheduled_end),
            "startedAt": iso(m.started_at), "endedAt": iso(m.ended_at),
            "host": {"id": str(host.id), "displayName": host.display_name} if host else {"id": str(m.host_id)},
            "saveTranscript": m.save_transcript,
            "myRole": me.role if me and not me.removed_at else None,
            "myRsvp": me.rsvp if me and not me.removed_at else None}


async def load(s: AsyncSession, ctx: Ctx, code: str) -> tuple[Meeting, MeetingParticipant | None]:
    """A meeting of the caller's organisation, and their participant row (None if not one yet)."""
    m = (await s.execute(select(Meeting).where(Meeting.join_code == code.strip().lower()))).scalar_one_or_none()
    if m is None or m.organization_id != ctx.org.id:
        raise HTTPException(404, "No meeting with that code.")            # same answer for other orgs' meetings
    me = await s.get(MeetingParticipant, (m.id, ctx.user.id))
    if me is not None and me.removed_at is not None:
        raise HTTPException(403, "You were removed from this meeting.")
    return m, me


async def _org_members(s: AsyncSession, ctx: Ctx, ids: list[str]) -> list[User]:
    if not ids:
        return []
    rows = (await s.execute(select(User).join(Membership, Membership.user_id == User.id)
                            .where(Membership.organization_id == ctx.org.id, User.id.in_(ids), User.is_active.is_(True)))).scalars().all()
    if len(rows) != len(set(ids)):
        raise HTTPException(400, "Everyone you invite must be a member of your organisation.")
    return list(rows)


class CreateIn(BaseModel):
    title: str = Field(default="Meeting", min_length=1, max_length=120)
    scheduledStart: datetime | None = None
    scheduledEnd: datetime | None = None
    participantIds: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _times(self):
        if (self.scheduledStart is None) != (self.scheduledEnd is None):
            raise ValueError("Give both a start and an end time, or neither (start now).")
        if self.scheduledStart and (self.scheduledStart.tzinfo is None or self.scheduledEnd.tzinfo is None):
            raise ValueError("Times need a time zone offset.")
        if self.scheduledStart and self.scheduledEnd <= self.scheduledStart:
            raise ValueError("The end must be after the start.")
        return self


@router.post("/api/meetings")
async def create(body: CreateIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    invitees = await _org_members(s, ctx, [i for i in body.participantIds if i != str(ctx.user.id)])
    if body.scheduledStart and body.scheduledStart < datetime.now(timezone.utc) - timedelta(minutes=5):
        raise HTTPException(400, "That start time has already passed.")
    code = new_join_code()
    m = Meeting(organization_id=ctx.org.id, host_id=ctx.user.id, title=body.title.strip(), join_code=code,
                room_name=f"mm-{code}", scheduled_start=body.scheduledStart, scheduled_end=body.scheduledEnd)
    s.add(m)
    await s.flush()
    now = datetime.now(timezone.utc)
    s.add(MeetingParticipant(meeting_id=m.id, user_id=ctx.user.id, role="HOST", invited_at=now, rsvp="ACCEPTED", responded_at=now))
    for u in invitees:
        s.add(MeetingParticipant(meeting_id=m.id, user_id=u.id, role="ATTENDEE", invited_at=now))
    await audit(s, "meeting.create", ctx.org.id, ctx.user.id, "Meeting", m.id, invited=len(invitees),
                scheduled=bool(body.scheduledStart))
    await s.commit()
    return {"meeting": meeting_view(m, None, ctx.user) | {"myRole": "HOST"}}


@router.get("/api/meetings")
async def list_mine(ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    """Meetings I host or was invited to or joined: live and upcoming first, then the last 30 days."""
    since = datetime.now(timezone.utc) - timedelta(days=30)
    rows = (await s.execute(
        select(Meeting, MeetingParticipant, User)
        .join(MeetingParticipant, and_(MeetingParticipant.meeting_id == Meeting.id, MeetingParticipant.user_id == ctx.user.id))
        .join(User, User.id == Meeting.host_id)
        .where(Meeting.organization_id == ctx.org.id, MeetingParticipant.removed_at.is_(None),
               or_(Meeting.status.in_(("LIVE", "SCHEDULED")), Meeting.created_at > since))
        .order_by(Meeting.created_at.desc()).limit(100))).all()
    return {"meetings": [meeting_view(m, p, h) for m, p, h in rows]}


@router.get("/api/meetings/{code}")
async def get_one(code: str, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    m, me = await load(s, ctx, code)
    rows = (await s.execute(select(MeetingParticipant, User).join(User, User.id == MeetingParticipant.user_id)
                            .where(MeetingParticipant.meeting_id == m.id, MeetingParticipant.removed_at.is_(None))
                            .order_by(MeetingParticipant.role, User.display_name))).all()
    host = await s.get(User, m.host_id)
    prefs = {k: getattr(me, k) for k in ("speak", "hear", "voice", "noise")} if me else {}
    return {"meeting": meeting_view(m, me, host),
            "participants": [{"id": str(u.id), "displayName": u.display_name, "role": p.role, "rsvp": p.rsvp,
                              "joined": p.first_joined_at is not None} for p, u in rows],
            "myPreferences": prefs}


class InviteIn(BaseModel):
    participantIds: list[str] = Field(min_length=1, max_length=50)


@router.post("/api/meetings/{code}/invite")
async def invite(code: str, body: InviteIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    m, me = await load(s, ctx, code)
    if not me or me.role not in MODERATORS:
        raise HTTPException(403, "Only the host or a co-host can invite people.")
    if m.status in ("ENDED", "CANCELLED"):
        raise HTTPException(409, "This meeting is over.")
    added = 0
    now = datetime.now(timezone.utc)
    for u in await _org_members(s, ctx, body.participantIds):
        p = await s.get(MeetingParticipant, (m.id, u.id))
        if p is None:
            s.add(MeetingParticipant(meeting_id=m.id, user_id=u.id, role="ATTENDEE", invited_at=now))
            added += 1
        elif p.removed_at is not None:
            p.removed_at, p.invited_at, p.rsvp, p.responded_at = None, now, "PENDING", None
            added += 1
    await audit(s, "meeting.invite", ctx.org.id, ctx.user.id, "Meeting", m.id, added=added)
    await s.commit()
    return {"added": added}


class RsvpIn(BaseModel):
    response: str = Field(pattern="^(accept|decline)$")


async def respond(s: AsyncSession, m: Meeting, me: MeetingParticipant | None, user: User, response: str) -> str:
    """Accept or decline my invitation; the host is told. Commits. (Shared with the assistant's tool.)"""
    from .reminders.service import notify_users
    if me is None:
        raise HTTPException(404, "You don't have an invitation to this meeting.")
    if me.role == "HOST":
        raise HTTPException(409, "You're the host of this meeting.")
    if m.status in ("ENDED", "CANCELLED"):
        raise HTTPException(409, "This meeting was cancelled." if m.status == "CANCELLED" else "This meeting is over.")
    accepted = response == "accept"
    me.rsvp, me.responded_at = ("ACCEPTED" if accepted else "DECLINED"), datetime.now(timezone.utc)
    await notify_users(s, [m.host_id], f"meeting.invitation_{'accepted' if accepted else 'declined'}",
                       f"{user.display_name} {'accepted' if accepted else 'declined'} “{m.title}”", link=f"/meetings/{m.join_code}")
    await audit(s, f"meeting.invitation_{response}", m.organization_id, user.id, "Meeting", m.id)
    await s.commit()
    return me.rsvp


@router.post("/api/meetings/{code}/rsvp")
async def rsvp(code: str, body: RsvpIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    m, me = await load(s, ctx, code)
    return {"rsvp": await respond(s, m, me, ctx.user, body.response)}


class JoinIn(BaseModel):
    speak: str
    hear: str
    voice: str = voices.DEFAULT_VOICE         # a Magpie voice, optionally with a tone ("Sofia.Calm")
    noise: str = "standard"


async def _voice(choice: str | None) -> str:
    """The voice to store: one Magpie offers (or any well-formed one while its list can't be read)."""
    v = voices.normalise(choice)
    if v is None:
        raise HTTPException(400, "That voice isn't available.")
    offered = {x["id"] for x in voices.all_voices(await voices.catalog())}
    if offered and v not in offered:
        raise HTTPException(400, "That voice isn't available.")
    return v


@router.get("/api/voices")
async def list_voices(lang: str = "en", ctx: Ctx = Depends(current)):
    """The voices and tones a speaker can be heard in (each listener hears the nearest their language has)."""
    by_lang = await voices.catalog()
    return {"voices": voices.all_voices(by_lang, lang), "default": voices.DEFAULT_VOICE,
            "languages": {l: [v["id"] for v in vs] for l, vs in by_lang.items()}}


PREVIEW_TEXT = {"en": "Hello! This is how the others will hear your translated speech.",
                "hi": "नमस्ते! दूसरे लोग आपकी अनुवादित बात इसी आवाज़ में सुनेंगे।"}


class PreviewIn(BaseModel):
    voice: str
    lang: str = "en"


@router.post("/api/voices/preview")
async def preview_voice(body: PreviewIn, ctx: Ctx = Depends(current)):
    """A short sample of a voice (WAV), in the given call language."""
    rate_limit(f"voice-preview:{ctx.user.id}", 30, 600)
    if body.lang not in voices.MAGPIE_LOCALE or not voices.MAGPIE_TTS_URL:
        raise HTTPException(404, "Voice samples aren't available.")
    voice = await _voice(body.voice)
    by_lang = await voices.catalog()
    form = aiohttp.FormData()
    form.add_field("language", voices.MAGPIE_LOCALE[body.lang])
    form.add_field("text", PREVIEW_TEXT[body.lang])
    form.add_field("voice", voices.magpie_name(by_lang, body.lang, voice))
    form.add_field("sample_rate_hz", "22050")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            async with http.post(f"{voices.MAGPIE_TTS_URL}/audio/synthesize", data=form) as r:
                if r.status != 200:
                    raise HTTPException(502, "The voice service couldn't make a sample right now.")
                audio = await r.read()
    except aiohttp.ClientError:
        raise HTTPException(502, "The voice service couldn't make a sample right now.") from None
    return Response(audio, media_type="audio/wav", headers={"cache-control": "no-store",
                                                             "x-voice": voices.voice_in(by_lang, body.lang, voice)})


@router.post("/api/meetings/{code}/join")
async def join(code: str, body: JoinIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    """A LiveKit token for this meeting's room (identity = my user id), with my call preferences."""
    if body.speak not in SPEAK or body.hear not in HEAR:
        raise HTTPException(400, "That language isn't available in calls.")
    m, me = await load(s, ctx, code)
    if m.status in ("ENDED", "CANCELLED"):
        raise HTTPException(409, "This meeting has ended.")
    now = datetime.now(timezone.utc)
    if me is None:                                   # an organisation member joining by link
        me = MeetingParticipant(meeting_id=m.id, user_id=ctx.user.id, role="ATTENDEE")
        s.add(me)
    me.first_joined_at = me.first_joined_at or now
    me.last_joined_at = now
    noise = body.noise if body.noise in NOISE_MODES and (body.noise != "studio" or bnr.available()) else "standard"
    me.speak, me.hear, me.voice, me.noise = body.speak, body.hear, await _voice(body.voice), noise
    if m.status == "SCHEDULED":
        m.status, m.started_at = "LIVE", now
    await audit(s, "meeting.join", ctx.org.id, ctx.user.id, "Meeting", m.id)
    await s.commit()
    jwt = (api.AccessToken(API_KEY, API_SECRET)
           .with_identity(str(ctx.user.id))
           .with_name(ctx.user.display_name)
           .with_attributes({"speak": me.speak, "hear": me.hear, "voice": me.voice, "noise": me.noise,
                             "role": me.role})
           .with_ttl(timedelta(hours=6))
           .with_grants(api.VideoGrants(room_join=True, room=m.room_name, can_publish=True, can_subscribe=True,
                                        can_publish_data=True, can_update_own_metadata=True))
           .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]))
           .to_jwt())
    return {"token": jwt, "identity": str(ctx.user.id), "room": m.room_name, "meeting": meeting_view(m, me, None)}


class PreferencesIn(BaseModel):
    speak: str | None = None
    hear: str | None = None
    voice: str | None = None
    noise: str | None = None


@router.patch("/api/meetings/{code}/preferences")
async def save_preferences(code: str, body: PreferencesIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    """Changes made during a call (languages, voice, noise), remembered for the next time I join."""
    m, me = await load(s, ctx, code)
    if me is None:
        raise HTTPException(403, "Join the meeting first.")
    if body.speak is not None and body.speak not in SPEAK or body.hear is not None and body.hear not in HEAR:
        raise HTTPException(400, "That language isn't available in calls.")
    if body.noise is not None and body.noise not in NOISE_MODES:
        raise HTTPException(400, "Unknown noise mode.")
    for k in ("speak", "hear", "noise"):
        if getattr(body, k) is not None:
            setattr(me, k, getattr(body, k))
    if body.voice is not None:
        me.voice = await _voice(body.voice)
    await s.commit()
    return {"myPreferences": {k: getattr(me, k) for k in ("speak", "hear", "voice", "noise")}}


async def _close_room(room: str):
    """Disconnect everyone from the room (best effort: the room may already be gone)."""
    try:
        lk = api.LiveKitAPI(LIVEKIT_HTTP, API_KEY, API_SECRET)
        try:
            await lk.room.delete_room(api.DeleteRoomRequest(room=room))
        finally:
            await lk.aclose()
    except Exception:  # noqa: BLE001
        pass


async def remove_from_room(room: str, identity: str):
    """Disconnect one person from a live room (best effort)."""
    try:
        lk = api.LiveKitAPI(LIVEKIT_HTTP, API_KEY, API_SECRET)
        try:
            await lk.room.remove_participant(api.RoomParticipantIdentity(room=room, identity=identity))
        finally:
            await lk.aclose()
    except Exception:  # noqa: BLE001
        pass


@router.post("/api/meetings/{code}/end")
async def end(code: str, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    m, me = await load(s, ctx, code)
    if not me or me.role not in MODERATORS:
        raise HTTPException(403, "Only the host or a co-host can end the meeting for everyone.")
    if m.status in ("ENDED", "CANCELLED"):
        return {"meeting": meeting_view(m, me)}
    now = datetime.now(timezone.utc)
    m.status = "ENDED" if m.started_at else "CANCELLED"
    m.ended_at = now
    await audit(s, "meeting.end", ctx.org.id, ctx.user.id, "Meeting", m.id, status=m.status)
    await s.commit()
    await _close_room(m.room_name)
    return {"meeting": meeting_view(m, me)}


async def end_abandoned(s: AsyncSession, live_rooms: set[str], grace_s: int = 120) -> int:
    """LIVE meetings whose room has been gone a while (everyone left) are ENDED. Run by the watchdog."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=grace_s)
    rows = (await s.execute(select(Meeting).where(Meeting.status == "LIVE", Meeting.started_at < cutoff))).scalars().all()
    ended = 0
    for m in rows:
        if m.room_name not in live_rooms:
            m.status, m.ended_at = "ENDED", datetime.now(timezone.utc)
            ended += 1
    if ended:
        await s.commit()
    return ended
