"""Bookings through the sandbox provider: food delivery, cabs, restaurant tables and movie tickets
(ported from meetmind's services/bookings.ts and services/partners.ts).

Quotes check everything and describe exactly what will happen (the assistant shows that sentence for
confirmation); booking functions re-check, write the bookings row and commit. Two people can't take
the last table of a slot or the same cinema seat: those bookings run under a Postgres advisory lock
per slot / per show and re-check inside it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from ..accounts import audit
from ..assistant.tools.base import ToolError
from ..models import Base
from .catalog import SANDBOX_RESTAURANTS, CabOption, FoodQuote, SandboxCommerce, sandbox_commerce
from .movies import (CATEGORIES, CINEMA_TZ, CINEMAS, MOVIES, ROWS, SEATS_PER_ROW, category_of, is_seat, parse_show_id,
                     price_seats, suggest_seats, taken_seats)


class Booking(Base):
    __tablename__ = "bookings"
    __mapper_args__ = {"eager_defaults": True}      # id, status and created_at come back from the insert
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(Text)                       # FOOD | CAB | TABLE | MOVIE
    provider: Mapped[str] = mapped_column(Text)                   # "sandbox"
    reference: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="CONFIRMED")    # CONFIRMED | CANCELED
    title: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    total_paise: Mapped[int | None] = mapped_column(Integer)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("meetings.id"))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@dataclass
class Who:
    """Who is booking: the user, their organisation and their time zone."""
    user_id: uuid.UUID
    org_id: uuid.UUID
    timezone: str


_override: SandboxCommerce | None = None


def get_commerce() -> SandboxCommerce:
    return _override or sandbox_commerce


def set_commerce_for_tests(p: SandboxCommerce | None):
    global _override
    _override = p


SANDBOX_NOTE = "Sandbox — no real order is placed and nothing is charged."
MAX_ORDER_PAISE = 20_000_00
FOOD_CANCEL_WINDOW = timedelta(minutes=5)


def rupees(paise: int) -> str:
    """₹1,23,456 or ₹299.50 (Indian digit grouping, paise only when there are some), like meetmind's."""
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


def local(d: datetime, tz: str) -> str:
    """Wed 28 Oct, 19:30"""
    x = d.astimezone(ZoneInfo(tz))
    return f"{x:%a} {x.day} {x:%b, %H:%M}"


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _place(value: str) -> str:
    v = value.strip()
    if len(v) < 3:
        raise ToolError("VALIDATION", "Give a fuller address")
    if len(v) > 200:
        raise ToolError("VALIDATION", "That address is too long.")
    return v


# ─── Food ────────────────────────────────────────────────────────────────
def quote_food_order(restaurant_id: str, items: list[tuple[str, int]], address: str) -> FoodQuote:
    _place(address)
    quote = get_commerce().quote_food(restaurant_id, items)
    if quote.total_paise > MAX_ORDER_PAISE:
        raise ToolError("VALIDATION", f"That order comes to {rupees(quote.total_paise)}, above the {rupees(MAX_ORDER_PAISE)} limit.")
    return quote


def describe_food_quote(q: FoodQuote, address: str) -> str:
    items = ", ".join(f"{l.quantity}× {l.name} ({rupees(l.total_paise)})" for l in q.lines)
    return (f"Order from {q.restaurant_name}: {items}. Delivery {rupees(q.delivery_fee_paise)}, taxes {rupees(q.taxes_paise)}, "
            f"total {rupees(q.total_paise)}, to “{address}”, in about {q.eta_minutes} min. {SANDBOX_NOTE}")


async def order_food(s: AsyncSession, who: Who, restaurant_id: str, items: list[tuple[str, int]], address: str,
                     notes: str | None = None, meeting_id: uuid.UUID | None = None, now: datetime | None = None) -> dict:
    address = _place(address)
    quote = quote_food_order(restaurant_id, items, address)
    commerce = get_commerce()
    reference, eta = commerce.place_food_order(quote, address)
    b = Booking(organization_id=who.org_id, user_id=who.user_id, kind="FOOD", provider=commerce.name, reference=reference,
                title=f"Order from {quote.restaurant_name}", total_paise=quote.total_paise,
                scheduled_for=_now(now) + timedelta(minutes=eta), meeting_id=meeting_id,
                details=quote.as_json() | {"address": address, "notes": (notes or "").strip() or None, "sandbox": commerce.sandbox})
    s.add(b)
    await s.flush()
    await audit(s, "booking.food", who.org_id, who.user_id, "Booking", b.id, totalPaise=quote.total_paise, provider=b.provider)
    await s.commit()
    return {"booking": view(b, who.timezone, now), "etaMinutes": eta}


