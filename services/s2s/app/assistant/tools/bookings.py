"""Bookings tools (sandbox provider): restaurants and food delivery, cabs, restaurant tables, movie
tickets, listing and cancelling (ported from meetmind's src/server/ai/tools.ts). Places (delivery
address, pickup, drop-off) must come from the conversation."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select

from ...commerce import service as bk
from ...commerce.movies import MOVIES, search_movies, showtimes
from ...models import Meeting
from ...uber import links as uber_links
from ...uber import service as uber
from .base import Tool, ToolContext, ToolError, ToolOutput
from .common import assert_places_given

NOTE = bk.SANDBOX_NOTE
DATE = r"^\d{4}-\d{2}-\d{2}$"


def _who(t: ToolContext) -> bk.Who:
    return bk.Who(t.user_id, t.org_id, t.timezone)


async def _meeting_id(t: ToolContext):
    """The call this chat is attached to, so a booking made in a meeting remembers it."""
    if not t.meeting_code:
        return None
    return (await t.s.execute(select(Meeting.id).where(Meeting.join_code == t.meeting_code,
                                                       Meeting.organization_id == t.org_id))).scalar_one_or_none()


# ─── Restaurants and food ────────────────────────────────────────────────
class SearchRestaurantsArgs(BaseModel):
    query: str | None = Field(default=None, max_length=100, description="Dish, restaurant name or area")
    cuisine: str | None = Field(default=None, max_length=50)
    mode: Literal["delivery", "dine_in"] | None = None


async def _search_restaurants(t: ToolContext, a: SearchRestaurantsArgs) -> ToolOutput:
    c = bk.get_commerce()
    found = c.search_restaurants(a.query, a.cuisine, a.mode)
    return ToolOutput(data={"sandbox": c.sandbox, "restaurants": [
        {"id": r.id, "name": r.name, "cuisine": ", ".join(r.cuisine), "area": r.area, "rating": r.rating,
         "priceForTwo": bk.rupees(r.price_for_two_paise), "delivers": bool(r.delivery),
         "deliveryEtaMin": r.delivery[1] if r.delivery else None, "takesReservations": bool(r.dine_in)} for r in found]},
        summary=f"{len(found)} restaurant(s)")


class MenuArgs(BaseModel):
    restaurant_id: str = Field(min_length=1)


async def _get_menu(t: ToolContext, a: MenuArgs) -> ToolOutput:
    r = bk.get_commerce().get_restaurant(a.restaurant_id)
    if r is None:
        raise ToolError("NOT_FOUND", "Unknown restaurant id. Use search_restaurants.")
    return ToolOutput(data={"restaurant": r.name, "delivers": bool(r.delivery),
                            "items": [{"id": m.id, "name": m.name, "price": bk.rupees(m.price_paise), "veg": m.veg} for m in r.menu]},
                      summary=f"{len(r.menu)} items at {r.name}")


class OrderItem(BaseModel):
    item_id: str = Field(min_length=1)
    quantity: int = Field(ge=1, le=20)


class OrderFoodArgs(BaseModel):
    restaurant_id: str = Field(min_length=1)
    items: list[OrderItem] = Field(min_length=1, max_length=20)
    delivery_address: str = Field(min_length=3, max_length=200, description="As the user said it; ask if not given")
    notes: str | None = Field(default=None, max_length=200)


def _items(a: OrderFoodArgs) -> list[tuple[str, int]]:
    return [(i.item_id, i.quantity) for i in a.items]


async def _order_food_preflight(t: ToolContext, a: OrderFoodArgs) -> str:
    await assert_places_given(t, [("delivery address", a.delivery_address)])
    return bk.describe_food_quote(bk.quote_food_order(a.restaurant_id, _items(a), a.delivery_address), a.delivery_address)


async def _order_food(t: ToolContext, a: OrderFoodArgs) -> ToolOutput:
    r = await bk.order_food(t.s, _who(t), a.restaurant_id, _items(a), a.delivery_address, a.notes, await _meeting_id(t), t.now)
    b = r["booking"]
    return ToolOutput(data={"reference": b["reference"], "title": b["title"], "total": b["total"], "etaMinutes": r["etaMinutes"],
                            "sandbox": b["sandbox"], "note": NOTE},
                      summary=f"{b['title']} · {b['total']}", link="/bookings")


# ─── Cabs ────────────────────────────────────────────────────────────────
class CabOptionsArgs(BaseModel):
    pickup: str = Field(min_length=3, max_length=200)
    drop: str = Field(min_length=3, max_length=200)


async def _get_cab_options(t: ToolContext, a: CabOptionsArgs) -> ToolOutput:
    opts = bk.cab_options(a.pickup, a.drop)
    return ToolOutput(data={"sandbox": bk.get_commerce().sandbox, "options": [
        {"type": o.type, "label": o.label, "seats": o.seats, "fare": bk.rupees(o.fare_paise), "pickupEtaMin": o.pickup_eta_minutes,
         "tripMin": o.trip_minutes, "distanceKm": o.distance_km} for o in opts],
        "uber_connected": await uber.is_connected(t.s, t),
        "note": "Sandbox fares: no real cab. If the user has connected Uber, get_uber_options gives real Uber options."},
        summary=f"{len(opts)} cab options", link=await uber_links.uber_link_for(a.pickup, a.drop, t.http))


class BookCabArgs(BaseModel):
    pickup: str = Field(min_length=3, max_length=200)
    drop: str = Field(min_length=3, max_length=200)
    cab_type: Literal["AUTO", "MINI", "SEDAN", "SUV"]
    pickup_time: AwareDatetime | None = Field(default=None, description="Pickup time as ISO 8601 with offset — get it from resolve_datetime")


async def _book_cab_preflight(t: ToolContext, a: BookCabArgs) -> str:
    await assert_places_given(t, [("pickup point", a.pickup), ("drop-off point", a.drop)])
    option, pickup_at = bk.quote_cab(a.pickup, a.drop, a.cab_type, a.pickup_time, t.now)
    return bk.describe_cab(option, a.pickup, a.drop, pickup_at, t.timezone, t.now)


async def _book_cab(t: ToolContext, a: BookCabArgs) -> ToolOutput:
    r = await bk.book_cab(t.s, _who(t), a.pickup, a.drop, a.cab_type, a.pickup_time, await _meeting_id(t), t.now)
    b = r["booking"]
    return ToolOutput(data={"reference": b["reference"], "title": b["title"], "fare": b["total"], "pickup": b["when"],
                            "driver": r["driver"], "vehicle": r["vehicle"], "note": NOTE},
                      summary=f"{b['title']} · {b['total']}", link="/bookings")


# ─── Tables ──────────────────────────────────────────────────────────────
class TableSlotsArgs(BaseModel):
    restaurant_id: str = Field(min_length=1)
    date: str = Field(pattern=DATE)
    party_size: int = Field(ge=1, le=12)


async def _find_table_slots(t: ToolContext, a: TableSlotsArgs) -> ToolOutput:
    c = bk.get_commerce()
    slots = await bk.find_table_slots(t.s, a.restaurant_id, a.date, a.party_size, t.now)
    return ToolOutput(data={"sandbox": c.sandbox, "timezone": c.timezone,
                            "slots": [{"start_time": x["start"], "local": x["local"], "available": x["available"]} for x in slots]},
                      summary=f"{sum(1 for x in slots if x['available'])} free slot(s)")


class ReserveTableArgs(BaseModel):
    restaurant_id: str = Field(min_length=1)
    start_time: AwareDatetime = Field(description="Slot start as ISO 8601 with offset — get it from resolve_datetime")
    party_size: int = Field(ge=1, le=12)


async def _reserve_table_preflight(t: ToolContext, a: ReserveTableArgs) -> str:
    c = bk.get_commerce()
    r = await bk.quote_table(t.s, a.restaurant_id, a.start_time, a.party_size, t.now)
    return bk.describe_table(r.name, r.area, a.start_time, a.party_size, c.timezone, t.timezone)


async def _reserve_table(t: ToolContext, a: ReserveTableArgs) -> ToolOutput:
    meeting_id = await _meeting_id(t)
    r = await bk.reserve_table(t.s, _who(t), a.restaurant_id, a.start_time, a.party_size, meeting_id, t.now)
    b = r["booking"]
    return ToolOutput(data={"reference": b["reference"], "title": b["title"], "when": b["when"], "note": NOTE},
                      summary=f"{b['title']} · {b['when']}", link="/bookings")


# ─── Listing and cancelling ──────────────────────────────────────────────
class NoArgs(BaseModel):
    pass


async def _list_bookings(t: ToolContext, a: NoArgs) -> ToolOutput:
    rows = await bk.list_bookings(t.s, _who(t)) + await uber.list_rides(t.s, t.user_id)
    rows.sort(key=lambda b: b.get("createdAt") or "", reverse=True)
    return ToolOutput(data={"bookings": [{k: b.get(k) for k in ("id", "kind", "title", "reference", "status", "total", "when", "sandbox", "uber", "ride")
                                          if b.get(k) is not None} for b in rows]},
                      summary=f"{len(rows)} booking(s)", link="/bookings")


class CancelBookingArgs(BaseModel):
    booking_id: str = Field(min_length=1)


async def _uber_ride(t: ToolContext, booking_id: str):
    """The user's Uber ride with this id, or None (then it's a sandbox booking)."""
    try:
        return await uber.get_ride(t.s, t, booking_id)
    except ToolError:
        return None


async def _cancel_booking_preflight(t: ToolContext, a: CancelBookingArgs) -> str:
    ride = await _uber_ride(t, a.booking_id)
    if ride is not None:
        uber.assert_cancellable(ride)
        return f"Cancel the {ride.title} (Uber ref {ride.request_id[:8]})." + (" Uber test mode: no real ride." if ride.env == "sandbox" else "")
    b = await bk.assert_cancellable(t.s, _who(t), a.booking_id, t.now)
    return f"Cancel {b.title} (ref {b.reference[:12]}).{' Sandbox booking.' if b.provider == 'sandbox' else ''}"


async def _cancel_booking(t: ToolContext, a: CancelBookingArgs) -> ToolOutput:
    if await _uber_ride(t, a.booking_id) is not None:
        r = await uber.cancel_ride(t.s, t, a.booking_id, t.http)
        return ToolOutput(data=r, summary=f"Cancelled {r['title']}", link="/bookings")
    r = await bk.cancel_booking(t.s, _who(t), a.booking_id, t.now)
    return ToolOutput(data=r, summary=f"Cancelled {r['title']}", link="/bookings")


# ─── Movies (sandbox cinemas) ────────────────────────────────────────────
class SearchMoviesArgs(BaseModel):
    query: str | None = Field(default=None, max_length=100)
    language: str | None = Field(default=None, max_length=30)
    genre: str | None = Field(default=None, max_length=40)


async def _search_movies(t: ToolContext, a: SearchMoviesArgs) -> ToolOutput:
    found = search_movies(a.query, a.language, a.genre)
    return ToolOutput(data={"sandbox": True, "films": [
        {"id": m.id, "title": m.title, "language": m.language, "genre": m.genre, "certificate": m.certificate,
         "runtime": f"{m.minutes // 60}h {m.minutes % 60}m", "synopsis": m.synopsis} for m in found]},
        summary=f"{len(found)} film(s)")


class ShowtimesArgs(BaseModel):
    movie_id: str = Field(min_length=1)
    date: str = Field(pattern=DATE)
    area: str | None = Field(default=None, max_length=60)


async def _get_showtimes(t: ToolContext, a: ShowtimesArgs) -> ToolOutput:
    shows = showtimes(a.movie_id, a.date, a.area, t.now)
    film = next((m.title for m in MOVIES if m.id == a.movie_id), None)
    return ToolOutput(data={"film": film, "shows": [{"show_id": x["show_id"], "cinema": x["cinema"], "area": x["area"], "when": x["local"]}
                                                    for x in shows]},
                      summary=f"{len(shows)} show(s)")


class SeatMapArgs(BaseModel):
    show_id: str = Field(min_length=10)
    party_size: int = Field(default=2, ge=1, le=10)


async def _get_seat_map(t: ToolContext, a: SeatMapArgs) -> ToolOutput:
    m = await bk.movie_seat_map(t.s, a.show_id, a.party_size)
    sug = m["suggestion"]
    return ToolOutput(data={"show": m["show"], "freeSeats": m["freeSeats"], "suggestedSeats": sug,
                            "categories": [{"name": c["name"], "price": c["price"],
                                            "freeByRow": {r["row"]: " ".join(r["free"]) for r in c["rows"]}} for c in m["categories"]]},
                      summary=f"{m['freeSeats']} free seats" + (f" · suggest {', '.join(sug)}" if sug else ""))


class BookMovieArgs(BaseModel):
    show_id: str = Field(min_length=10)
    seats: list[Annotated[str, Field(min_length=2, max_length=3)]] = Field(min_length=1, max_length=10)


async def _book_movie_preflight(t: ToolContext, a: BookMovieArgs) -> str:
    return bk.describe_movie(await bk.quote_movie(t.s, a.show_id, a.seats, t.now))


async def _book_movie(t: ToolContext, a: BookMovieArgs) -> ToolOutput:
    meeting_id = await _meeting_id(t)
    r = await bk.book_movie(t.s, _who(t), a.show_id, a.seats, meeting_id, t.now)
    b = r["booking"]
    return ToolOutput(data={"reference": b["reference"], "title": b["title"], "seats": r["seats"], "total": b["total"],
                            "when": b["when"], "note": NOTE},
                      summary=f"{b['title']} · {b['total']}", link="/bookings")


TOOLS = [
    Tool(name="search_restaurants", kind="read",
         description=f"Find restaurants for food delivery or a table. {NOTE} Mention that when you present results.",
         args=SearchRestaurantsArgs, run=_search_restaurants, label=lambda a: "Finding restaurants"),
    Tool(name="get_menu", kind="read", description="Get a restaurant's menu with item ids and prices (for order_food).",
         args=MenuArgs, run=_get_menu, label=lambda a: "Reading the menu"),
    Tool(name="order_food", kind="sensitive",
         description="Order food for delivery. Needs item ids from get_menu and a delivery address the user gave. "
                     "The user confirms the items and total before anything is ordered.",
         args=OrderFoodArgs, run=_order_food, preflight=_order_food_preflight, label=lambda a: "Ordering food"),
    Tool(name="get_cab_options", kind="read",
         description=f"Fare estimates for a cab between two places (auto, mini, sedan, SUV). {NOTE}",
         args=CabOptionsArgs, run=_get_cab_options, label=lambda a: "Checking cab fares"),
    Tool(name="book_cab", kind="sensitive",
         description="Book a cab. pickup_time is optional (ISO from resolve_datetime); omit for 'now'. "
                     "The user confirms the route, time and fare first.",
         args=BookCabArgs, run=_book_cab, preflight=_book_cab_preflight, label=lambda a: "Booking a cab"),
    Tool(name="find_table_slots", kind="read",
         description="Available table times at a restaurant on a date (YYYY-MM-DD, the restaurant's local date) for a party size.",
         args=TableSlotsArgs, run=_find_table_slots, label=lambda a: "Checking table availability"),
    Tool(name="reserve_table", kind="sensitive",
         description="Reserve a table. start_time must be one of the available start_time values from find_table_slots. "
                     "The user confirms first.",
         args=ReserveTableArgs, run=_reserve_table, preflight=_reserve_table_preflight, label=lambda a: "Reserving a table"),
    Tool(name="list_bookings", kind="read",
         description="The user's recent food orders, cabs, Uber rides, table reservations and movie tickets.",
         args=NoArgs, run=_list_bookings, label=lambda a: "Checking your bookings"),
    Tool(name="cancel_booking", kind="sensitive",
         description="Cancel a food order (within 5 minutes), cab, Uber ride (before the trip starts), table reservation or movie booking (id from list_bookings). "
                     "The user confirms first.",
         args=CancelBookingArgs, run=_cancel_booking, preflight=_cancel_booking_preflight, label=lambda a: "Cancelling a booking"),
    Tool(name="search_movies", kind="read",
         description=f"Films now showing (sandbox cinemas in Bengaluru). Filter by title words, language or genre. {NOTE}",
         args=SearchMoviesArgs, run=_search_movies, label=lambda a: "Finding films"),
    Tool(name="get_showtimes", kind="read",
         description="Showtimes for a film on a date (YYYY-MM-DD, local date in Bengaluru), optionally near an area. "
                     "Returns show_id values for get_seat_map / book_movie_tickets.",
         args=ShowtimesArgs, run=_get_showtimes, label=lambda a: "Checking showtimes"),
    Tool(name="get_seat_map", kind="read",
         description="Free seats for a show by category (Classic rows A–C, Prime D–H, Recliner I–J) with prices, and a suggested "
                     "block of adjacent seats for the party size. Row A is nearest the screen.",
         args=SeatMapArgs, run=_get_seat_map, label=lambda a: "Checking seats"),
    Tool(name="book_movie_tickets", kind="sensitive",
         description="Book specific seats for a show (show_id from get_showtimes, seats like F7 from get_seat_map). Ask the user "
                     "which film, date/time and how many people if not given; suggest seats from get_seat_map. The user confirms "
                     "seats and total first.",
         args=BookMovieArgs, run=_book_movie, preflight=_book_movie_preflight, label=lambda a: f"Booking {len(a.seats)} ticket(s)"),
]
