"""Reminders, notifications, your mobile number and your text messages: the HTTP routes."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..accounts import Ctx, current, get_db
from ..assistant.tools.base import ToolError
from . import service, sms

router = APIRouter()
_STATUS = {"VALIDATION": 400, "NOT_FOUND": 404, "RATE_LIMITED": 429, "NOT_CONFIGURED": 503, "PROVIDER_UNAVAILABLE": 502}


async def _call(coro):
    """Run a service call; its ToolErrors become HTTP errors with the same message."""
    try:
        return await coro
    except ToolError as e:
        raise HTTPException(_STATUS.get(e.code, 400), e.message) from None


class ReminderIn(BaseModel):
    text: str = Field(max_length=300)
    dueAt: AwareDatetime
    sms: bool | None = None                 # default: your "text me reminders" setting


@router.get("/api/reminders")
async def list_reminders(include: Literal["upcoming", "all"] = "upcoming", ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    return {"reminders": await service.list_reminders(s, ctx.user.id, include)}


@router.post("/api/reminders")
async def create_reminder(body: ReminderIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    r = await _call(service.create_reminder(s, ctx.user.id, ctx.org.id, body.text, body.dueAt, body.sms))
    await s.commit()
    return {"reminder": r}


@router.delete("/api/reminders/{reminder_id}")
async def cancel_reminder(reminder_id: str, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    r = await _call(service.cancel_reminder(s, ctx.user.id, reminder_id))
    await s.commit()
    return r


@router.get("/api/notifications")
async def notifications(limit: int = 20, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    return await service.list_notifications(s, ctx.user.id, max(1, min(limit, 100)))


class ReadIn(BaseModel):
    ids: list[str] | None = Field(default=None, max_length=200)    # none: all of them


@router.post("/api/notifications/read")
async def mark_read(body: ReadIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    await service.mark_notifications_read(s, ctx.user.id, body.ids)
    await s.commit()
    return {"ok": True}


class PhoneIn(BaseModel):
    phoneNumber: str | None = Field(default=None, max_length=30)     # empty or null removes it


@router.post("/api/profile/phone")
async def set_phone(body: PhoneIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    return await _call(sms.set_phone(s, ctx.user.id, ctx.org.id, body.phoneNumber))


@router.post("/api/profile/phone/verify/start")
async def verify_start(ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    return await _call(sms.start_verification(s, ctx.user.id, ctx.org.id))


class CodeIn(BaseModel):
    code: str = Field(max_length=20)


@router.post("/api/profile/phone/verify")
async def verify(body: CodeIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    return await _call(sms.confirm_verification(s, ctx.user.id, ctx.org.id, body.code))


class EnabledIn(BaseModel):
    enabled: bool


@router.put("/api/profile/sms-reminders")
async def sms_reminders(body: EnabledIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    return await _call(sms.set_sms_reminders(s, ctx.user.id, body.enabled))


@router.get("/api/profile/sms")
async def my_sms(ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    """Texts sent to you (in development, the outbox, codes included), and your number's state."""
    return {**await sms.list_my_sms(s, ctx.user.id), "phone": await sms.phone_status(s, ctx.user.id)}