# ─── Cabs ────────────────────────────────────────────────────────────────
def cab_options(pickup: str, drop: str) -> list[CabOption]:
    return get_commerce().quote_cab(_place(pickup), _place(drop))


def _pickup_time(at: datetime | None, now: datetime) -> datetime:
    t = at or now
    if t < now - timedelta(minutes=2):
        raise ToolError("VALIDATION", "That pickup time has already passed.")
    if t > now + timedelta(days=7):
        raise ToolError("VALIDATION", "Cabs can be booked up to 7 days ahead.")
    return t


def quote_cab(pickup: str, drop: str, cab_type: str, pickup_at: datetime | None = None, now: datetime | None = None) -> tuple[CabOption, datetime]:
    pickup_at = _pickup_time(pickup_at, _now(now))
    option = next(o for o in cab_options(pickup, drop) if o.type == cab_type)
    return option, pickup_at


def describe_cab(o: CabOption, pickup: str, drop: str, pickup_at: datetime, tz: str, now: datetime | None = None) -> str:
    when = f"now (driver ~{o.pickup_eta_minutes} min away)" if pickup_at - _now(now) < timedelta(minutes=5) else local(pickup_at, tz)
    return (f"Book a {o.label} from “{pickup.strip()}” to “{drop.strip()}”, pickup {when}. About {o.distance_km:g} km, "
            f"~{o.trip_minutes} min, fare {rupees(o.fare_paise)}. {SANDBOX_NOTE}")


async def book_cab(s: AsyncSession, who: Who, pickup: str, drop: str, cab_type: str, pickup_at: datetime | None = None,
                   meeting_id: uuid.UUID | None = None, now: datetime | None = None) -> dict:
    option, pickup_at = quote_cab(pickup, drop, cab_type, pickup_at, now)
    pickup, drop = pickup.strip(), drop.strip()
    commerce = get_commerce()
    reference, driver, vehicle = commerce.book_cab(option, pickup, drop, pickup_at)
    b = Booking(organization_id=who.org_id, user_id=who.user_id, kind="CAB", provider=commerce.name, reference=reference,
                title=f"{option.label} to {drop}", total_paise=option.fare_paise, scheduled_for=pickup_at, meeting_id=meeting_id,
                details={"type": option.type, "label": option.label, "seats": option.seats, "farePaise": option.fare_paise,
                         "pickupEtaMinutes": option.pickup_eta_minutes, "tripMinutes": option.trip_minutes,
                         "distanceKm": option.distance_km, "pickup": pickup, "drop": drop, "driver": driver,
                         "vehicle": vehicle, "sandbox": commerce.sandbox})
    s.add(b)
    await s.flush()
    await audit(s, "booking.cab", who.org_id, who.user_id, "Booking", b.id, farePaise=option.fare_paise)
    await s.commit()
    return {"booking": view(b, who.timezone, now), "driver": driver, "vehicle": vehicle}


# ─── Tables ──────────────────────────────────────────────────────────────
async def _taken_for(s: AsyncSession, restaurant_id: str, day: datetime):
    """How many confirmed reservations each slot start already has (around that day)."""
    rows = (await s.execute(select(Booking.scheduled_for).where(
        Booking.kind == "TABLE", Booking.status == "CONFIRMED", Booking.details["restaurantId"].astext == restaurant_id,
        Booking.scheduled_for >= day - timedelta(days=1), Booking.scheduled_for <= day + timedelta(days=2)))).scalars().all()
    return lambda start: sum(1 for r in rows if r == start)


def _date(date: str) -> datetime:
    try:
        return datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ToolError("VALIDATION", "Date must be YYYY-MM-DD.") from None


