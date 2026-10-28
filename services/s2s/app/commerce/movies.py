"""Sandbox cinema catalogue (ported from meetmind's providers/commerce/movies.ts): fictional films and
cinemas (no real chain), deterministic showtimes and seat availability. Nothing here reaches a real
cinema or payment network."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..assistant.tools.base import ToolError
from .catalog import h, js_round, norm, wall_time_to_utc

CINEMA_TZ = "Asia/Kolkata"


@dataclass
class Movie:
    id: str
    title: str
    language: str
    genre: str
    certificate: str
    minutes: int
    synopsis: str


@dataclass
class Cinema:
    id: str
    name: str
    area: str


@dataclass
class Show:
    id: str
    cinema: Cinema
    movie: Movie
    date: str
    time: str
    start: datetime


MOVIES = [
    Movie("mv-monsoon-express", "Monsoon Express", "Hindi", "Action thriller", "UA13+", 148, "A train, a storm and a missing ledger: one night from Mumbai to Goa."),
    Movie("mv-last-signal", "The Last Signal", "English", "Sci-fi", "UA13+", 132, "A deep-space relay picks up a message from Earth sent fifty years too late."),
    Movie("mv-kadhal-paadhai", "Kadhal Paadhai", "Tamil", "Romance drama", "U", 141, "Two strangers, one bus route, and a summer in Madurai."),
    Movie("mv-little-big-chef", "Little Big Chef", "English", "Animation · family", "U", 98, "A ten-year-old enters a national cooking contest with her grandmother's recipes."),
    Movie("mv-ghar-wapsi", "Ghar Wapsi", "Hindi", "Comedy drama", "U", 126, "Three siblings come home for a wedding nobody wanted to plan."),
]
CINEMAS = [
    Cinema("cn-galaxy-kor", "Galaxy Cinemas", "Koramangala"),
    Cinema("cn-starlight-wf", "Starlight Multiplex", "Whitefield"),
    Cinema("cn-regal-mg", "Regal Picture House", "MG Road"),
]
TIMES = ["10:30", "13:15", "16:00", "18:45", "21:45"]

ROWS = list("ABCDEFGHIJ")
SEATS_PER_ROW = 14
CATEGORIES = [("Classic", "ABC", 18000), ("Prime", "DEFGH", 26000), ("Recliner", "IJ", 45000)]   # name, rows, price
FEE_PER_TICKET_PAISE = 3000
GST_ON_FEE = 0.18
_SHOW = re.compile(r"^(cn-[a-z-]+)~(mv-[a-z-]+)~(\d{4})(\d{2})(\d{2})~(\d{2})(\d{2})$")
_SEAT = re.compile(r"^[A-J](?:[1-9]|1[0-4])$")


def category_of(seat: str) -> tuple[str, str, int]:
    return next(c for c in CATEGORIES if seat[0] in c[1])


def is_seat(s: str) -> bool:
    return bool(_SEAT.match(s))


def show_id(cinema_id: str, movie_id: str, date: str, time: str) -> str:
    """Show ids are readable and self-describing: cinema~movie~YYYYMMDD~HHmm."""
    return f"{cinema_id}~{movie_id}~{date.replace('-', '')}~{time.replace(':', '')}"


def shows_on(cinema_id: str, movie_id: str, date: str) -> list[str]:
    """Which times a film plays at a cinema on a date (3–4 of the 5 slots, varying by day)."""
    if h(f"{cinema_id}|{movie_id}") % 4 == 0:            # not every cinema shows every film
        return []
    return [t for t in TIMES if h(f"{cinema_id}|{movie_id}|{date}|{t}") % 5 != 0]


def parse_show_id(id: str) -> Show:
    m = _SHOW.match(id)
    unknown = ToolError("VALIDATION", "Unknown show. Use get_showtimes to find show ids.")
    if not m:
        raise unknown
    cinema = next((c for c in CINEMAS if c.id == m[1]), None)
    movie = next((x for x in MOVIES if x.id == m[2]), None)
    if cinema is None or movie is None:
        raise unknown
    date, time = f"{m[3]}-{m[4]}-{m[5]}", f"{m[6]}:{m[7]}"
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise unknown from None
    if time not in TIMES or time not in shows_on(cinema.id, movie.id, date):
        raise ToolError("VALIDATION", "That show doesn't exist. Use get_showtimes.")
    return Show(id, cinema, movie, date, time, wall_time_to_utc(date, time, CINEMA_TZ))


def search_movies(query: str | None = None, language: str | None = None, genre: str | None = None) -> list[Movie]:
    q = norm(query or "")
    return [m for m in MOVIES
            if (not language or norm(language) in norm(m.language))
            and (not genre or norm(genre) in norm(m.genre))
            and (not q or all(w in norm(f"{m.title} {m.genre} {m.language}") for w in q.split(" ")))]


def showtimes(movie_id: str, date: str, area: str | None = None, now: datetime | None = None) -> list[dict]:
    movie = next((m for m in MOVIES if m.id == movie_id), None)
    if movie is None:
        raise ToolError("NOT_FOUND", "Unknown film. Use search_movies.")
    now = now or datetime.now(timezone.utc)
    out = []
    for c in CINEMAS:
        if area and norm(area) not in norm(c.area) and norm(area) not in norm(c.name):
            continue
        for t in shows_on(c.id, movie.id, date):
            start = wall_time_to_utc(date, t, CINEMA_TZ)
            if start <= now + timedelta(minutes=15):      # bookable until 15 min before
                continue
            local = start.astimezone(ZoneInfo(CINEMA_TZ))
            out.append({"show_id": show_id(c.id, movie.id, date, t), "cinema": c.name, "area": c.area, "start": start,
                        "local": f"{local:%a} {local.day} {local:%b}, {t}"})
    return out


def taken_seats(show: str, booked: list[str]) -> set[str]:
    """Seats already sold for a show: deterministic "other customers" plus our own bookings."""
    taken = set(booked)
    for r in ROWS:
        for n in range(1, SEATS_PER_ROW + 1):
            if h(f"{show}|{r}{n}") % 100 < 38:
                taken.add(f"{r}{n}")
    return taken


def suggest_seats(taken: set[str], count: int, category: str | None = None) -> list[str] | None:
    """Best block of `count` adjacent free seats: Prime first, closest to the centre."""
    order = [r for r in "FGEHDIJCBA" if not category or category_of(f"{r}1")[0].lower() == category.lower()]
    centre = (SEATS_PER_ROW + 1) / 2
    for r in order:
        best, best_dist = None, float("inf")
        for s in range(1, SEATS_PER_ROW - count + 2):
            block = [f"{r}{s + i}" for i in range(count)]
            if any(x in taken for x in block):
                continue
            dist = abs(s + (count - 1) / 2 - centre)
            if dist < best_dist:
                best, best_dist = block, dist
        if best:
            return best
    return None


def price_seats(seats: list[str]) -> dict:
    tickets = sum(category_of(s)[2] for s in seats)
    fee = js_round(len(seats) * FEE_PER_TICKET_PAISE * (1 + GST_ON_FEE))
    return {"tickets_paise": tickets, "fee_paise": fee, "total_paise": tickets + fee}
