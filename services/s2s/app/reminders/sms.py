"""Text messages (ported from meetmind's services/sms.ts and providers/sms.ts).

Provider: Twilio over its REST API when TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM are set;
else the dev outbox (nothing leaves the server, messages show on the reminders page) when
SMS_DEV_OUTBOX=true; else SMS is unavailable.

Every text is recorded in sms_messages. Destinations must start with an allowed prefix
(SMS_ALLOWED_PREFIXES, default "+91"), and there are daily caps per user (SMS_DAILY_LIMIT_PER_USER,
default 20) and for the whole server (SMS_DAILY_LIMIT, default 2000).

Phone verification: a 6-digit code is texted to the saved number; only its sha256 is stored; it
expires after 10 minutes and 5 wrong attempts spend it. A number gets at most 5 codes a day.

The functions here commit their own changes (a recorded text must stay recorded).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import aiohttp
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..accounts import audit
from ..assistant.tools.base import ToolError
from ..security import rate_limit
from .models import PhoneVerification, SmsMessage, user_phone

log = logging.getLogger("s2s.sms")

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
PER_NUMBER_CODES = 5                       # codes a number may receive a day, whichever accounts ask
DAY = timedelta(days=1)
NOT_CONFIGURED = "Text messages aren't set up on this server."
E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class SmsError(Exception):
    pass


@dataclass
class SmsProvider:
    name: str                                       # "twilio" sends real texts; "outbox" only records them
    send: Callable[[str, str], Awaitable[tuple[str, str | None]]]   # (to, body) -> (status, provider id)


def _twilio(sid: str, token: str, sender: str) -> SmsProvider:
    """Twilio Programmable Messaging. TWILIO_FROM is a number or a Messaging Service SID (MG…)."""
    async def send(to: str, body: str) -> tuple[str, str | None]:
        form = {"To": to, "Body": body}
        form["MessagingServiceSid" if sender.startswith("MG") else "From"] = sender
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as h:
            async with h.post(url, data=form, auth=aiohttp.BasicAuth(sid, token)) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = {}
                if r.status >= 400:
                    code = f" ({data['code']})" if data.get("code") else ""
                    raise SmsError(f"Twilio {r.status}{code}: {data.get('message') or 'send failed'}")
                return "SENT", data.get("sid")
    return SmsProvider("twilio", send)


async def _outbox_send(to: str, body: str) -> tuple[str, str | None]:
    return "OUTBOX", None


OUTBOX = SmsProvider("outbox", _outbox_send)
_override: SmsProvider | None = None


def get_provider() -> SmsProvider | None:
    """Twilio when configured; the dev outbox only when explicitly enabled (showing a code to whoever
    asked for it means they could "verify" any number); otherwise None."""
    if _override:
        return _override
    sid, token, sender = (os.environ.get(k, "") for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"))
    if sid and token and sender:
        return _twilio(sid, token, sender)
    return OUTBOX if os.environ.get("SMS_DEV_OUTBOX") == "true" else None


def set_provider_for_tests(p: SmsProvider | None):
    global _override
    _override = p


def is_available() -> bool:
    return get_provider() is not None


def destination_allowed(to: str) -> bool:
    """Destinations we will text, as E.164 prefixes (SMS_ALLOWED_PREFIXES, default India only). "*" allows all."""
    allowed = [p.strip() for p in os.environ.get("SMS_ALLOWED_PREFIXES", "+91").split(",") if p.strip()]
    return "*" in allowed or any(to.startswith(p) for p in allowed)


def normalize_phone(raw: str) -> str:
    """'+91 98123-45678' -> '+919812345678'; raises ToolError unless it is E.164."""
    v = re.sub(r"[\s()\-]", "", raw.strip())
    if not E164.match(v):
        raise ToolError("VALIDATION", "Use international format, e.g. +919812345678")
    return v


def can_text(phone_number: str | None, verified_at, verified_via: str | None) -> bool:
    """True when SMS can actually reach this person: verified through the provider in use now."""
    p = get_provider()
    return bool(p and phone_number and verified_at and verified_via == p.name)


async def user_phone_row(s: AsyncSession, user_id: uuid.UUID):
    return (await s.execute(select(user_phone).where(user_phone.c.id == user_id))).one()


def _limits() -> tuple[int, int]:
    return int(os.environ.get("SMS_DAILY_LIMIT_PER_USER") or 20), int(os.environ.get("SMS_DAILY_LIMIT") or 2000)


@dataclass
class SendResult:
    ok: bool
    status: str                     # OUTBOX | SENT | FAILED
    error: str | None = None


async def send_sms(s: AsyncSession, user_id: uuid.UUID | None, to: str, body: str, purpose: str,
                   stored_body: str | None = None) -> SendResult:
    """Send a text and record it (committed). Never raises for delivery problems: returns the status.
    Enforces the destination allowlist and the daily caps (cost and abuse control)."""
    provider = get_provider()

    async def record(status: str, provider_id: str | None = None, error: str | None = None):
        # Verification codes are only ever stored in the dev outbox; a real SMS stores a masked copy.
        kept = body if provider and provider.name == "outbox" else (stored_body or body)
        s.add(SmsMessage(user_id=user_id, to_number=to, body=kept, purpose=purpose, provider=provider.name if provider else "none",
                         status=status, provider_id=provider_id, error=error))
        await s.commit()

    async def fail(error: str) -> SendResult:
        await record("FAILED", error=error)
        return SendResult(False, "FAILED", error)

    if provider is None:
        return await fail(NOT_CONFIGURED)
    if not destination_allowed(to):
        return await fail("This country isn't enabled for text messages.")
    since = datetime.now(timezone.utc) - DAY
    counted = (SmsMessage.status.in_(("SENT", "OUTBOX")), SmsMessage.created_at >= since)
    per_user, overall = _limits()
    mine = (await s.scalar(select(func.count()).select_from(SmsMessage).where(SmsMessage.user_id == user_id, *counted))) if user_id else 0
    total = await s.scalar(select(func.count()).select_from(SmsMessage).where(*counted))
    if mine >= per_user:
        return await fail("Daily text-message limit reached for this account.")
    if total >= overall:
        return await fail("Daily text-message limit reached for this server.")
    try:
        status, provider_id = await provider.send(to, body)
    except Exception as e:  # noqa: BLE001 - any delivery problem is recorded, not raised
        error = (str(e) or type(e).__name__)[:300]
        log.error("sms send failed: %s", error)
        return await fail(error)
    await record(status, provider_id)
    return SendResult(True, status)


def _hash_code(user_id: uuid.UUID, phone: str, code: str) -> str:
    return hashlib.sha256(f"{user_id}:{phone}:{code}".encode()).hexdigest()


async def set_phone(s: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID, raw: str | None) -> dict:
    """Save (or clear, with empty/None) your mobile number. A different number is no longer verified
    and SMS reminders are turned off."""
    phone = normalize_phone(raw) if raw and raw.strip() else None
    u = await user_phone_row(s, user_id)
    if phone != u.phone_number:
        await s.execute(update(user_phone).where(user_phone.c.id == user_id).values(
            phone_number=phone, phone_verified_at=None, phone_verified_via=None, sms_reminders=False))
        await audit(s, "profile.phone", org_id, user_id, "User", user_id)
        await s.commit()
    return await phone_status(s, user_id)


async def phone_status(s: AsyncSession, user_id: uuid.UUID) -> dict:
    u = await user_phone_row(s, user_id)
    p = get_provider()
    return {"phoneNumber": u.phone_number, "verified": can_text(u.phone_number, u.phone_verified_at, u.phone_verified_via),
            "verifiedAt": u.phone_verified_at.isoformat() if u.phone_verified_at else None,
            "smsReminders": bool(u.sms_reminders), "provider": p.name if p else None}


async def start_verification(s: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID) -> dict:
    """Text a 6-digit code to the saved number."""
    provider = get_provider()
    if provider is None:
        raise ToolError("NOT_CONFIGURED", NOT_CONFIGURED)
    rate_limit(f"phone-verify-start:{user_id}", int(os.environ.get("SMS_VERIFY_STARTS_PER_10MIN") or 3), 600)
    u = await user_phone_row(s, user_id)
    if not u.phone_number:
        raise ToolError("VALIDATION", "Add your mobile number in your profile first.")
    if u.phone_verified_at and u.phone_verified_via == provider.name:
        return {"alreadyVerified": True, "provider": provider.name}
    if not destination_allowed(u.phone_number):
        raise ToolError("VALIDATION", "Text messages to this country aren't enabled on this server.")
    # A number can receive only a few codes a day, whichever accounts ask (stops SMS-bombing a stranger).
    recent = await s.scalar(select(func.count()).select_from(SmsMessage).where(
        SmsMessage.to_number == u.phone_number, SmsMessage.purpose == "verification",
        SmsMessage.created_at >= datetime.now(timezone.utc) - DAY))
    if recent >= PER_NUMBER_CODES:
        raise ToolError("RATE_LIMITED", "Too many codes were sent to this number today. Try again tomorrow.")

    code = f"{secrets.randbelow(1_000_000):06d}"
    values = {"phone": u.phone_number, "code_hash": _hash_code(user_id, u.phone_number, code), "attempts": 0,
              "expires_at": datetime.now(timezone.utc) + CODE_TTL}
    await s.execute(insert(PhoneVerification).values(user_id=user_id, **values)
                    .on_conflict_do_update(index_elements=[PhoneVerification.user_id], set_=values))
    text = "Your MeetMind verification code is {}. It expires in 10 minutes."
    sent = await send_sms(s, user_id, u.phone_number, text.format(code), "verification", stored_body=text.format("••••••"))
    if not sent.ok:
        raise ToolError("PROVIDER_UNAVAILABLE", f"We couldn't send the text message: {sent.error}")
    await audit(s, "phone.verify_start", org_id, user_id)
    await s.commit()
    return {"alreadyVerified": False, "provider": provider.name}


async def confirm_verification(s: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID, code: str) -> dict:
    rate_limit(f"phone-verify:{user_id}", 10, 600)
    code = (code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ToolError("VALIDATION", "Enter the 6-digit code")
    provider = get_provider()
    if provider is None:
        raise ToolError("NOT_CONFIGURED", NOT_CONFIGURED)
    u = await user_phone_row(s, user_id)
    now = datetime.now(timezone.utc)
    # Claim one attempt atomically before comparing, so parallel guesses can't exceed the limit.
    stored = (await s.execute(
        update(PhoneVerification)
        .where(PhoneVerification.user_id == user_id, PhoneVerification.phone == (u.phone_number or ""),
               PhoneVerification.expires_at > now, PhoneVerification.attempts < MAX_ATTEMPTS)
        .values(attempts=PhoneVerification.attempts + 1)
        .returning(PhoneVerification.code_hash))).scalar_one_or_none()
    await s.commit()
    if stored is None:
        v = await s.get(PhoneVerification, user_id)
        if v and v.attempts >= MAX_ATTEMPTS and v.expires_at > now:
            raise ToolError("RATE_LIMITED", "Too many wrong codes. Send a new one.")
        raise ToolError("VALIDATION", "That code has expired. Send a new one.")
    if not hmac.compare_digest(stored, _hash_code(user_id, u.phone_number, code)):
        raise ToolError("VALIDATION", "That code isn't right.")
    await s.execute(update(user_phone).where(user_phone.c.id == user_id).values(phone_verified_at=now, phone_verified_via=provider.name))
    v = await s.get(PhoneVerification, user_id)
    if v:
        await s.delete(v)
    await audit(s, "phone.verified", org_id, user_id, via=provider.name)
    await s.commit()
    return {"verified": True}


async def set_sms_reminders(s: AsyncSession, user_id: uuid.UUID, enabled: bool) -> dict:
    u = await user_phone_row(s, user_id)
    if enabled and not can_text(u.phone_number, u.phone_verified_at, u.phone_verified_via):
        raise ToolError("VALIDATION", "Verify your mobile number first.")
    await s.execute(update(user_phone).where(user_phone.c.id == user_id).values(sms_reminders=enabled))
    await s.commit()
    return {"enabled": enabled}


async def list_my_sms(s: AsyncSession, user_id: uuid.UUID) -> dict:
    """Your own text messages (verification codes are masked unless this is the dev outbox)."""
    rows = (await s.execute(select(SmsMessage).where(SmsMessage.user_id == user_id)
                            .order_by(SmsMessage.created_at.desc()).limit(50))).scalars().all()
    p = get_provider()
    return {"provider": p.name if p else None, "messages": [
        {"id": str(m.id), "to": m.to_number, "body": m.body, "purpose": m.purpose, "provider": m.provider,
         "status": m.status, "error": m.error, "createdAt": m.created_at.isoformat()} for m in rows]}