async def find_table_slots(s: AsyncSession, restaurant_id: str, date: str, party_size: int, now: datetime | None = None) -> list[dict]:
    day = _date(date)
    commerce = get_commerce()
    slots = commerce.table_slots(restaurant_id, date, party_size, await _taken_for(s, restaurant_id, day))
    cutoff = _now(now) + timedelta(minutes=30)
    return [{"start": x.start.isoformat(), "local": local(x.start, commerce.timezone), "available": x.available}
            for x in slots if x.start > cutoff]


async def quote_table(s: AsyncSession, restaurant_id: str, start: datetime, party_size: int, now: datetime | None = None):
    if not 1 <= party_size <= 12:
        raise ToolError("VALIDATION", "Party size must be between 1 and 12.")
    commerce = get_commerce()
    r = commerce.get_restaurant(restaurant_id)
    if r is None:
        raise ToolError("NOT_FOUND", "Unknown restaurant. Use search_restaurants to find one.")
    date = start.astimezone(ZoneInfo(commerce.timezone)).strftime("%Y-%m-%d")
    slots = commerce.table_slots(r.id, date, party_size, await _taken_for(s, r.id, start))
    slot = next((x for x in slots if x.start == start), None)
    if slot is None:
        raise ToolError("VALIDATION", f"{r.name} has no seating at that time. Use find_table_slots to see the times.")
    if not slot.available:
        raise ToolError("CONFLICT", f"{r.name} is full at {local(start, commerce.timezone)}. Offer the user another slot from find_table_slots.")
    if start < _now(now) + timedelta(minutes=30):
        raise ToolError("VALIDATION", "Tables must be booked at least 30 minutes ahead.")
    return r


def describe_table(name: str, area: str, start: datetime, party: int, tz: str, user_tz: str | None = None) -> str:
    """Restaurant-local time; the zone is named when it differs from the user's, so plans never look inconsistent."""
    other = (f" ({tz} time; {start.astimezone(ZoneInfo(user_tz)):%H:%M} in {user_tz})" if user_tz and user_tz != tz else "")
    return f"Reserve a table for {party} at {name} ({area}) on {local(start, tz)}{other}. {SANDBOX_NOTE}"


async def reserve_table(s: AsyncSession, who: Who, restaurant_id: str, start: datetime, party_size: int,
                        meeting_id: uuid.UUID | None = None, now: datetime | None = None) -> dict:
    # Serialise reservations for the same restaurant and slot, then re-check capacity inside the lock.
    key = f"table:{restaurant_id}:{start.astimezone(timezone.utc).isoformat()}"
    await s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
    try:
        r = await quote_table(s, restaurant_id, start, party_size, now)
        commerce = get_commerce()
        reference = commerce.reserve_table(r.id, start, party_size)
        b = Booking(organization_id=who.org_id, user_id=who.user_id, kind="TABLE", provider=commerce.name, reference=reference,
                    title=f"Table for {party_size} at {r.name}", scheduled_for=start, meeting_id=meeting_id,
                    details={"restaurantId": r.id, "restaurant": r.name, "area": r.area, "partySize": party_size,
                             "sandbox": commerce.sandbox})
        s.add(b)
        await s.flush()
        await audit(s, "booking.table", who.org_id, who.user_id, "Booking", b.id)
        await s.commit()
    except BaseException:
        await s.rollback()                  # releases the lock
        raise
    return {"booking": view(b, commerce.timezone, now)}


# ─── Listing and cancelling ──────────────────────────────────────────────
def view(b: Booking, tz: str, now: datetime | None = None) -> dict:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    created = b.created_at or _now(now)
    v = {"id": str(b.id), "kind": b.kind, "title": b.title, "reference": b.reference, "status": b.status,
         "provider": b.provider, "sandbox": b.provider == "sandbox",
         "total": rupees(b.total_paise) if b.total_paise is not None else None,
         "when": local(b.scheduled_for, tz) if b.scheduled_for else None,
         "scheduledFor": iso(b.scheduled_for), "createdAt": iso(created), "canceledAt": iso(b.canceled_at),
         "details": b.details}
    # Where a sandbox order stands now (the partner's lifecycle, on its accelerated clock).
    stages = None
    if b.kind == "FOOD":
        stages = food_stages(created, b.canceled_at, now)
    elif b.kind == "MOVIE" and b.scheduled_for:
        film = next((m for m in MOVIES if m.title == (b.details or {}).get("film")), None)
        stages = movie_stages(created, b.scheduled_for, film.minutes if film else 120, b.canceled_at, now)
    done = [x for x in stages or [] if x["done"]]
    if done:
        v["stage"] = done[-1]["name"]
    return v


