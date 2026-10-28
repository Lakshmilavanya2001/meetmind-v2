"""Reminders and in-app notifications (ported from meetmind's services/reminders.ts and
services/notifications.ts).

A reminder is for yourself only: in-app always, and by SMS only to your own verified number.
create/cancel don't commit (the caller does); the worker, send_due_reminders, commits as it goes.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..accounts import audit
from ..assistant.tools.base import ToolError
from . import sms as smsmod
from .models import Notification, Reminder

log = logging.getLogger("s2s.reminders")

MAX_PENDING = 100
LINK = "/reminders"


def fmt_when(d: datetime, tz: str, year: bool = True) -> str:
    """'Wed 29 Oct 2026, 15:00' in the person's time zone."""
    d = d.astimezone(ZoneInfo(tz))
    return f"{d:%a} {d.day} {d:%b}" + (f" {d:%Y}" if year else "") + f", {d:%H:%M}"


def _uuid(v: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v))
    except ValueError:
        return None


async def check_reminder(s: AsyncSession, user_id: uuid.UUID, text: str, due_at: datetime, sms: bool | None,
                         now: datetime | None = None) -> dict:
    """Validate a reminder without creating it (also describes it before the user confirms)."""
    text = (text or "").strip()
    if not text:
        raise ToolError("VALIDATION", "What should we remind you about?")
    if len(text) > 300:
        raise ToolError("VALIDATION", "Keep the reminder under 300 characters.")
    if due_at.tzinfo is None:
        raise ToolError("VALIDATION", "Give the time with its time-zone offset.")
    now = now or datetime.now(timezone.utc)
    if due_at < now - timedelta(seconds=60):
        raise ToolError("VALIDATION", "That time has already passed.")
    if due_at > now + timedelta(days=366):
        raise ToolError("VALIDATION", "Reminders can be set up to a year ahead.")
    u = await smsmod.user_phone_row(s, user_id)
    use_sms = u.sms_reminders if sms is None else sms
    if use_sms and not smsmod.is_available():
        raise ToolError("VALIDATION", "Text messages aren't set up on this server. The reminder was not created; you can create it without SMS.")
    if use_sms and not smsmod.can_text(u.phone_number, u.phone_verified_at, u.phone_verified_via):
        raise ToolError("VALIDATION", "SMS reminders need a verified mobile number (Profile → Mobile number). The reminder was not created; you can create it without SMS.")
    pending = await s.scalar(select(func.count()).select_from(Reminder).where(Reminder.user_id == user_id, Reminder.status == "PENDING"))
    if pending >= MAX_PENDING:
        raise ToolError("VALIDATION", f"You already have {MAX_PENDING} upcoming reminders.")
    return {"text": text, "due_at": due_at, "sms": bool(use_sms), "phone": u.phone_number, "timezone": u.timezone,
            "when": fmt_when(due_at, u.timezone)}


def view(r: Reminder, tz: str) -> dict:
    return {"id": str(r.id), "text": r.text, "dueAt": r.due_at.isoformat(), "when": fmt_when(r.due_at, tz), "sms": r.sms,
            "status": r.status, "source": r.source, "meetingId": str(r.meeting_id) if r.meeting_id else None,
            "sentAt": r.sent_at.isoformat() if r.sent_at else None, "smsStatus": r.sms_status}


async def create_reminder(s: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID, text: str, due_at: datetime,
                          sms: bool | None = None, source: str = "user", now: datetime | None = None) -> dict:
    """A personal reminder for yourself (flushed, not committed)."""
    c = await check_reminder(s, user_id, text, due_at, sms, now)
    r = Reminder(organization_id=org_id, user_id=user_id, text=c["text"], due_at=c["due_at"], sms=c["sms"], source=source)
    s.add(r)
    await s.flush()
    await audit(s, "reminder.create", org_id, user_id, "Reminder", r.id, sms=c["sms"], source=source)
    await s.refresh(r)
    return view(r, c["timezone"])


async def list_reminders(s: AsyncSession, user_id: uuid.UUID, include: str = "upcoming") -> list[dict]:
    tz = (await smsmod.user_phone_row(s, user_id)).timezone
    stmt = select(Reminder).where(Reminder.user_id == user_id).limit(100)
    if include == "all":
        stmt = stmt.order_by(Reminder.due_at.desc())
    else:
        stmt = stmt.where(Reminder.status.in_(("PENDING", "SENDING"))).order_by(Reminder.due_at)
    return [view(r, tz) for r in (await s.execute(stmt)).scalars().all()]


async def get_pending(s: AsyncSession, user_id: uuid.UUID, reminder_id: str) -> Reminder:
    rid = _uuid(reminder_id)
    r = (await s.execute(select(Reminder).where(Reminder.id == rid, Reminder.user_id == user_id, Reminder.status == "PENDING"))).scalar_one_or_none() if rid else None
    if r is None:
        raise ToolError("NOT_FOUND", "No upcoming reminder with that id.")
    return r


