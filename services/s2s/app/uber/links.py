""""Open in Uber": a universal link that opens the Uber app (or m.uber.com) with the pickup and drop-off filled in.

The rider sees Uber's real prices and confirms the ride in Uber; nothing is booked or paid from MeetMind, so no
Uber API access is needed. Coordinates make Uber place the pins exactly; without them only the address text is passed.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode, urlparse

from .geocode import geocode

UBER_LINK_HOST = "m.uber.com"


@dataclass
class LinkPlace:
    address: str
    lat: float | None = None
    lng: float | None = None


def uber_ride_link(pickup: LinkPlace, drop: LinkPlace, client_id: str | None = None) -> str:
    q: dict[str, str] = {"action": "setPickup"}
    if client_id:
        q["client_id"] = client_id
    for side, p in (("pickup", pickup), ("dropoff", drop)):
        if p.lat is not None and p.lng is not None:
            q[f"{side}[latitude]"] = f"{p.lat:.6f}"
            q[f"{side}[longitude]"] = f"{p.lng:.6f}"
        q[f"{side}[nickname]"] = p.address
        q[f"{side}[formatted_address]"] = p.address
    return f"https://{UBER_LINK_HOST}/ul/?{urlencode(q)}"


def classify_result_link(link: str | None) -> Literal["internal", "uber"] | None:
    """Links the assistant may show: pages in this app, or an Uber ride link. Anything else is dropped."""
    if not link:
        return None
    if link.startswith("/") and not link.startswith("//"):
        return "internal"
    try:
        u = urlparse(link)
    except ValueError:
        return None
    return "uber" if u.scheme == "https" and u.hostname == UBER_LINK_HOST else None


async def uber_link_for(pickup: str, drop: str, http=None) -> str:
    """"Open in Uber" link for a route; coordinates when the geocoder answers within 3 s, else the addresses only."""
    try:
        p, d = await asyncio.wait_for(asyncio.gather(geocode(pickup, http), geocode(drop, http)), timeout=3)
    except Exception:  # noqa: BLE001 - too slow or failed: addresses only
        p = d = None
    at = lambda address, g: LinkPlace(address, g.lat, g.lng) if g else LinkPlace(address)  # noqa: E731
    return uber_ride_link(at(pickup, p), at(drop, d), os.environ.get("UBER_CLIENT_ID") or None)
