"""Uber Riders API (v1.2) client. https://developer.uber.com/docs/riders/references/api

- Each user connects their own Uber account (OAuth 2.0 authorization code). While the Uber app is in
  development, only its owner and up to 5 listed developers can connect; that's enough for a demo.
- UBER_ENV=sandbox (the default) sends ride requests to sandbox-api.uber.com: no real car comes and
  nothing is charged, but fares and products are real. Sandbox rides don't move on their own:
  set_sandbox_ride_status does it.
- UBER_ENV=production books real rides. It needs Uber's approval of the app and an explicit setting.

Errors are ToolErrors with a message safe to show (never tokens or raw internals).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from ..assistant.tools.base import ToolError

UBER_SCOPES = ["ride_request.estimate", "ride_request.ride_booking"]
TIMEOUT = aiohttp.ClientTimeout(total=10)


@dataclass
class UberConfig:
    client_id: str
    client_secret: str
    env: str                 # "sandbox" | "production"
    api_base: str
    auth_base: str
    redirect_uri: str


def read_config() -> UberConfig | None:
    env = os.environ
    client_id = env.get("UBER_CLIENT_ID", "").strip()
    client_secret = env.get("UBER_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    mode = "production" if env.get("UBER_ENV", "").strip() == "production" else "sandbox"
    app = env.get("PUBLIC_URL", "").strip().rstrip("/")
    default_api = "https://api.uber.com" if mode == "production" else "https://sandbox-api.uber.com"
    return UberConfig(
        client_id=client_id, client_secret=client_secret, env=mode,
        api_base=(env.get("UBER_API_URL", "").strip() or default_api).rstrip("/"),
        auth_base=(env.get("UBER_AUTH_URL", "").strip() or "https://auth.uber.com").rstrip("/"),
        redirect_uri=env.get("UBER_REDIRECT_URI", "").strip() or f"{app}/api/integrations/uber/callback",
    )


def require_config() -> UberConfig:
    c = read_config()
    if c is None:
        raise ToolError("NOT_CONFIGURED", "Uber isn't set up on this server (UBER_CLIENT_ID and UBER_CLIENT_SECRET).")
    return c


@contextlib.asynccontextmanager
async def http_session(http: aiohttp.ClientSession | None):
    """The caller's aiohttp session, or a short-lived one."""
    if http is not None:
        yield http
        return
    async with aiohttp.ClientSession() as h:
        yield h


@dataclass
class UberTokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scopes: list[str]


async def _uber_error(res: aiohttp.ClientResponse, what: str) -> ToolError:
    """Uber's error body → a message safe to show."""
    code = message = ""
    try:
        b = json.loads(await res.text())
        errs = b.get("errors") or [{}]
        code = b.get("code") or errs[0].get("code") or b.get("error") or ""
        message = b.get("message") or errs[0].get("title") or ""
    except Exception:  # noqa: BLE001 - not JSON
        pass
    detail = f": {message[:200]}" if message else ""
    st = res.status
    if st == 401:
        return ToolError("FORBIDDEN", f"Uber didn't accept this account's login ({what}). Reconnect Uber in Settings.", {"reason": "UBER_AUTH"})
    if st == 403:
        return ToolError("FORBIDDEN", f"Uber refused {what}{detail}. The Uber app may lack the ride permissions, or this account isn't one of its developers.", {"reason": "UBER_FORBIDDEN"})
    if st == 404:
        return ToolError("NOT_FOUND", f"Uber has no such ride or product ({what}).")
    if st == 409 and re.search("surge", code + message, re.I):
        return ToolError("CONFLICT", f"Uber has surge pricing right now{detail}. Surge rides must be confirmed in the Uber app.", {"reason": "UBER_SURGE"})
    if st in (409, 422):
        return ToolError("CONFLICT", f"Uber couldn't do {what}{detail}.", {"reason": code or "UBER_CONFLICT"})
    if st == 429:
        return ToolError("RATE_LIMITED", "Uber is limiting requests right now. Try again in a minute.")
    return ToolError("PROVIDER_UNAVAILABLE", f"Uber didn't answer properly ({what}, HTTP {st}).")


async def _call(http: aiohttp.ClientSession, c: UberConfig, token: str, method: str, path: str,
                body: dict | None = None, what: str = "the request") -> Any:
    headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en_US"}
    try:
        async with http.request(method, f"{c.api_base}{path}", headers=headers, json=body, timeout=TIMEOUT) as res:
            if res.status >= 400:
                raise await _uber_error(res, what)
            if res.status == 204:
                return None
            return json.loads(await res.text() or "null")
    except ToolError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError):
        raise ToolError("PROVIDER_UNAVAILABLE", f"Couldn't reach Uber ({what}).") from None


# ─── OAuth ───────────────────────────────────────────────────────────────
def authorize_url(c: UberConfig, state: str) -> str:
    q = urlencode({"client_id": c.client_id, "response_type": "code", "scope": " ".join(UBER_SCOPES),
                   "redirect_uri": c.redirect_uri, "state": state})
    return f"{c.auth_base}/oauth/v2/authorize?{q}"


