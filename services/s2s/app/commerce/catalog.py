"""The sandbox commerce provider (ported from meetmind's providers/commerce/sandbox.ts and types.ts).

A fixed, fictional catalogue of Bengaluru restaurants, deterministic cab fares and table slots that
fill up. Nothing here reaches a real restaurant, cab company or payment network.
"""
from __future__ import annotations

import hashlib
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from ..assistant.tools.base import ToolError

TZ = "Asia/Kolkata"
CabType = Literal["AUTO", "MINI", "SEDAN", "SUV"]


@dataclass
class MenuItem:
    id: str
    name: str
    price_paise: int
    veg: bool


@dataclass
class Restaurant:
    id: str
    name: str
    cuisine: list[str]
    area: str
    rating: float
    price_for_two_paise: int
    delivery: tuple[int, int] | None          # (fee in paise, eta in minutes), or None: no delivery
    dine_in: int | None                       # tables per 30-minute slot, or None: no reservations
    menu: list[MenuItem] = field(default_factory=list)
    city: str = "Bengaluru"


@dataclass
class FoodLine:
    item_id: str
    name: str
    quantity: int
    unit_paise: int
    total_paise: int


@dataclass
class FoodQuote:
    restaurant_id: str
    restaurant_name: str
    lines: list[FoodLine]
    subtotal_paise: int
    delivery_fee_paise: int
    taxes_paise: int
    total_paise: int
    eta_minutes: int

    def as_json(self) -> dict:
        """Stored in the booking's details, in meetmind's shape."""
        return {"restaurant": {"id": self.restaurant_id, "name": self.restaurant_name},
                "lines": [{"itemId": l.item_id, "name": l.name, "quantity": l.quantity, "unitPaise": l.unit_paise,
                           "totalPaise": l.total_paise} for l in self.lines],
                "subtotalPaise": self.subtotal_paise, "deliveryFeePaise": self.delivery_fee_paise,
                "taxesPaise": self.taxes_paise, "totalPaise": self.total_paise, "etaMinutes": self.eta_minutes}


@dataclass
class CabOption:
    type: CabType
    label: str
    seats: int
    fare_paise: int
    pickup_eta_minutes: int
    trip_minutes: int
    distance_km: float


@dataclass
class TableSlot:
    start: datetime
    available: bool


def _m(id, name, price, veg=True):
    return MenuItem(id, name, price, veg)