async def cancel_reminder(s: AsyncSession, user_id: uuid.UUID, reminder_id: str) -> dict:
    """Only your own, only while still PENDING (not committed)."""
    rid = _uuid(reminder_id)
    done = (await s.execute(update(Reminder).where(Reminder.id == rid, Reminder.user_id == user_id, Reminder.status == "PENDING")
                            .values(status="CANCELED"))).rowcount if rid else 0
    if not done:
        raise ToolError("NOT_FOUND", "No upcoming reminder with that id.")
    return {"canceled": True}


# ─── Notifications ──────────────────────────────────────────────────────

async def notify_users(s: AsyncSession, user_ids: list[uuid.UUID], type: str, title: str, body: str | None = None,
                       link: str | None = None, data: dict | None = None):
    """In-app notifications (added to the session, not committed)."""
    for uid in dict.fromkeys(user_ids):
        s.add(Notification(user_id=uid, type=type, title=title, body=body, link=link, data=data))


async def list_notifications(s: AsyncSession, user_id: uuid.UUID, limit: int = 20) -> dict:
    rows = (await s.execute(select(Notification).where(Notification.user_id == user_id)
                            .order_by(Notification.created_at.desc()).limit(limit))).scalars().all()
    unread = await s.scalar(select(func.count()).select_from(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None)))
    return {"items": [{"id": str(n.id), "type": n.type, "title": n.title, "body": n.body, "link": n.link,
                       "readAt": n.read_at.isoformat() if n.read_at else None, "createdAt": n.created_at.isoformat()} for n in rows],
            "unread": unread}


async def mark_notifications_read(s: AsyncSession, user_id: uuid.UUID, ids: list[str] | None = None):
    """Mark your notifications read (all, or the given ids). Other people's rows are never touched."""
    stmt = update(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))
    if ids is not None:
        stmt = stmt.where(Notification.id.in_([u for u in map(_uuid, ids) if u]))
    await s.execute(stmt.values(read_at=func.now()))


# ─── The worker ─────────────────────────────────────────────────────────

async def send_due_reminders(s: AsyncSession, now: datetime | None = None) -> int:
    """Deliver due reminders; returns how many were sent. Each is claimed atomically
    (PENDING -> SENDING, committed) before delivery, so several workers never double-send."""
    now = now or datetime.now(timezone.utc)
    due = (await s.execute(select(Reminder.id).where(Reminder.status == "PENDING", Reminder.due_at <= now)
                           .order_by(Reminder.due_at).limit(50))).scalars().all()
    sent = 0
    for rid in due:
        claimed = (await s.execute(update(Reminder).where(Reminder.id == rid, Reminder.status == "PENDING")
                                   .values(status="SENDING", sent_at=func.now()))).rowcount   # sent_at = claim time until delivered
        await s.commit()
        if not claimed:
            continue
        r = await s.get(Reminder, rid, populate_existing=True)
        u = await smsmod.user_phone_row(s, r.user_id)
        try:
            if not u.is_active:
                r.status, r.sent_at = "CANCELED", None
                await s.commit()
                continue
            await notify_users(s, [r.user_id], "reminder", "Reminder", r.text, LINK, {"reminderId": str(r.id)})
            sms_status = None
            if r.sms:
                if not smsmod.can_text(u.phone_number, u.phone_verified_at, u.phone_verified_via):
                    sms_status = "skipped: number not verified for SMS"
                else:
                    # Action items come from meeting text other people control: text a fixed notice, not their words.
                    body = ("MeetMind: you have an action item due today. Open MeetMind → Tasks for details."
                            if r.source == "action_item" else f"MeetMind reminder: {r.text}"[:320])
                    res = await smsmod.send_sms(s, r.user_id, u.phone_number, body, "reminder")     # commits the notification too
                    sms_status = ("outbox" if res.status == "OUTBOX" else "sent") if res.ok else f"failed: {res.error}"
            r.status, r.sent_at, r.sms_status = "SENT", datetime.now(timezone.utc), sms_status
            await s.commit()
            sent += 1
        except Exception as e:  # noqa: BLE001 - one bad reminder mustn't stop the others
            log.error("reminder delivery failed: %s", e)
            await s.rollback()
            await s.execute(update(Reminder).where(Reminder.id == rid).values(status="FAILED", sms_status=f"failed: {str(e)[:200]}"))
            await s.commit()
    # Recover reminders claimed more than 5 minutes ago but never finished (a crash mid-delivery).
    await s.execute(update(Reminder).where(Reminder.status == "SENDING", Reminder.sent_at < datetime.now(timezone.utc) - timedelta(minutes=5))
                    .values(status="PENDING", sent_at=None))
    await s.commit()
    return sent
