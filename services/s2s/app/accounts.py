"""Accounts and organisations: sign up (a new organisation, or an existing one with its join code),
log in and out, who am I, the organisation's members. Sessions are a random cookie whose sha256 is
stored; they last 30 days and are checked on every request."""
from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import db
from .models import AuditLog, Membership, Organization, Session, User
from .security import (SESSION_COOKIE, SESSION_DAYS, assert_same_origin, client_ip, hash_password, new_token,
                       rate_limit, token_hash, verify_password)

router = APIRouter()
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class Ctx:
    """The signed-in person, in the organisation of their session."""
    user: User
    org: Organization
    role: str
    session_id: uuid.UUID


async def get_db():
    async with db.sessionmaker()() as s:
        yield s


async def current(request: Request, s: AsyncSession = Depends(get_db)) -> Ctx:
    assert_same_origin(request)
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Please sign in.")
    now = datetime.now(timezone.utc)
    row = (await s.execute(
        select(Session, User, Organization, Membership.role)
        .join(User, User.id == Session.user_id)
        .join(Organization, Organization.id == Session.organization_id)
        .join(Membership, (Membership.user_id == User.id) & (Membership.organization_id == Organization.id))
        .where(Session.token_hash == token_hash(token), Session.expires_at > now, User.is_active.is_(True))
    )).first()
    if row is None:
        raise HTTPException(401, "Your session has ended. Please sign in again.")
    sess, user, org, role = row
    if (now - sess.last_seen_at).total_seconds() > 3600:      # slide the expiry, at most hourly
        sess.last_seen_at, sess.expires_at = now, now + timedelta(days=SESSION_DAYS)
        await s.commit()
    return Ctx(user, org, role, sess.id)


async def audit(s: AsyncSession, action: str, org_id=None, actor_id=None, target_type=None, target_id=None, **meta):
    s.add(AuditLog(organization_id=org_id, actor_id=actor_id, action=action, target_type=target_type,
                   target_id=str(target_id) if target_id else None, metadata_=meta))


def user_view(u: User) -> dict:
    return {"id": str(u.id), "email": u.email, "displayName": u.display_name, "timezone": u.timezone,
            "preferredLanguage": u.preferred_language}


def _slug(name: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "org") + "-" + secrets.token_hex(3)


async def _start_session(s: AsyncSession, response: Response, request: Request, user: User, org_id: uuid.UUID):
    token, digest = new_token()
    now = datetime.now(timezone.utc)
    s.add(Session(token_hash=digest, user_id=user.id, organization_id=org_id, expires_at=now + timedelta(days=SESSION_DAYS)))
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, secure=secure,
                        samesite="lax", path="/")


class SignupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(max_length=200)
    password: str = Field(min_length=10, max_length=200)
    organization: str | None = Field(default=None, max_length=80)     # a new organisation's name, or
    join_code: str | None = Field(default=None, max_length=60)        # an existing one's join code

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        if not EMAIL.match(v):
            raise ValueError("Enter a valid email address")
        return v


@router.post("/api/auth/signup")
async def signup(body: SignupIn, request: Request, response: Response, s: AsyncSession = Depends(get_db)):
    assert_same_origin(request)
    rate_limit(f"signup:{client_ip(request)}", 10, 3600)
    if bool(body.organization) == bool(body.join_code):
        raise HTTPException(400, "Give either a new organisation's name or a join code.")
    if (await s.execute(select(User.id).where(func.lower(User.email) == body.email.lower()))).first():
        raise HTTPException(409, "An account with that email already exists. Sign in instead.")
    if body.join_code:
        org = (await s.execute(select(Organization).where(Organization.join_code == body.join_code.strip()))).scalar_one_or_none()
        if org is None:
            raise HTTPException(400, "That join code doesn't match any organisation.")
        role = "MEMBER"
    else:
        org = Organization(name=body.organization.strip(), slug=_slug(body.organization), join_code=f"{_slug(body.organization)[:12]}-{secrets.token_hex(4)}")
        s.add(org)
        await s.flush()
        role = "OWNER"
    user = User(email=body.email, display_name=body.name.strip(), password_hash=hash_password(body.password))
    s.add(user)
    await s.flush()
    s.add(Membership(organization_id=org.id, user_id=user.id, role=role))
    await audit(s, "auth.signup", org.id, user.id, "User", user.id, role=role)
    await _start_session(s, response, request, user, org.id)
    await s.commit()
    return {"user": user_view(user), "organization": {"id": str(org.id), "name": org.name}, "role": role}


class LoginIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


@router.post("/api/auth/login")
async def login(body: LoginIn, request: Request, response: Response, s: AsyncSession = Depends(get_db)):
    assert_same_origin(request)
    rate_limit(f"login:{client_ip(request)}", 30, 900)
    rate_limit(f"login:{body.email.lower()}", 10, 900)
    user = (await s.execute(select(User).where(func.lower(User.email) == body.email.strip().lower()))).scalar_one_or_none()
    if not verify_password(user.password_hash if user else None, body.password) or not user or not user.is_active:
        raise HTTPException(401, "Wrong email or password.")
    m = (await s.execute(select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at))).scalars().first()
    if m is None:
        raise HTTPException(403, "This account isn't in any organisation.")
    await audit(s, "auth.login", m.organization_id, user.id)
    await _start_session(s, response, request, user, m.organization_id)
    await s.commit()
    return {"user": user_view(user)}


@router.post("/api/auth/logout")
async def logout(response: Response, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    sess = await s.get(Session, ctx.session_id)
    if sess:
        await s.delete(sess)
        await s.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/me")
async def me(ctx: Ctx = Depends(current)):
    org = {"id": str(ctx.org.id), "name": ctx.org.name}
    if ctx.role in ("OWNER", "ADMIN"):
        org["joinCode"] = ctx.org.join_code                   # only admins can let people in
    return {"user": user_view(ctx.user), "organization": org, "role": ctx.role}


class ProfileIn(BaseModel):
    displayName: str = Field(min_length=1, max_length=80)
    timezone: str = Field(min_length=1, max_length=60)
    preferredLanguage: str = Field(min_length=2, max_length=10)


@router.patch("/api/me")
async def update_me(body: ProfileIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, "Unknown time zone.") from None
    user = await s.get(User, ctx.user.id)
    user.display_name, user.timezone, user.preferred_language = body.displayName.strip(), body.timezone, body.preferredLanguage
    await s.commit()
    return {"user": user_view(user)}


@router.get("/api/org/members")
async def members(q: str = "", ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    stmt = (select(User, Membership.role).join(Membership, Membership.user_id == User.id)
            .where(Membership.organization_id == ctx.org.id, User.is_active.is_(True)).order_by(User.display_name).limit(200))
    if q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(User.display_name).like(like) | func.lower(User.email).like(like))
    return {"members": [{**user_view(u), "role": r} for u, r in (await s.execute(stmt)).all()]}
