"""Uber tools (Riders API; test mode unless UBER_ENV=production): real options and fares, booking after the
user confirms, the ride's status, and moving test rides along. Places must come from the conversation."""
from __future__ import annotations

from pydantic import BaseModel, Field

from ...uber import service
from .base import Tool, ToolContext, ToolOutput
from .common import assert_places_given


class RouteArgs(BaseModel):
    pickup: str = Field(min_length=3, max_length=200)
    drop: str = Field(min_length=3, max_length=200)


class BookUberArgs(RouteArgs):
    product_id: str = Field(min_length=1, max_length=100)


class RideArgs(BaseModel):
    booking_id: str = Field(min_length=1)


async def _options(t: ToolContext, a: RouteArgs) -> ToolOutput:
    if not await service.is_connected(t.s, t):
        return ToolOutput(data={"connected": False, "message": "Uber isn't connected for this user. Use get_cab_options (sandbox) instead, "
                                                              "and mention they can connect Uber in Settings."},
                          summary="Uber not connected", link="/settings")
    r = await service.uber_options(t.s, t, a.pickup, a.drop, t.http)
    return ToolOutput(data={"connected": True, **r}, summary=f"{len(r['options'])} Uber options")


async def _book_preflight(t: ToolContext, a: BookUberArgs) -> str:
    await assert_places_given(t, [("pickup point", a.pickup), ("drop-off point", a.drop)])
    return service.describe_ride(await service.quote_ride(t.s, t, a.pickup, a.drop, a.product_id, t.http))


async def _book(t: ToolContext, a: BookUberArgs) -> ToolOutput:
    r = await service.book_ride(t.s, t, a.pickup, a.drop, a.product_id, t.http)
    ride = r["ride"]
    return ToolOutput(data={"booking_id": ride["id"], "title": ride["title"], "ride": r["status"], "note": r["note"]},
                      summary=f"{ride['title']} · {r['status']['label']}", link="/settings")


async def _status(t: ToolContext, a: RideArgs) -> ToolOutput:
    r = await service.get_ride_status(t.s, t, a.booking_id, t.http)
    return ToolOutput(data=r, summary=f"{r['title']}: {r['label']}", link="/settings")


async def _advance(t: ToolContext, a: RideArgs) -> ToolOutput:
    r = await service.advance_test_ride(t.s, t, a.booking_id, t.http)
    return ToolOutput(data=r, summary=f"{r['title']}: {r['label']}", link="/settings")


TOOLS = [
    Tool(name="get_uber_options", kind="read",
         description="Real Uber ride options, fares and pickup times between two places, if the user has connected their Uber account. "
                     "If not connected, it says so: then use get_cab_options (sandbox) instead.",
         args=RouteArgs, run=_options, label=lambda a: "Checking Uber fares"),
    Tool(name="book_uber", kind="sensitive",
         description="Book an Uber ride now (product_id from get_uber_options). Pickup is immediate; Uber rides can't be scheduled here. "
                     "The user confirms the route and fare first.",
         args=BookUberArgs, run=_book, preflight=_book_preflight, label=lambda a: "Booking an Uber"),
    Tool(name="get_ride_status", kind="read",
         description="Live status of one of the user's Uber rides (booking id from book_uber or list_bookings): stage, driver, car, pickup time.",
         args=RideArgs, run=_status, label=lambda a: "Checking your ride"),
    Tool(name="advance_test_ride", kind="write",
         description="Uber TEST MODE only, for demos: move one of the user's test rides to its next stage "
                     "(finding a driver → driver on the way → arriving → on the trip → completed). Only when the user asks.",
         args=RideArgs, run=_advance, label=lambda a: "Moving the test ride along"),
]
