"""Demo web app: the call page plus the token endpoint it joins with.

Served behind the HTTPS proxy (browsers need HTTPS for the microphone), which
also forwards /rtc to LiveKit, so the page connects to wss://<same host>/.
"""
from __future__ import annotations

import asyncio
import contextlib

import aiohttp
import logging
import os
import re
import secrets
import time
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from livekit import api
from pydantic import BaseModel, Field

from . import bnr, db, maintenance, readiness
from .accounts import router as accounts_router
from .languages import HEAR, SPEAK, describe
from .meetings import end_abandoned
from .meetings import router as meetings_router
from . import transcripts
from .transcripts import router as transcripts_router
from .assistant.api import router as assistant_router
from .commerce.api import router as bookings_router
from .reminders.api import router as reminders_router
from .reminders.service import send_due_reminders
from .uber.api import router as uber_router
from .security import SESSION_COOKIE

API_KEY = os.environ["LIVEKIT_API_KEY"]
API_SECRET = os.environ["LIVEKIT_API_SECRET"]
LIVEKIT_HTTP = os.environ.get("LIVEKIT_URL", "http://127.0.0.1:7880").replace("ws", "http", 1)
# The translator joins only rooms that request it by this name (in the callers' tokens), so the
# LiveKit server can be shared with other apps (meetmind's).
AGENT_NAME = os.environ.get("S2S_AGENT_NAME", "indic-s2s")
STATIC = Path(__file__).parent / "static"
log = logging.getLogger("s2s.web")
# The app's own messages (housekeeping, reminders sent, meetings ended, assistant errors) next to uvicorn's.
if not logging.getLogger().handlers:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s:%(name)s: %(message)s")

@contextlib.asynccontextmanager
async def lifespan(_app):
    """Apply database migrations, then keep the translator watchdog running for the app's life."""
    if os.environ.get("DATABASE_URL"):
        await db.migrate()
    watchdog = asyncio.create_task(translator_watchdog())
    try:
        yield
    finally:
        watchdog.cancel()
        await db.dispose()


app = FastAPI(title="MeetMind v2", lifespan=lifespan)
app.include_router(accounts_router)
app.include_router(meetings_router)
app.include_router(transcripts_router)
for r in (assistant_router, bookings_router, reminders_router, uber_router):
    app.include_router(r)
# The original open demo (anyone with a room name joins, no account) only when asked for.
ALLOW_ANONYMOUS_ROOMS = os.environ.get("ALLOW_ANONYMOUS_ROOMS", "0") == "1"

# LiveKit dispatches the translator once, when a room is created. If the worker
# restarts or crashes mid-call, the room is left without one; this puts it back.
WATCHDOG_EVERY_S = 5
MISSING_GRACE_S = 8          # covers the normal join delay of a new room's translator
REDISPATCH_AFTER_S = 30
HOUSEKEEPING_EVERY_S = 300   # expired sessions and confirmations, transcript retention      # give a dispatched translator time to join before retrying


async def translator_watchdog():
    missing_since: dict[str, float] = {}
    dispatched_at: dict[str, float] = {}
    lk = api.LiveKitAPI(LIVEKIT_HTTP, API_KEY, API_SECRET)
    agent_kind = 4           # ParticipantInfo.Kind.AGENT
    last_housekeeping = 0.0
    while True:
        await asyncio.sleep(WATCHDOG_EVERY_S)
        try:
            rooms = (await lk.room.list_rooms(api.ListRoomsRequest())).rooms
            seen = set()
            for r in rooms:
                ps = (await lk.room.list_participants(api.ListParticipantsRequest(room=r.name))).participants
                humans = [p for p in ps if p.kind != agent_kind]
                if not humans or any(p.kind == agent_kind for p in ps):
                    continue
                # only this app's rooms: they asked for the translator when they were created
                if not any(d.agent_name == AGENT_NAME for d in await lk.agent_dispatch.list_dispatch(r.name)):
                    continue
                seen.add(r.name)
                now = time.time()
                first = missing_since.setdefault(r.name, now)
                if now - first >= MISSING_GRACE_S and now - dispatched_at.get(r.name, 0) >= REDISPATCH_AFTER_S:
                    await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=r.name))
                    dispatched_at[r.name] = now
                    log.warning("room %s had no translator for %.0fs; dispatched one", r.name, now - first)
            for name in list(missing_since):
                if name not in seen:
                    missing_since.pop(name)
            for name in list(dispatched_at):
                if name not in {r.name for r in rooms}:
                    dispatched_at.pop(name)
            # Meetings whose room has been empty and closed for a while are over; ended meetings get
            # their summary, and new transcript lines their search index.
            if os.environ.get("DATABASE_URL"):
                async with db.sessionmaker()() as s:
                    n = await end_abandoned(s, {r.name for r in rooms})
                    if n:
                        log.info("ended %d meeting(s) whose room had closed", n)
                async with db.sessionmaker()() as s, aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=5, sock_read=60)) as http:
                    await transcripts.background(s, http)
        except Exception as e:  # noqa: BLE001 - keep watching
            log.warning("translator watchdog: %s", e)
        if os.environ.get("DATABASE_URL"):
            try:                                     # due reminders, even if LiveKit is unreachable
                async with db.sessionmaker()() as s:
                    n = await send_due_reminders(s)
                    if n:
                        log.info("sent %d reminder(s)", n)
            except Exception as e:  # noqa: BLE001
                log.warning("reminders: %s", e)
            if time.time() - last_housekeeping >= HOUSEKEEPING_EVERY_S:
                last_housekeeping = time.time()
                try:
                    async with db.sessionmaker()() as s:
                        await maintenance.run(s)
                except Exception as e:  # noqa: BLE001
                    log.warning("housekeeping: %s", e)


class JoinRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    room: str = Field(min_length=1, max_length=60)
    speak: str
    hear: str
    voice: str = "female"
    # off / standard / enhanced: the browser's own processing; studio: NVIDIA BNR in the agent
    noise: str = "standard"


NOISE_MODES = ("off", "standard", "enhanced", "studio")


@app.get("/api/languages")
async def languages():
    return {**describe(), "noise": {"studio": bnr.available()}}


@app.post("/api/token")
async def token(req: JoinRequest):
    if not ALLOW_ANONYMOUS_ROOMS:
        raise HTTPException(404, "Not found")
    if req.speak not in SPEAK:
        raise HTTPException(400, f"cannot recognise speech in {req.speak!r}")
    if req.hear not in HEAR:
        raise HTTPException(400, f"cannot speak {req.hear!r}")
    room = re.sub(r"[^a-zA-Z0-9_-]", "-", req.room.strip())[:60]
    identity = re.sub(r"[^a-zA-Z0-9_-]", "", req.name.strip().lower())[:20] or "caller"
    identity = f"{identity}-{secrets.token_hex(3)}"
    jwt = (api.AccessToken(API_KEY, API_SECRET)
           .with_identity(identity)
           .with_name(req.name.strip())
           .with_attributes({"speak": req.speak, "hear": req.hear,
                             "voice": "male" if req.voice == "male" else "female",
                             "noise": req.noise if req.noise in NOISE_MODES and (req.noise != "studio" or bnr.available()) else "standard"})
           .with_ttl(timedelta(hours=4))
           .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                                        can_publish_data=True, can_update_own_metadata=True))
           .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]))
           .to_jwt())
    return {"token": jwt, "identity": identity, "room": room}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready():
    """Database, LiveKit, the call agent and the model servers: up or down (503 if a required one is down)."""
    from fastapi.responses import JSONResponse
    r = await readiness.check()
    return JSONResponse(r, status_code=200 if r["status"] == "ok" else 503)


# Pages. The call page is the same for every meeting (/m/<code>); the rest need a session cookie
# to be useful, and redirect to the login page without one (the APIs check the session itself).
def _page(name: str):
    return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})


@app.get("/")
async def home(request: Request):
    if ALLOW_ANONYMOUS_ROOMS and not request.cookies.get(SESSION_COOKIE):
        return _page("index.html")
    return RedirectResponse("/dashboard" if request.cookies.get(SESSION_COOKIE) else "/login")


@app.get("/login")
async def login_page():
    return _page("login.html")


@app.get("/signup")
async def signup_page():
    return _page("login.html")


@app.get("/dashboard")
async def dashboard_page(request: Request):
    return _page("dashboard.html") if request.cookies.get(SESSION_COOKIE) else RedirectResponse("/login")


@app.get("/meetings/{code}")
async def transcript_page(code: str, request: Request):
    if not request.cookies.get(SESSION_COOKIE):
        return RedirectResponse(f"/login?next=/meetings/{code}")
    return _page("transcript.html")


@app.get("/search")
async def search_page(request: Request):
    return _page("search.html") if request.cookies.get(SESSION_COOKIE) else RedirectResponse("/login?next=/search")


def _signed_in_page(name: str, path: str):
    async def page(request: Request):
        return _page(name) if request.cookies.get(SESSION_COOKIE) else RedirectResponse(f"/login?next={path}")
    app.get(path)(page)


for _name, _path in (("assistant.html", "/assistant"), ("bookings.html", "/bookings"), ("reminders.html", "/reminders"),
                     ("settings.html", "/settings")):
    _signed_in_page(_name, _path)


@app.get("/m/{code}")
async def meeting_page(code: str, request: Request):
    if not request.cookies.get(SESSION_COOKIE):
        return RedirectResponse(f"/login?next=/m/{code}")
    return _page("index.html")


app.mount("/", StaticFiles(directory=STATIC, html=False), name="static")
