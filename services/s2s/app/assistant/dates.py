"""Dates and times from what people say ("tomorrow at 3 PM for 30 minutes"), in the user's time zone,
without the LLM doing date arithmetic (dateparser, preferring future dates)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import dateparser
from dateparser.search import search_dates

_DUR = [re.compile(r"\bfor\s+(\d+(?:\.\d+)?)\s*(minutes?|mins?|hours?|hrs?|h)\b", re.I),
        re.compile(r"\b(\d+(?:\.\d+)?)\s*-?\s*(minutes?|mins?|hours?|hrs?)\s+(?:long|meeting|call)\b", re.I)]
_TIME = re.compile(r"\b(\d{1,2}(:\d{2})?\s*(am|pm|a\.m\.|p\.m\.)|\d{1,2}:\d{2}|at\s+\d{1,2}|noon|midnight|morning|afternoon|evening|tonight)\b", re.I)
_RELATIVE = re.compile(r"\bin\s+\d+(?:\.\d+)?\s*(minutes?|mins?|hours?|hrs?)\b", re.I)
# "at 11", "at 4" (no am/pm): 1-7 means the afternoon (a working-day guess, said back to the user).
_BARE_AT = re.compile(r"\bat\s+(\d{1,2})(?!\s*(?::|\d|am\b|pm\b|a\.m\.|p\.m\.|o'?clock\s*(?:am|pm)))", re.I)
_SOFT = {"morning": 9, "afternoon": 14, "evening": 18, "tonight": 20, "noon": 12, "midnight": 0}


def resolve(text: str, timezone: str, now: datetime) -> dict:
    tz = ZoneInfo(timezone)
    local_now = now.astimezone(tz).replace(tzinfo=None)
    settings = {"PREFER_DATES_FROM": "future", "RELATIVE_BASE": local_now, "RETURN_AS_TIMEZONE_AWARE": False,
                "PREFER_DAY_OF_MONTH": "first"}
    clean = text
    for d in _DUR:
        clean = d.sub(" ", clean)
    relative = bool(_RELATIVE.search(clean))
    clean = re.sub(r"\bnext week\s+(?=(mon|tue|wed|thu|fri|sat|sun))", "next ", clean, flags=re.I)
    clean = re.sub(r"\btonight\b", "today 8 pm", clean, flags=re.I)
    if re.fullmatch(r"\s*(at\s+)?\d{1,2}(:\d{2})?\s*(am|pm|a\.m\.|p\.m\.)?\s*", clean, re.I):
        clean = "today " + clean                    # "at 3": a time today (or tomorrow if it's past), not March
    found = _weekday(clean, local_now)
    if found is None:
        found = dateparser.parse(clean, languages=["en", "hi"], settings=settings)
    if found is None:
        hits = search_dates(clean, languages=["en"], settings=settings) or []
        found = hits[0][1] if hits else None
    if found is None:
        return {"ok": False, "reason": f'Could not find a date or time in "{text}".'}
    time_given = bool(_TIME.search(text)) or relative
    soft = None if relative else next((h for w, h in _SOFT.items() if re.search(rf"\b{w}\b", text, re.I)), None)
    if soft is not None and not re.search(r"\d", text.split(" at ")[-1] if " at " in text else ""):
        found = found.replace(hour=soft, minute=0)
    bare = _BARE_AT.search(text) if not re.search(r"\d\s*(am|pm|a\.m\.|p\.m\.)|\d:\d", text, re.I) else None
    assumed_pm = False
    if bare and 0 <= int(bare.group(1)) <= 23:
        h = int(bare.group(1))
        assumed_pm = 1 <= h <= 7
        found = found.replace(hour=h + 12 if assumed_pm else h, minute=0)
    if not time_given:
        found = found.replace(hour=0, minute=0)
    found = found.replace(second=0, microsecond=0)
    if time_given and found < local_now - timedelta(minutes=1) and not re.search(r"\b(yesterday|ago|last)\b", text, re.I):
        found += timedelta(days=1)                 # "at 3 PM" said at 5 PM means tomorrow
    start = found.replace(tzinfo=tz)
    minutes = None
    for d in _DUR:
        m = d.search(text)
        if m:
            minutes = round(float(m.group(1)) * (60 if m.group(2).lower().startswith("h") else 1))
            break
    end = start + timedelta(minutes=minutes) if minutes else None
    fmt = lambda t: t.strftime("%A %d %B %Y, %H:%M") + f" ({timezone})"  # noqa: E731
    return {"ok": True, "start": start.isoformat(), "end": end.isoformat() if end else None,
            "startLocal": fmt(start) if time_given else start.strftime("%A %d %B %Y") + " (no time given)",
            "endLocal": fmt(end) if end else None, "timeSpecified": time_given, "durationMinutes": minutes, "timezone": timezone,
            **({"note": f"No am/pm given: assumed {int(bare.group(1))} pm. Say so to the user."} if assumed_pm else {})}


_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_WEEKDAY = re.compile(r"\b(?:(next|this|coming)\s+(?:week\s+)?)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thur?s?|fri|sat|sun)\b", re.I)
_EXPLICIT_DATE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\d{1,2}[/-]\d{1,2}", re.I)


def _clock(text: str) -> tuple[int, int] | None:
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b", text, re.I)
    if m:
        h, mi = int(m.group(1)) % 12, int(m.group(2) or 0)
        return (h + (12 if m.group(3).lower().startswith("p") else 0), mi)
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = _BARE_AT.search(text)
    if m and 0 <= int(m.group(1)) <= 23:
        h = int(m.group(1))
        return (h + 12 if 1 <= h <= 7 else h, 0)
    for w, h in _SOFT.items():
        if re.search(rf"\b{w}\b", text, re.I):
            return (h, 0)
    return None


def _weekday(text: str, local_now: datetime) -> datetime | None:
    """"Friday 4pm", "next Tuesday at 9", "this Thursday morning": the next such day (1-7 days ahead,
    or today if it's that day and the time is still to come)."""
    m = _WEEKDAY.search(text)
    if not m or _EXPLICIT_DATE.search(text) or re.search(r"\b(today|tomorrow|yesterday)\b", text, re.I):
        return None
    target = _DAYS.index(m.group(2).lower()[:3])
    ahead = (target - local_now.weekday()) % 7
    clock = _clock(text)
    if ahead == 0 and (m.group(1) or not clock or (clock[0], clock[1]) <= (local_now.hour, local_now.minute)):
        ahead = 7
    day = (local_now + timedelta(days=ahead)).replace(hour=clock[0] if clock else 0, minute=clock[1] if clock else 0)
    return day
