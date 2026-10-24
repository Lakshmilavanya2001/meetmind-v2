"""Demo web app: the call page plus the token endpoint it joins with.

Served behind the HTTPS proxy (browsers need HTTPS for the microphone), which
also forwards /rtc to LiveKit, so the page connects to wss://<same host>/.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from livekit import api
from pydantic import BaseModel, Field

from .languages import HEAR, SPEAK, describe

API_KEY = os.environ["LIVEKIT_API_KEY"]
API_SECRET = os.environ["LIVEKIT_API_SECRET"]
LIVEKIT_HTTP = os.environ.get("LIVEKIT_URL", "http://127.0.0.1:7880").replace("ws", "http", 1)
STATIC = Path(__file__).parent / "static"
log = logging.getLogger("s2s.web")

app = FastAPI(title="Indic call translator demo")

# LiveKit dispatches the translator once, when a room is created. If the worker
# restarts or crashes mid-call, the room is left without one; this puts it back.
WATCHDOG_EVERY_S = 5
MISSING_GRACE_S = 8          # covers the normal join delay of a new room's translator
REDISPATCH_AFTER_S = 30      # give a dispatched translator time to join before retrying


async def translator_watchdog():
    missing_since: dict[str, float] = {}
    dispatched_at: dict[str, float] = {}
    lk = api.LiveKitAPI(LIVEKIT_HTTP, API_KEY, API_SECRET)
    agent_kind = 4           # ParticipantInfo.Kind.AGENT
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
                seen.add(r.name)
                now = time.time()
                first = missing_since.setdefault(r.name, now)
                if now - first >= MISSING_GRACE_S and now - dispatched_at.get(r.name, 0) >= REDISPATCH_AFTER_S:
                    await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(agent_name="", room=r.name))
                    dispatched_at[r.name] = now
                    log.warning("room %s had no translator for %.0fs; dispatched one", r.name, now - first)
            for name in list(missing_since):
                if name not in seen:
                    missing_since.pop(name)
            for name in list(dispatched_at):
                if name not in {r.name for r in rooms}:
                    dispatched_at.pop(name)
        except Exception as e:  # noqa: BLE001 - keep watching
            log.warning("translator watchdog: %s", e)


@app.on_event("startup")
async def start_watchdog():
    app.state.watchdog = asyncio.create_task(translator_watchdog())   # keep a reference


class JoinRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    room: str = Field(min_length=1, max_length=60)
    speak: str
    hear: str
    voice: str = "female"


@app.get("/api/languages")
async def languages():
    return describe()


@app.post("/api/token")
async def token(req: JoinRequest):
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
                             "voice": "male" if req.voice == "male" else "female"})
           .with_ttl(timedelta(hours=4))
           .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                                        can_publish_data=True, can_update_own_metadata=True))
           .to_jwt())
    return {"token": jwt, "identity": identity, "room": room}


@app.get("/health")
async def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
