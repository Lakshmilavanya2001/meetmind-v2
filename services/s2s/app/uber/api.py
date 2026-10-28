"""Uber routes: the connection (status, connect, callback, disconnect) and your Uber rides (list, live status,
next test stage, cancel)."""
from __future__ import annotations

import hmac
import logging
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..accounts import Ctx, current, get_db
from ..assistant.tools.base import ToolError
from . import service

router = APIRouter()
log = logging.getLogger("s2s.uber")
STATE_PATH = "/api/integrations/uber"
_STATUS = {"VALIDATION": 400, "NOT_FOUND": 404, "FORBIDDEN": 403, "CONFLICT": 409, "RATE_LIMITED": 429,
           "NOT_CONFIGURED": 503, "PROVIDER_UNAVAILABLE": 502}


def _http_error(e: ToolError) -> HTTPException:
    return HTTPException(_STATUS.get(e.code, 500), e.message)


def _is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


@router.get("/api/integrations/uber")
async def status(ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    return await service.get_status(s, ctx)


@router.get("/api/integrations/uber/connect")
async def connect(request: Request, ctx: Ctx = Depends(current)):
    """A top-level navigation from Settings: off to Uber's sign-in, remembering a one-time state bound to this user."""
    try:
        url, state = service.start_connect(ctx)
    except ToolError as e:
        raise _http_error(e) from None
    res = RedirectResponse(url, status_code=302)
    res.set_cookie(service.UBER_STATE_COOKIE, f"{ctx.user.id}.{state}", max_age=600, path=STATE_PATH,
                   httponly=True, secure=_is_https(request), samesite="lax")
    return res


@router.get("/api/integrations/uber/callback")
async def callback(request: Request, s: AsyncSession = Depends(get_db)):
    """Uber sends the user back here after sign-in. The state must match the cookie set when they started, for
    the same signed-in user, so another site can't attach its own Uber account to this user."""
    base = os.environ.get("PUBLIC_URL", "").strip().rstrip("/") or str(request.base_url).rstrip("/")

    def back(result: str) -> RedirectResponse:
        res = RedirectResponse(f"{base}/settings?uber={result}", status_code=302)
        res.delete_cookie(service.UBER_STATE_COOKIE, path=STATE_PATH)
        return res

    try:
        ctx = await current(request, s)
    except HTTPException:
        return back("signin")
    q = request.query_params
    if q.get("error"):
        return back("denied")
    expected = request.cookies.get(service.UBER_STATE_COOKIE, "")
    code = q.get("code")
    given = f"{ctx.user.id}.{q.get('state', '')}"
    if not code or not expected or not hmac.compare_digest(expected.encode(), given.encode()):
        return back("expired")
    try:
        await service.finish_connect(s, ctx, code)
        return back("connected")
    except ToolError:
        return back("error")
    except Exception:  # noqa: BLE001 - logged; the user sees "error"
        log.exception("uber connect failed")
        return back("error")


@router.post("/api/integrations/uber/disconnect")
async def disconnect(ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    return await service.disconnect(s, ctx)


@router.get("/api/rides")
async def rides(ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    return {"rides": await service.list_rides(s, ctx.user.id)}


@router.get("/api/rides/{ride_id}")
async def ride_status(ride_id: uuid.UUID, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    """The live status of your own Uber ride (refreshed from Uber)."""
    try:
        return await service.get_ride_status(s, ctx, ride_id)
    except ToolError as e:
        raise _http_error(e) from None


@router.post("/api/rides/{ride_id}/advance")
async def advance(ride_id: uuid.UUID, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    """Uber test mode only: move your ride to its next stage (for demos)."""
    try:
        return await service.advance_test_ride(s, ctx, ride_id)
    except ToolError as e:
        raise _http_error(e) from None


@router.post("/api/rides/{ride_id}/cancel")
async def cancel(ride_id: uuid.UUID, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    try:
        return await service.cancel_ride(s, ctx, ride_id)
    except ToolError as e:
        raise _http_error(e) from None