SANDBOX_RESTAURANTS: list[Restaurant] = [
    Restaurant("r-spice-route", "Spice Route Kitchen", ["North Indian", "Mughlai"], "Indiranagar", 4.4, 90000, (4000, 35), 4, [
        _m("m-paneer-tikka", "Paneer Tikka", 28000), _m("m-butter-chicken", "Butter Chicken", 34000, False),
        _m("m-dal-makhani", "Dal Makhani", 24000), _m("m-garlic-naan", "Garlic Naan", 6000), _m("m-jeera-rice", "Jeera Rice", 16000)]),
    Restaurant("r-dosa-corner", "Dosa Corner", ["South Indian"], "Koramangala", 4.6, 40000, (2500, 25), 6, [
        _m("m-masala-dosa", "Masala Dosa", 12000), _m("m-idli-vada", "Idli Vada", 9000),
        _m("m-filter-coffee", "Filter Coffee", 4000), _m("m-pongal", "Ven Pongal", 11000)]),
    Restaurant("r-biryani-house", "Hyderabad Biryani House", ["Biryani", "Hyderabadi"], "HSR Layout", 4.3, 70000, (3500, 40), None, [
        _m("m-chicken-biryani", "Chicken Dum Biryani", 32000, False), _m("m-veg-biryani", "Veg Biryani", 26000),
        _m("m-mirchi-salan", "Mirchi Ka Salan", 12000), _m("m-double-ka-meetha", "Double Ka Meetha", 11000)]),
    Restaurant("r-green-bowl", "The Green Bowl", ["Healthy", "Salads"], "Whitefield", 4.2, 60000, (3000, 30), 3, [
        _m("m-quinoa-bowl", "Quinoa Power Bowl", 29000), _m("m-chicken-salad", "Grilled Chicken Salad", 31000, False),
        _m("m-cold-pressed", "Cold-pressed Juice", 14000)]),
    Restaurant("r-pasta-street", "Pasta Street", ["Italian", "Pizza"], "MG Road", 4.5, 120000, (5000, 45), 5, [
        _m("m-margherita", "Margherita Pizza", 38000), _m("m-arrabbiata", "Penne Arrabbiata", 36000),
        _m("m-tiramisu", "Tiramisu", 25000)]),
    Restaurant("r-slice-republic", "Slice Republic", ["Pizza", "Italian", "Fast food"], "Koramangala", 4.3, 70000, (3000, 30), None, [
        _m("m-margherita-m", "Margherita pizza (medium)", 29900), _m("m-margherita-l", "Margherita pizza (large)", 44900),
        _m("m-farmhouse-m", "Farmhouse pizza (medium)", 39900), _m("m-farmhouse-l", "Farmhouse pizza (large)", 57900),
        _m("m-peppy-paneer-m", "Peppy Paneer pizza (medium)", 42900),
        _m("m-chicken-tikka-m", "Chicken Tikka pizza (medium)", 46900, False),
        _m("m-chicken-tikka-l", "Chicken Tikka pizza (large)", 64900, False),
        _m("m-garlic-bread", "Garlic breadsticks", 14900), _m("m-choco-lava", "Choco lava cake", 10900),
        _m("m-cola", "Cola (500 ml)", 6000)]),
    Restaurant("r-coastal-curry", "Coastal Curry Co.", ["Seafood", "Mangalorean"], "Jayanagar", 4.4, 100000, None, 4, [
        _m("m-fish-curry", "Mangalorean Fish Curry", 42000, False), _m("m-neer-dosa", "Neer Dosa (3)", 9000),
        _m("m-prawn-ghee-roast", "Prawn Ghee Roast", 52000, False)]),
]


def h(s: str) -> int:
    """A stable number for a string (the first 4 bytes of its sha256, like meetmind's)."""
    return int.from_bytes(hashlib.sha256(s.encode()).digest()[:4], "big")


def js_round(x: float) -> int:
    """JavaScript's Math.round (halves go up), so fares match meetmind's to the paisa."""
    return math.floor(x + 0.5)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def ref(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3).upper()}"


def wall_time_to_utc(date: str, hhmm: str, tz: str) -> datetime:
    """A local date (YYYY-MM-DD) and time (HH:MM) in a zone, as a UTC instant."""
    y, m, d = (int(x) for x in date.split("-"))
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


CABS = [("AUTO", "Auto rickshaw", 3, 3000, 1500), ("MINI", "Mini (hatchback)", 4, 5000, 1400),
        ("SEDAN", "Sedan", 4, 7000, 1800), ("SUV", "SUV", 6, 10000, 2400)]   # type, label, seats, base, per km
# Lunch and dinner seatings, 30-minute slots (restaurant local time).
SLOT_TIMES = ["12:00", "12:30", "13:00", "13:30", "14:00", "19:00", "19:30", "20:00", "20:30", "21:00", "21:30", "22:00"]
UNKNOWN_RESTAURANT = "Unknown restaurant. Use search_restaurants to find one."