async def list_bookings(s: AsyncSession, who: Who) -> list[dict]:
    rows = (await s.execute(select(Booking).where(Booking.user_id == who.user_id)
                            .order_by(Booking.created_at.desc()).limit(50))).scalars().all()
    return [view(b, who.timezone) for b in rows]


async def get_own_booking(s: AsyncSession, who: Who, id: str) -> Booking:
    try:
        bid = uuid.UUID(str(id))
    except ValueError:
        raise ToolError("NOT_FOUND", "No booking with that id.") from None
    b = (await s.execute(select(Booking).where(Booking.id == bid, Booking.user_id == who.user_id))).scalar_one_or_none()
    if b is None:
        raise ToolError("NOT_FOUND", "No booking with that id.")
    return b


async def assert_cancellable(s: AsyncSession, who: Who, id: str, now: datetime | None = None) -> Booking:
    b = await get_own_booking(s, who, id)
    if b.status != "CONFIRMED":
        raise ToolError("VALIDATION", "That booking is already cancelled.")
    now = _now(now)
    if b.kind == "FOOD" and now - b.created_at > FOOD_CANCEL_WINDOW:
        raise ToolError("VALIDATION", "Food orders can only be cancelled within 5 minutes of ordering.")
    if b.kind != "FOOD" and b.scheduled_for and b.scheduled_for < now:
        raise ToolError("VALIDATION", "That booking time has already passed.")
    return b


async def cancel_booking(s: AsyncSession, who: Who, id: str, now: datetime | None = None) -> dict:
    b = await assert_cancellable(s, who, id, now)
    done = (await s.execute(update(Booking).where(Booking.id == b.id, Booking.status == "CONFIRMED")
                            .values(status="CANCELED", canceled_at=_now(now)).returning(Booking.id))).first()
    if done is None:
        await s.rollback()
        raise ToolError("VALIDATION", "That booking is already cancelled.")
    await audit(s, "booking.cancel", who.org_id, who.user_id, "Booking", b.id)
    await s.commit()
    return {"canceled": True, "title": b.title, "reference": b.reference}


# ─── Movies (sandbox cinemas) ────────────────────────────────────────────
async def _booked_seats(s: AsyncSession, show_id: str) -> list[str]:
    rows = (await s.execute(select(Booking.details).where(
        Booking.kind == "MOVIE", Booking.status == "CONFIRMED", Booking.details["showId"].astext == show_id))).scalars().all()
    return [seat for d in rows for seat in (d.get("seats") or [])]


async def movie_seat_map(s: AsyncSession, show_id: str, count: int = 2) -> dict:
    """Free seats for a show, grouped by category with prices, plus a suggested block."""
    show = parse_show_id(show_id)
    taken = taken_seats(show.id, await _booked_seats(s, show.id))
    categories = [{"name": name, "price": rupees(price),
                   "rows": [{"row": r, "free": [f"{r}{i}" for i in range(1, SEATS_PER_ROW + 1) if f"{r}{i}" not in taken]}
                            for r in rows]}
                  for name, rows, price in CATEGORIES]
    return {"show": {"id": show.id, "film": show.movie.title, "cinema": show.cinema.name, "area": show.cinema.area,
                     "when": local(show.start, CINEMA_TZ), "screenAt": "row A"},
            "freeSeats": len(ROWS) * SEATS_PER_ROW - len(taken),
            "categories": categories,
            "suggestion": suggest_seats(taken, min(10, max(1, count))),
            "taken": sorted(taken)}


