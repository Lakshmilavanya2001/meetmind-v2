"""Bookings pages' API: your own orders and bookings (made through the assistant, with confirmation),
cancelling one, and the sandbox partners' view of your orders."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..accounts import Ctx, current, get_db
from ..assistant.tools.base import ToolError
from ..uber import service as uber
from . import service as bk

router = APIRouter()
_STATUS = {"NOT_FOUND": 404, "VALIDATION": 400, "CONFLICT": 409, "FORBIDDEN": 403}


def _who(ctx: Ctx) -> bk.Who:
    return bk.Who(ctx.user.id, ctx.org.id, ctx.user.timezone)


def _http(e: ToolError) -> HTTPException:
    return HTTPException(_STATUS.get(e.code, 400), e.message)


@router.get("/api/bookings")
async def list_mine(ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    rows = await bk.list_bookings(s, _who(ctx)) + await uber.list_rides(s, ctx.user.id)
    rows.sort(key=lambda b: b.get("createdAt") or "", reverse=True)
    return {"bookings": rows}


@router.post("/api/bookings/{booking_id}/cancel")
async def cancel(booking_id: str, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    """Only your own bookings: anyone else's answers "No booking with that id"."""
    try:
        try:
            await uber.get_ride(s, ctx, booking_id)
        except ToolError:
            return await bk.cancel_booking(s, _who(ctx), booking_id)
        return await uber.cancel_ride(s, ctx, booking_id)
    except ToolError as e:
        raise _http(e) from None


@router.get("/api/partners")
async def partners(ctx: Ctx = Depends(current)):
    return bk.partner_directory()


@router.get("/api/partners/food")
async def food_partner(restaurant: str = "r-slice-republic", ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    """The sandbox restaurant's view of your orders."""
    try:
        return await bk.food_portal(s, _who(ctx), restaurant)
    except ToolError as e:
        raise _http(e) from None


@router.get("/api/partners/cinema")
async def cinema_partner(cinema: str = "cn-galaxy-kor", ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    """The sandbox cinema's view of your bookings."""
    try:
        return await bk.cinema_portal(s, _who(ctx), cinema)
    except ToolError as e:
        raise _http(e) from None
