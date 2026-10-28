"""Place name → coordinates, for Uber fares and to pre-fill the "Open in Uber" link (Uber expects coordinates).

Off unless GEOCODER_URL is set. It speaks the Nominatim search API, e.g. https://nominatim.openstreetmap.org
(public: at most 1 request a second, and a contact in the User-Agent: set GEOCODER_CONTACT) or a self-hosted
copy. GEOCODER_COUNTRIES (e.g. "in") narrows results to those countries. Results are cached; a slow or failed
lookup just gives no coordinates.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

import aiohttp

from .provider import http_session

TIMEOUT = aiohttp.ClientTimeout(total=2.5)
MIN_GAP_S = 1.1                  # the public server's limit is 1 request a second (tests set 0)
CACHE_MAX = 500
CACHE_S = 24 * 3600


@dataclass
class GeoPoint:
    lat: float
    lng: float
    label: str


_cache: dict[str, tuple[float, GeoPoint | None]] = {}
_lock: asyncio.Lock | None = None
_last_at = 0.0


def read_config() -> dict | None:
    url = os.environ.get("GEOCODER_URL", "").strip().rstrip("/")
    if not url:
        return None
    return {"url": url, "contact": os.environ.get("GEOCODER_CONTACT", "").strip(),
            "countries": os.environ.get("GEOCODER_COUNTRIES", "").strip()}


async def geocode(place: str, http: aiohttp.ClientSession | None = None) -> GeoPoint | None:
    global _lock, _last_at
    cfg = read_config()
    key = place.strip().lower()
    if not cfg or len(key) < 3:
        return None
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]

    if _lock is None:
        _lock = asyncio.Lock()
    # One request at a time, spaced out, so the public server's rate limit is respected.
    async with _lock:
        hit = _cache.get(key)                         # someone else may have just looked it up
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        wait = _last_at + MIN_GAP_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_at = time.monotonic()
        point = await _lookup(cfg, place.strip(), http)

    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.monotonic(), point)
    return point


async def _lookup(cfg: dict, place: str, http) -> GeoPoint | None:
    q = {"q": place, "format": "jsonv2", "limit": "1"}
    if cfg["countries"]:
        q["countrycodes"] = cfg["countries"]
    headers = {"User-Agent": "MeetMind/1.0" + (f" ({cfg['contact']})" if cfg["contact"] else ""), "Accept": "application/json"}
    try:
        async with http_session(http) as h, h.get(f"{cfg['url']}/search", params=q, headers=headers, timeout=TIMEOUT) as res:
            if res.status != 200:
                return None
            rows = await res.json(content_type=None)
        r = rows[0] if isinstance(rows, list) and rows else None
        if not r:
            return None
        lat, lng = float(r["lat"]), float(r["lon"])
        return GeoPoint(lat, lng, r.get("display_name") or place)
    except Exception:  # noqa: BLE001 - slow, down, or not what we expected: no coordinates
        return None


def reset_cache():
    """Tests only."""
    global _last_at, _lock
    _cache.clear()
    _last_at = 0.0
    _lock = None