async def quote_movie(s: AsyncSession, show_id: str, seats: list[str], now: datetime | None = None) -> dict:
    show = parse_show_id(show_id)
    if show.start < _now(now) + timedelta(minutes=15):
        raise ToolError("VALIDATION", "Booking for this show has closed.")
    seats = list(dict.fromkeys(x.strip().upper() for x in seats))
    if not 1 <= len(seats) <= 10:
        raise ToolError("VALIDATION", "Book between 1 and 10 seats.")
    bad = [x for x in seats if not is_seat(x)]
    if bad:
        raise ToolError("VALIDATION", f"{', '.join(bad)} isn't a seat (rows A–J, seats 1–{SEATS_PER_ROW}).")
    taken = taken_seats(show.id, await _booked_seats(s, show.id))
    gone = [x for x in seats if x in taken]
    if gone:
        raise ToolError("CONFLICT", f"{', '.join(gone)} {'are' if len(gone) > 1 else 'is'} already taken. Use get_seat_map to pick free seats.")
    return {"show": show, "seats": seats, "price": price_seats(seats)}


def describe_movie(q: dict) -> str:
    show, seats, price = q["show"], q["seats"], q["price"]
    cats = " + ".join(dict.fromkeys(category_of(x)[0] for x in seats))
    return (f"Book {len(seats)} ticket{'s' if len(seats) > 1 else ''} for {show.movie.title} ({show.movie.language}, "
            f"{show.movie.certificate}) at {show.cinema.name}, {show.cinema.area}, {local(show.start, CINEMA_TZ)} — seats "
            f"{', '.join(seats)} ({cats}): tickets {rupees(price['tickets_paise'])} + fees {rupees(price['fee_paise'])} = "
            f"{rupees(price['total_paise'])}. {SANDBOX_NOTE}")


async def book_movie(s: AsyncSession, who: Who, show_id: str, seats: list[str], meeting_id: uuid.UUID | None = None,
                     now: datetime | None = None) -> dict:
    # One sale at a time per show, so two people can't buy the same seat.
    await s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"movie:{show_id}"})
    try:
        q = await quote_movie(s, show_id, seats, now)
        show, price = q["show"], q["price"]
        b = Booking(organization_id=who.org_id, user_id=who.user_id, kind="MOVIE", provider="sandbox",
                    reference=f"MV-{uuid.uuid4().hex[:6].upper()}",
                    title=f"{len(q['seats'])} × {show.movie.title} at {show.cinema.name}", total_paise=price["total_paise"],
                    scheduled_for=show.start, meeting_id=meeting_id,
                    details={"showId": show.id, "film": show.movie.title, "cinema": show.cinema.name, "area": show.cinema.area,
                             "seats": q["seats"], "ticketsPaise": price["tickets_paise"], "feePaise": price["fee_paise"],
                             "sandbox": True})
        s.add(b)
        await s.flush()
        await audit(s, "booking.movie", who.org_id, who.user_id, "Booking", b.id, seats=len(q["seats"]))
        await s.commit()
    except BaseException:
        await s.rollback()                  # releases the lock
        raise
    return {"booking": view(b, CINEMA_TZ, now), "seats": q["seats"]}


# ─── Sandbox partner portals (meetmind's services/partners.ts) ───────────
# What the restaurant and the cinema "see" when the assistant submits an order: only the signed-in
# user's own orders (a demo of the hand-off, not a merchant system), on an accelerated clock so a
# demo shows every stage.
FOOD_STAGES = [("Order received", 0), ("Accepted by the restaurant", 15), ("Being prepared", 45),
               ("Out for delivery", 120), ("Delivered", 240)]                # seconds after ordering


def food_stages(created_at: datetime, canceled_at: datetime | None, now: datetime | None = None) -> list[dict]:
    now = _now(now)
    out = []
    for name, secs in FOOD_STAGES:
        t = created_at + timedelta(seconds=secs)
        done = t <= now and (canceled_at is None or t <= canceled_at)
        out.append({"name": name, "at": t.isoformat() if done else None, "done": done})
    if canceled_at:
        out.append({"name": "Cancelled by the customer", "at": canceled_at.isoformat(), "done": True})
    return out