async def _token_request(http: aiohttp.ClientSession, c: UberConfig, params: dict[str, str]) -> UberTokens:
    form = {"client_id": c.client_id, "client_secret": c.client_secret, **params}
    try:
        async with http.post(f"{c.auth_base}/oauth/v2/token", data=form, timeout=TIMEOUT) as res:
            # invalid_grant / invalid_client: the code or refresh token was refused, so the user must sign in again.
            if res.status in (400, 401):
                raise ToolError("FORBIDDEN", "Uber refused the sign-in. Connect Uber again in Settings.", {"reason": "UBER_AUTH"})
            if res.status >= 400:
                raise await _uber_error(res, "signing in")
            t = json.loads(await res.text())
    except ToolError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError):
        raise ToolError("PROVIDER_UNAVAILABLE", "Couldn't reach Uber's sign-in server.") from None
    if not t.get("access_token"):
        raise ToolError("PROVIDER_UNAVAILABLE", "Uber's sign-in server returned no access token.")
    return UberTokens(
        access_token=t["access_token"],
        refresh_token=t.get("refresh_token") or params.get("refresh_token"),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=t.get("expires_in") or 3600),
        scopes=[x for x in re.split(r"[\s,]+", t.get("scope") or "") if x],
    )


async def exchange_code(http, c: UberConfig, code: str) -> UberTokens:
    return await _token_request(http, c, {"grant_type": "authorization_code", "redirect_uri": c.redirect_uri, "code": code})


async def refresh_tokens(http, c: UberConfig, refresh_token: str) -> UberTokens:
    return await _token_request(http, c, {"grant_type": "refresh_token", "refresh_token": refresh_token})


async def revoke_token(http, c: UberConfig, token: str):
    with contextlib.suppress(Exception):
        form = {"client_id": c.client_id, "client_secret": c.client_secret, "token": token}
        async with http.post(f"{c.auth_base}/oauth/v2/revoke", data=form, timeout=TIMEOUT):
            pass


# ─── Rides ───────────────────────────────────────────────────────────────
# Places are (lat, lng) tuples.
def _coords(start, end=None) -> str:
    q = {"start_latitude": start[0], "start_longitude": start[1]}
    if end:
        q |= {"end_latitude": end[0], "end_longitude": end[1]}
    return urlencode(q)


async def get_products(http, c, token, at) -> list[dict]:
    r = await _call(http, c, token, "GET", f"/v1.2/products?{urlencode({'latitude': at[0], 'longitude': at[1]})}", what="listing ride options")
    return (r or {}).get("products") or []


async def get_price_estimates(http, c, token, start, end) -> list[dict]:
    r = await _call(http, c, token, "GET", f"/v1.2/estimates/price?{_coords(start, end)}", what="estimating fares")
    return (r or {}).get("prices") or []


async def get_time_estimates(http, c, token, start) -> list[dict]:
    r = await _call(http, c, token, "GET", f"/v1.2/estimates/time?{_coords(start)}", what="estimating pickup times")
    return (r or {}).get("times") or []


def _ride_body(product_id: str, start, end) -> dict:
    return {"product_id": product_id, "start_latitude": start[0], "start_longitude": start[1],
            "end_latitude": end[0], "end_longitude": end[1]}


async def get_fare_estimate(http, c, token, product_id, start, end) -> dict:
    """The upfront fare for one product (fare_id is needed to book, and expires after a couple of minutes)."""
    return await _call(http, c, token, "POST", "/v1.2/requests/estimate", _ride_body(product_id, start, end), "getting the fare") or {}


async def create_ride(http, c, token, product_id, fare_id, start, end) -> dict:
    body = _ride_body(product_id, start, end) | ({"fare_id": fare_id} if fare_id else {})
    return await _call(http, c, token, "POST", "/v1.2/requests", body, "booking the ride") or {}


async def get_ride(http, c, token, request_id: str) -> dict:
    return await _call(http, c, token, "GET", f"/v1.2/requests/{quote(request_id, safe='')}", what="checking the ride") or {}


async def cancel_ride(http, c, token, request_id: str):
    await _call(http, c, token, "DELETE", f"/v1.2/requests/{quote(request_id, safe='')}", what="cancelling the ride")


# Sandbox ride stages, in the order Uber requires (driver_canceled may be set at any time).
SANDBOX_STAGES = ["processing", "accepted", "arriving", "in_progress", "completed"]


async def set_sandbox_ride_status(http, c, token, request_id: str, status: str):
    if c.env != "sandbox":
        raise ToolError("FORBIDDEN", "Rides can only be moved along by hand in Uber test mode.")
    await _call(http, c, token, "PUT", f"/v1/sandbox/requests/{quote(request_id, safe='')}", {"status": status}, "moving the test ride along")


# Statuses after which a ride is over.
RIDE_FINAL = {"completed", "rider_canceled", "driver_canceled", "no_drivers_available"}
RIDE_LABEL = {
    "processing": "Finding a driver",
    "no_drivers_available": "No drivers available",
    "accepted": "Driver on the way",
    "arriving": "Driver arriving",
    "in_progress": "On the trip",
    "driver_canceled": "Cancelled by the driver",
    "rider_canceled": "Cancelled",
    "completed": "Completed",
}

