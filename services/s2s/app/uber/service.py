"""Uber for each user: their own connection (tokens encrypted at rest), real options and fares, booking after
a confirmed quote, the ride's status, moving test rides along, cancelling.

- A token about to expire (within a minute) is refreshed; a refused refresh marks the connection ERROR,
  and the user is asked to reconnect.
- The fare shown in a confirmation is held for 15 minutes; at booking a fresh fare is fetched and the
  booking is refused if it's more than 10% higher.
- Functions take a "ctx": anything with user_id/org_id (the assistant's ToolContext) or user.id/org.id
  (accounts.Ctx). Errors are ToolErrors with messages safe to show.
"""
from __future__ import annotations

import math
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func, select
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .. import db
from ..accounts import audit
from ..assistant.tools.base import ToolError
from ..crypto import CryptoError, decrypt_json, encrypt_json
from ..models import Base
from . import provider as P
from .geocode import geocode
from .provider import RIDE_FINAL, RIDE_LABEL, SANDBOX_STAGES, UberConfig, http_session, read_config, require_config

UBER_STATE_COOKIE = "mm2_uber_state"
UBER_TEST_NOTE = "Uber test mode — no real car comes and nothing is charged."
QUOTE_TTL_S = 15 * 60


class UberConnection(Base):
    __tablename__ = "uber_connections"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), unique=True)
    status: Mapped[str] = mapped_column(Text)
    encrypted_tokens: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    env: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UberRide(Base):
    __tablename__ = "uber_rides"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    request_id: Mapped[str] = mapped_column(Text)
    product_id: Mapped[str] = mapped_column(Text)
    product: Mapped[str] = mapped_column(Text)
    pickup: Mapped[str] = mapped_column(Text)
    drop_off: Mapped[str] = mapped_column(Text)
    fare: Mapped[str | None] = mapped_column(Text)
    fare_paise: Mapped[int | None] = mapped_column(Integer)
    ride_status: Mapped[str] = mapped_column(Text, server_default="processing")
    driver: Mapped[str | None] = mapped_column(Text)
    vehicle: Mapped[str | None] = mapped_column(Text)
    eta_min: Mapped[int | None] = mapped_column(Integer)
    env: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="CONFIRMED")
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("meetings.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def title(self) -> str:
        return f"Uber {self.product} to {self.drop_off}"


# ─── Small helpers ───────────────────────────────────────────────────────
def _ids(ctx) -> tuple[uuid.UUID, uuid.UUID]:
    """(org_id, user_id) of a ToolContext or an accounts.Ctx."""
    if hasattr(ctx, "user_id"):
        return ctx.org_id, ctx.user_id
    return ctx.org.id, ctx.user.id


def note_for(env: str) -> str:
    return UBER_TEST_NOTE if env == "sandbox" else "This books a REAL Uber ride, charged to your Uber account."


def provider_name(env: str) -> str:
    return "uber-sandbox" if env == "sandbox" else "uber"


def _round(x: float) -> int:
    return math.floor(x + 0.5)                       # like JavaScript's Math.round


def rupees(paise: int) -> str:
    """₹1,23,456 (Indian grouping), with paise only when there are some."""
    whole, frac = divmod(abs(paise), 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join([head, *groups, tail]) if head else ",".join([*groups, tail])
    return f"{'-' if paise < 0 else ''}₹{s}" + (f".{frac:02d}" if frac else "")


def _place(value: str) -> str:
    v = (value or "").strip()
    if len(v) < 3:
        raise ToolError("VALIDATION", "Give a fuller address")
    if len(v) > 200:
        raise ToolError("VALIDATION", "That address is too long")
    return v


# ─── Connection ──────────────────────────────────────────────────────────
async def _find_connection(s: AsyncSession, user_id) -> UberConnection | None:
    return (await s.execute(select(UberConnection).where(UberConnection.user_id == user_id))).scalar_one_or_none()


async def get_status(s: AsyncSession, ctx) -> dict:
    c = read_config()
    conn = await _find_connection(s, _ids(ctx)[1]) if c else None
    return {
        "configured": c is not None,
        "env": c.env if c else None,
        "connected": bool(conn and conn.status == "ACTIVE"),
        "needsReconnect": bool(conn and conn.status == "ERROR"),
        "connectedAt": conn.updated_at.isoformat() if conn and conn.status == "ACTIVE" else None,
    }


def start_connect(ctx) -> tuple[str, str]:
    """Starts "Connect Uber": (Uber's sign-in URL, a one-time state the caller keeps in a cookie)."""
    c = require_config()
    state = secrets.token_urlsafe(24)
    return P.authorize_url(c, state), state


async def finish_connect(s: AsyncSession, ctx, code: str, http=None) -> dict:
    org_id, user_id = _ids(ctx)
    c = require_config()
    async with http_session(http) as h:
        t = await P.exchange_code(h, c, code)
    conn = await _find_connection(s, user_id)
    if conn is None:
        conn = UberConnection(organization_id=org_id, user_id=user_id)
        s.add(conn)
    conn.organization_id = org_id
    conn.status = "ACTIVE"
    conn.encrypted_tokens = encrypt_json({"accessToken": t.access_token, "refreshToken": t.refresh_token})
    conn.scopes, conn.expires_at, conn.env = t.scopes, t.expires_at, c.env
    conn.updated_at = datetime.now(timezone.utc)
    await audit(s, "integration.uber_connect", org_id, user_id, env=c.env, scopes=t.scopes)
    await s.commit()
    return {"connected": True, "scopes": t.scopes}


async def disconnect(s: AsyncSession, ctx, http=None) -> dict:
    org_id, user_id = _ids(ctx)
    conn = await _find_connection(s, user_id)
    if conn is None:
        return {"connected": False}
    c = read_config()
    if c:
        try:
            token = decrypt_json(conn.encrypted_tokens)["accessToken"]
            async with http_session(http) as h:
                await P.revoke_token(h, c, token)
        except (CryptoError, KeyError, ValueError):
            pass                                     # the stored token is unreadable: deleting it is enough
    await s.delete(conn)
    await audit(s, "integration.uber_disconnect", org_id, user_id)
    await s.commit()
    return {"connected": False}


def _expired() -> ToolError:
    return ToolError("VALIDATION", "The Uber login has expired. Reconnect Uber in Settings.", {"reason": "UBER_NOT_CONNECTED"})


async def _access_token(ctx, http) -> tuple[UberConfig, str]:
    """A valid access token for the user, refreshed when it's about to expire.

    Uses its own database session, so a refreshed token is saved even when the caller (e.g. a tool's
    preflight) never commits."""
    c = require_config()
    user_id = _ids(ctx)[1]
    async with db.sessionmaker()() as s:
        conn = await _find_connection(s, user_id)
        if conn is None:
            raise ToolError("VALIDATION", "Uber isn't connected. Connect it in Settings.", {"reason": "UBER_NOT_CONNECTED"})
        if conn.status == "ERROR":
            raise _expired()
        try:
            stored = decrypt_json(conn.encrypted_tokens)
        except CryptoError:
            conn.status = "ERROR"
            await s.commit()
            raise _expired() from None
        if conn.expires_at is None or conn.expires_at > datetime.now(timezone.utc) + timedelta(seconds=60):
            return c, stored["accessToken"]
        if not stored.get("refreshToken"):
            conn.status = "ERROR"
            await s.commit()
            raise _expired()
        try:
            t = await P.refresh_tokens(http, c, stored["refreshToken"])
        except ToolError as e:
            if e.code == "PROVIDER_UNAVAILABLE":
                raise                                # Uber unreachable: keep the login
            conn.status = "ERROR"
            await s.commit()
            raise _expired() from None
        conn.encrypted_tokens = encrypt_json({"accessToken": t.access_token, "refreshToken": t.refresh_token})
        conn.expires_at, conn.updated_at = t.expires_at, datetime.now(timezone.utc)
        if t.scopes:
            conn.scopes = t.scopes
        await s.commit()
        return c, t.access_token


async def is_connected(s: AsyncSession, ctx) -> bool:
    if read_config() is None:
        return False
    conn = await _find_connection(s, _ids(ctx)[1])
    return bool(conn and conn.status == "ACTIVE")


# ─── Fares ───────────────────────────────────────────────────────────────
async def _locate(label: str, value: str, http) -> tuple[float, float]:
    g = await geocode(_place(value), http)
    if g is None:
        raise ToolError("VALIDATION", f"Couldn't find the {label} “{value}” on the map. Ask the user for a fuller address (area and city).")
    return g.lat, g.lng


async def _route(pickup: str, drop: str, http):
    return await _locate("pickup point", pickup, http), await _locate("drop-off point", drop, http)


async def uber_options(s: AsyncSession, ctx, pickup: str, drop: str, http=None) -> dict:
    async with http_session(http) as h:
        c, token = await _access_token(ctx, h)
        start, end = await _route(pickup, drop, h)
        products = await P.get_products(h, c, token, start)
        prices = await P.get_price_estimates(h, c, token, start, end)
        try:
            times = await P.get_time_estimates(h, c, token, start)
        except ToolError:
            times = []
    options = []
    for p in products:
        price = next((x for x in prices if x.get("product_id") == p.get("product_id")), None) or {}
        eta = next((x for x in times if x.get("product_id") == p.get("product_id")), None)
        surge = price.get("surge_multiplier")
        options.append({
            "product_id": p.get("product_id"),
            "name": p.get("display_name"),
            "seats": p.get("capacity"),
            "fare": price.get("estimate"),
            "surge": surge if surge and surge > 1 else None,
            "pickupEtaMin": _round(eta["estimate"] / 60) if eta else None,
            "tripMin": _round(price["duration"] / 60) if price.get("duration") else None,
            "distanceKm": _round(price["distance"] * 1.609 * 10) / 10 if price.get("distance") else None,
        })
    return {"env": c.env, "note": note_for(c.env), "options": options}


# Fares shown in a confirmation, so booking can't silently cost more than what the user agreed to.
# In memory: the confirmation and the booking happen in the same process.
_quotes: dict[str, tuple[float, str, float]] = {}       # key → (value, display, time.time())


def _quote_key(user_id, product_id: str, pickup: str, drop: str) -> str:
    return "|".join([str(user_id), product_id, pickup.strip().lower(), drop.strip().lower()])


@dataclass
class Quote:
    c: UberConfig
    token: str
    pickup: str
    drop: str
    product_id: str
    start: tuple[float, float]
    end: tuple[float, float]
    product: dict
    est: dict
    fare: str


async def quote_ride(s: AsyncSession, ctx, pickup: str, drop: str, product_id: str, http=None) -> Quote:
    pickup, drop = _place(pickup), _place(drop)
    if not product_id or len(product_id) > 100:
        raise ToolError("VALIDATION", "Give the product_id from get_uber_options.")
    async with http_session(http) as h:
        c, token = await _access_token(ctx, h)
        start, end = await _route(pickup, drop, h)
        est = await P.get_fare_estimate(h, c, token, product_id, start, end)
        products = await P.get_products(h, c, token, start)
    product = next((p for p in products if p.get("product_id") == product_id), None)
    if product is None:
        raise ToolError("VALIDATION", "That Uber option isn't available at this pickup. Use get_uber_options for the current ones.")
    fare_obj = est.get("fare")
    fare = (fare_obj or {}).get("display") or (est.get("estimate") or {}).get("display") or "fare shown in Uber"
    if fare_obj:
        if len(_quotes) > 5000:
            _quotes.clear()
        _quotes[_quote_key(_ids(ctx)[1], product_id, pickup, drop)] = (fare_obj["value"], fare_obj["display"], time.time())
    return Quote(c, token, pickup, drop, product_id, start, end, product, est, fare)


def describe_ride(q: Quote) -> str:
    """The sentence the user confirms."""
    eta = f", driver ~{q.est['pickup_estimate']} min away" if q.est.get("pickup_estimate") else ""
    dur = (q.est.get("trip") or {}).get("duration_estimate")
    trip = f", ~{_round(dur / 60)} min trip" if dur else ""
    return f"Book an Uber {q.product.get('display_name')} from “{q.pickup}” to “{q.drop}”, pickup now{eta}{trip}. Fare {q.fare}. {note_for(q.c.env)}"


async def book_ride(s: AsyncSession, ctx, pickup: str, drop: str, product_id: str, http=None, meeting_id=None) -> dict:
    """Books the ride (after the user confirmed a quote). Returns {ride (list_rides view), status (ride stage), note}."""
    org_id, user_id = _ids(ctx)
    key = _quote_key(user_id, product_id, (pickup or "").strip(), (drop or "").strip())
    agreed = _quotes.get(key)
    if agreed is None or time.time() - agreed[2] > QUOTE_TTL_S:
        raise ToolError("CONFLICT", "That fare has expired. Ask to book the ride again to see the current fare.")
    async with http_session(http) as h:
        # Fares (and their fare_id) expire after a couple of minutes: get a fresh one and hold it to what was agreed.
        q = await quote_ride(s, ctx, pickup, drop, product_id, h)
        fare_obj = q.est.get("fare")
        if fare_obj and fare_obj["value"] > agreed[0] * 1.1 + 1:
            raise ToolError("CONFLICT", f"The Uber fare went up from {agreed[1]} to {fare_obj['display']}. Ask the user whether to book at the new fare.")
        ride = await P.create_ride(h, q.c, q.token, product_id, (fare_obj or {}).get("fare_id"), q.start, q.end)
    _quotes.pop(key, None)
    r = UberRide(organization_id=org_id, user_id=user_id, request_id=ride.get("request_id") or "", product_id=product_id,
                 product=q.product.get("display_name") or product_id, pickup=q.pickup, drop_off=q.drop, fare=q.fare,
                 fare_paise=_round(fare_obj["value"] * 100) if fare_obj and fare_obj.get("currency_code") == "INR" else None,
                 ride_status=ride.get("status") or "processing", env=q.c.env, status="CONFIRMED", meeting_id=meeting_id)
    s.add(r)
    await s.flush()
    await audit(s, "booking.cab", org_id, user_id, "UberRide", r.id, provider=provider_name(q.c.env), fare=q.fare)
    await s.commit()
    return {"ride": ride_row_view(r), "status": ride_view(ride), "note": note_for(q.c.env)}


# ─── Ride status ─────────────────────────────────────────────────────────
def ride_view(r: dict) -> dict:
    """Uber's ride → stage, label, driver, car, pickup ETA."""
    st = r.get("status") or "processing"
    d, v = r.get("driver") or {}, r.get("vehicle")
    driver = None
    if d.get("name"):
        driver = d["name"] + (f" (★ {d['rating']:g})" if d.get("rating") else "")
    vehicle = " ".join(str(x) for x in (v.get("make"), v.get("model"), v.get("license_plate")) if x) or None if v else None
    return {"status": st, "label": RIDE_LABEL.get(st, st), "driver": driver, "vehicle": vehicle,
            "etaMin": (r.get("pickup") or {}).get("eta") or r.get("eta"), "final": st in RIDE_FINAL}


def ride_row_view(r: UberRide) -> dict:
    """A ride as a booking (the same shape as the bookings list), so the two can be merged."""
    return {
        "id": str(r.id), "kind": "CAB", "title": r.title, "reference": r.request_id[:8], "status": r.status,
        "provider": provider_name(r.env), "sandbox": False, "uber": True, "uberTest": r.env == "sandbox",
        "ride": {"status": r.ride_status, "label": RIDE_LABEL.get(r.ride_status, r.ride_status), "driver": r.driver, "vehicle": r.vehicle},
        "total": rupees(r.fare_paise) if r.fare_paise is not None else r.fare,
        "pickup": r.pickup, "drop": r.drop_off, "product": r.product,
        "scheduledFor": r.created_at.isoformat() if r.created_at else None,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
        "canceledAt": r.canceled_at.isoformat() if r.canceled_at else None,
    }


async def _own_ride(s: AsyncSession, ctx, ride_id, missing: str = "No Uber ride with that id.") -> UberRide:
    org_id, user_id = _ids(ctx)
    try:
        rid = ride_id if isinstance(ride_id, uuid.UUID) else uuid.UUID(str(ride_id))
    except ValueError:
        raise ToolError("NOT_FOUND", missing) from None
    r = (await s.execute(select(UberRide).where(UberRide.id == rid, UberRide.user_id == user_id,
                                                UberRide.organization_id == org_id))).scalar_one_or_none()
    if r is None:
        raise ToolError("NOT_FOUND", missing)
    return r


def _save(r: UberRide, v: dict):
    r.ride_status, r.driver, r.vehicle, r.eta_min = v["status"], v["driver"], v["vehicle"], v["etaMin"]
    if v["status"] in ("rider_canceled", "driver_canceled", "no_drivers_available") and r.status != "CANCELED":
        r.status, r.canceled_at = "CANCELED", datetime.now(timezone.utc)


async def get_ride_status(s: AsyncSession, ctx, ride_id, http=None) -> dict:
    """The live status of one of the user's rides (refreshed from Uber unless it's over)."""
    r = await _own_ride(s, ctx, ride_id)
    if r.ride_status in RIDE_FINAL:
        return {"id": str(r.id), "title": r.title, "status": r.ride_status, "label": RIDE_LABEL.get(r.ride_status, r.ride_status),
                "driver": r.driver, "vehicle": r.vehicle, "etaMin": None, "final": True}
    async with http_session(http) as h:
        c, token = await _access_token(ctx, h)
        v = ride_view(await P.get_ride(h, c, token, r.request_id))
    _save(r, v)
    await s.commit()
    return {"id": str(r.id), "title": r.title, **v}


async def advance_test_ride(s: AsyncSession, ctx, ride_id, http=None) -> dict:
    """Test mode only: move the ride to its next stage (Uber's sandbox never does it by itself)."""
    r = await _own_ride(s, ctx, ride_id)
    if r.env != "sandbox":
        raise ToolError("FORBIDDEN", "Only Uber test-mode rides can be moved along by hand.")
    async with http_session(http) as h:
        c, token = await _access_token(ctx, h)
        current = (await P.get_ride(h, c, token, r.request_id)).get("status") or ""
        i = SANDBOX_STAGES.index(current) if current in SANDBOX_STAGES else -1
        if i < 0 or i == len(SANDBOX_STAGES) - 1:
            raise ToolError("VALIDATION", f"The ride is {RIDE_LABEL.get(current, current)}: there's no next stage.")
        await P.set_sandbox_ride_status(h, c, token, r.request_id, SANDBOX_STAGES[i + 1])
        v = ride_view(await P.get_ride(h, c, token, r.request_id))
    _save(r, v)
    await s.commit()
    return {"id": str(r.id), "title": r.title, **v}


def assert_cancellable(r: UberRide):
    if r.status != "CONFIRMED":
        raise ToolError("VALIDATION", "That booking is already cancelled.")
    if r.ride_status in RIDE_FINAL:
        raise ToolError("VALIDATION", f"That ride is already {RIDE_LABEL.get(r.ride_status, 'over').lower()}.")
    if r.ride_status == "in_progress":
        raise ToolError("VALIDATION", "That ride is already under way. It can only be ended in the Uber app.")


async def get_ride(s: AsyncSession, ctx, ride_id) -> UberRide:
    """One of the user's rides (for a cancel confirmation)."""
    return await _own_ride(s, ctx, ride_id, "No booking with that id.")


async def cancel_ride(s: AsyncSession, ctx, ride_id, http=None) -> dict:
    """Cancels one of the user's rides with Uber, then here (commits). For cancel_booking too."""
    org_id, user_id = _ids(ctx)
    r = await _own_ride(s, ctx, ride_id, "No booking with that id.")
    assert_cancellable(r)
    async with http_session(http) as h:
        c, token = await _access_token(ctx, h)
        await P.cancel_ride(h, c, token, r.request_id)
    r.ride_status, r.status, r.canceled_at = "rider_canceled", "CANCELED", datetime.now(timezone.utc)
    await audit(s, "booking.cancel", org_id, user_id, "UberRide", r.id)
    await s.commit()
    return {"canceled": True, "title": r.title, "reference": r.request_id[:8]}


async def list_rides(s: AsyncSession, user_id, limit: int = 50) -> list[dict]:
    """The user's rides, newest first, as bookings (see ride_row_view)."""
    rows = (await s.execute(select(UberRide).where(UberRide.user_id == user_id)
                            .order_by(UberRide.created_at.desc()).limit(limit))).scalars().all()
    return [ride_row_view(r) for r in rows]


def reset_quotes():
    """Tests only."""
    _quotes.clear()