class SandboxCommerce:
    name = "sandbox"
    sandbox = True               # sandbox providers never place real orders or take payment
    timezone = TZ

    def search_restaurants(self, query: str | None = None, cuisine: str | None = None, mode: str | None = None) -> list[Restaurant]:
        q, c = norm(query or ""), norm(cuisine or "")
        out = []
        for r in SANDBOX_RESTAURANTS:
            if mode == "delivery" and not r.delivery:
                continue
            if mode == "dine_in" and not r.dine_in:
                continue
            if c and not any(c in norm(x) for x in r.cuisine):
                continue
            hay = norm(" ".join([r.name, r.area, *r.cuisine, *(m.name for m in r.menu)]))
            if not q or all(w in hay for w in q.split(" ")):
                out.append(r)
        return out

    def get_restaurant(self, id: str) -> Restaurant | None:
        return next((r for r in SANDBOX_RESTAURANTS if r.id == id), None)

    def quote_food(self, restaurant_id: str, items: list[tuple[str, int]]) -> FoodQuote:
        r = self.get_restaurant(restaurant_id)
        if r is None:
            raise ToolError("NOT_FOUND", UNKNOWN_RESTAURANT)
        if not r.delivery:
            raise ToolError("VALIDATION", f"{r.name} doesn't deliver; it only takes table reservations.")
        merged: dict[str, int] = {}
        for item_id, qty in items:
            merged[item_id] = merged.get(item_id, 0) + qty
        lines = []
        for item_id, qty in merged.items():
            m = next((x for x in r.menu if x.id == item_id), None)
            if m is None:
                raise ToolError("VALIDATION", f"“{item_id}” isn't on {r.name}'s menu. Use get_menu for item ids.")
            lines.append(FoodLine(item_id, m.name, qty, m.price_paise, m.price_paise * qty))
        subtotal = sum(l.total_paise for l in lines)
        taxes = js_round(subtotal * 0.05)
        fee, eta = r.delivery
        return FoodQuote(r.id, r.name, lines, subtotal, fee, taxes, subtotal + taxes + fee, eta)

    def place_food_order(self, quote: FoodQuote, address: str) -> tuple[str, int]:
        return ref("FD"), quote.eta_minutes

    def quote_cab(self, pickup: str, drop: str) -> list[CabOption]:
        a, b = sorted([norm(pickup), norm(drop)])
        if a == b:
            raise ToolError("VALIDATION", "Pickup and drop-off are the same place.")
        distance = 3 + (h(f"{a}|{b}") % 220) / 10          # 3–25 km, the same both ways
        return [CabOption(type=t, label=label, seats=seats, distance_km=round(distance * 10) / 10,
                          fare_paise=js_round((base + per_km * distance) / 100) * 100,
                          pickup_eta_minutes=3 + ((h(a) + i) % 8), trip_minutes=js_round(distance * 3.2))
                for i, (t, label, seats, base, per_km) in enumerate(CABS)]

    def book_cab(self, option: CabOption, pickup: str, drop: str, pickup_at: datetime) -> tuple[str, str, str]:
        drivers = ["Manjunath", "Suresh", "Imran", "Lakshmi", "Ravi", "Anitha"]
        n = h(option.type + str(time.time_ns())) % len(drivers)
        vehicle = f"{option.label} · KA 0{1 + n % 9} SB {1000 + h(str(n)) % 9000}"
        return ref("CB"), f"{drivers[n]} (sandbox driver)", vehicle

    def table_slots(self, restaurant_id: str, date: str, party_size: int, taken: Callable[[datetime], int]) -> list[TableSlot]:
        r = self.get_restaurant(restaurant_id)
        if r is None:
            raise ToolError("NOT_FOUND", UNKNOWN_RESTAURANT)
        if not r.dine_in:
            raise ToolError("VALIDATION", f"{r.name} is delivery-only; it doesn't take table reservations.")
        if party_size > 12:
            raise ToolError("VALIDATION", "For more than 12 people, please call the restaurant.")
        slots = []
        for t in SLOT_TIMES:
            start = wall_time_to_utc(date, t, TZ)
            busy = h(f"{r.id}|{date}|{t}") % 5 == 0        # some slots are already full
            slots.append(TableSlot(start, not busy and taken(start) < r.dine_in))
        return slots

    def reserve_table(self, restaurant_id: str, start: datetime, party_size: int) -> str:
        return ref("TB")


sandbox_commerce = SandboxCommerce()