def movie_stages(created_at: datetime, show: datetime, minutes: int, canceled_at: datetime | None,
                 now: datetime | None = None) -> list[dict]:
    now = _now(now)

    def at(name: str, t: datetime, ok: bool = True) -> dict:
        done = ok and t <= now
        return {"name": name, "at": t.isoformat() if done else None, "done": done}

    ok = canceled_at is None
    out = [at("Booking received", created_at), at("Seats blocked and tickets issued", created_at + timedelta(seconds=5), ok),
           at("Showtime", show, ok), at("Show finished", show + timedelta(minutes=minutes), ok)]
    if canceled_at:
        out.append({"name": "Cancelled, seats released", "at": canceled_at.isoformat(), "done": True})
    return out


async def food_portal(s: AsyncSession, who: Who, restaurant_id: str) -> dict:
    r = next((x for x in SANDBOX_RESTAURANTS if x.id == restaurant_id), None)
    if r is None:
        raise ToolError("NOT_FOUND", "Unknown restaurant.")
    orders = (await s.execute(select(Booking).where(
        Booking.user_id == who.user_id, Booking.organization_id == who.org_id, Booking.kind == "FOOD",
        Booking.details["restaurant"]["id"].astext == r.id).order_by(Booking.created_at.desc()).limit(30))).scalars().all()
    return {"restaurant": {"id": r.id, "name": r.name, "area": r.area},
            "orders": [{"id": str(o.id), "reference": o.reference, "receivedAt": o.created_at.isoformat(), "status": o.status,
                        "items": [{"name": l["name"], "quantity": l["quantity"], "total": rupees(l["totalPaise"])} for l in o.details["lines"]],
                        "address": o.details.get("address"), "notes": o.details.get("notes"),
                        "subtotal": rupees(o.details["subtotalPaise"]), "delivery": rupees(o.details["deliveryFeePaise"]),
                        "taxes": rupees(o.details["taxesPaise"]), "total": rupees(o.total_paise or 0),
                        "stages": food_stages(o.created_at, o.canceled_at)} for o in orders]}


async def cinema_portal(s: AsyncSession, who: Who, cinema_id: str) -> dict:
    c = next((x for x in CINEMAS if x.id == cinema_id), None)
    if c is None:
        raise ToolError("NOT_FOUND", "Unknown cinema.")
    rows = (await s.execute(select(Booking).where(
        Booking.user_id == who.user_id, Booking.organization_id == who.org_id, Booking.kind == "MOVIE")
        .order_by(Booking.created_at.desc()).limit(60))).scalars().all()
    mine = [b for b in rows if str(b.details.get("showId", "")).startswith(f"{c.id}~")][:20]
    out = []
    for b in mine:
        d = b.details
        show = parse_show_id(d["showId"])
        sold = taken_seats(d["showId"], [])
        out.append({"id": str(b.id), "reference": b.reference, "receivedAt": b.created_at.isoformat(), "status": b.status,
                    "film": show.movie.title, "language": show.movie.language, "certificate": show.movie.certificate,
                    "when": local(show.start, CINEMA_TZ), "seats": d["seats"],
                    "tickets": rupees(d["ticketsPaise"]), "fees": rupees(d["feePaise"]), "total": rupees(b.total_paise or 0),
                    # Hall layout for the portal: this booking's seats vs. seats sold to others.
                    "hall": [["mine" if f"{r}{i}" in d["seats"] else "sold" if f"{r}{i}" in sold else "free"
                              for i in range(1, SEATS_PER_ROW + 1)] for r in ROWS],
                    "stages": movie_stages(b.created_at, show.start, show.movie.minutes, b.canceled_at)})
    return {"cinema": {"id": c.id, "name": c.name, "area": c.area}, "bookings": out}


def partner_directory() -> dict:
    return {"restaurants": [{"id": r.id, "name": r.name, "area": r.area, "cuisine": ", ".join(r.cuisine)}
                            for r in SANDBOX_RESTAURANTS if r.delivery],
            "cinemas": [{"id": c.id, "name": c.name, "area": c.area, "films": len(MOVIES)} for c in CINEMAS]}
