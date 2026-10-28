"""The assistant's HTTP routes: chat (streamed as one JSON event per line), conversations, and the
user's Confirm / Cancel on actions and Approve / Reject on plans."""
from __future__ import annotations

import json
import logging
import os
import uuid

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db
from ..accounts import Ctx, current, get_db
from ..security import rate_limit
from . import engine, llm, plans
from .tools import load
from .tools.base import ToolError

router = APIRouter()
log = logging.getLogger("s2s.assistant")
# The assistant's own voice on the page (as in calls: S2S_ASSISTANT_VOICE, a Magpie voice or female/male).
ASSISTANT_VOICE = os.environ.get("S2S_ASSISTANT_VOICE") or "female"
_STATUS = {"VALIDATION": 400, "NOT_FOUND": 404, "CONFLICT": 409, "FORBIDDEN": 403, "NOT_CONFIGURED": 503}


def asker(ctx: Ctx) -> engine.Asker:
    u = ctx.user
    return engine.Asker(u.id, ctx.org.id, u.display_name, u.email, u.timezone, ctx.role, ctx.org.name, u.preferred_language)


def http_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=5, sock_read=90))


def _uuid(v: str) -> str:
    try:
        return str(uuid.UUID(v))
    except ValueError:
        raise HTTPException(404, "Not found.") from None


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversationId: str | None = None
    meetingCode: str | None = Field(default=None, max_length=40)
    voice: bool = False                     # spoken: short replies to be read aloud; "yes"/"no" decide a pending action
    language: str | None = Field(default=None, pattern="^(en|hi)$")


async def _spoken_decision(s, http, a, cid, message: str) -> dict | None:
    """In voice mode, "yes" / "no" right after a confirmation question decides the pending action."""
    from .voice import NO, YES
    if not cid or not (YES.search(message) or NO.search(message)):
        return None
    pending = (await s.execute(text("SELECT id FROM agent_actions WHERE conversation_id = :c AND user_id = :u AND status = 'PENDING_CONFIRMATION' "
                                    "AND created_at > now() - interval '15 minutes' ORDER BY created_at DESC LIMIT 1"),
                               {"c": cid, "u": a.user_id})).scalar()
    if not pending:
        return None
    await engine._add(s, cid, "USER", message)
    decision = "confirm" if YES.search(message) and not NO.search(message) else "reject"
    try:
        r = await engine.decide_action(s, http, a, pending, decision)
        return {"type": "decided", "actionId": str(pending), "decision": decision, "ok": r["ok"],
                "text": r["message"]["content"], "link": r["message"].get("link")}
    except ToolError as e:
        return {"type": "decided", "actionId": str(pending), "decision": decision, "ok": False, "text": e.message}


@router.post("/api/assistant/chat")
async def chat(body: ChatIn, ctx: Ctx = Depends(current)):
    rate_limit(f"assistant:{ctx.user.id}", 30, 600)
    if not llm.configured():
        raise HTTPException(503, "The assistant isn't set up on this server.")
    load()
    a = asker(ctx)
    cid = _uuid(body.conversationId) if body.conversationId else None

    async def events():
        # Its own database session: the request's one closes when the response starts.
        async with db.sessionmaker()() as s, http_session() as http:
            try:
                decided = await _spoken_decision(s, http, a, cid, body.message) if body.voice else None
                if decided:
                    yield json.dumps({"type": "conversation", "id": cid}) + "\n"
                    yield json.dumps(decided, default=str, ensure_ascii=False) + "\n"
                    yield json.dumps({"type": "done", "conversationId": cid}) + "\n"
                    return
                async for ev in engine.run_turn(s, http, a, body.message, cid, body.meetingCode,
                                                channel="voice" if body.voice else "text", language=body.language):
                    yield json.dumps(ev, default=str, ensure_ascii=False) + "\n"
            except ToolError as e:
                yield json.dumps({"type": "error", "message": e.message}) + "\n"
            except Exception:  # noqa: BLE001
                log.exception("assistant turn failed")
                yield json.dumps({"type": "error", "message": "The assistant couldn't finish that. Nothing more was changed."}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"cache-control": "no-store", "x-accel-buffering": "no"})


@router.get("/api/assistant/conversations")
async def conversations(ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    rows = (await s.execute(text("SELECT c.id, c.title, c.updated_at, m.join_code, m.title AS meeting_title FROM agent_conversations c "
                                 "LEFT JOIN meetings m ON m.id = c.meeting_id WHERE c.user_id = :u AND c.organization_id = :o "
                                 "ORDER BY c.updated_at DESC LIMIT 50"), {"u": ctx.user.id, "o": ctx.org.id})).mappings().all()
    return {"conversations": [{"id": str(r["id"]), "title": r["title"], "updatedAt": r["updated_at"].isoformat(),
                               "meeting": {"code": r["join_code"], "title": r["meeting_title"]} if r["join_code"] else None} for r in rows]}


@router.get("/api/assistant/conversations/{cid}")
async def conversation(cid: str, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    cid = _uuid(cid)
    conv = (await s.execute(text("SELECT c.id, c.title, m.join_code, m.title AS meeting_title FROM agent_conversations c LEFT JOIN meetings m "
                                 "ON m.id = c.meeting_id WHERE c.id = :c AND c.user_id = :u AND c.organization_id = :o"),
                            {"c": cid, "u": ctx.user.id, "o": ctx.org.id})).mappings().first()
    if conv is None:
        raise HTTPException(404, "Conversation not found.")
    rows = (await s.execute(text("SELECT * FROM agent_messages WHERE conversation_id = :c ORDER BY id"), {"c": cid})).mappings().all()
    pending = {str(r["id"]) for r in (await s.execute(text("SELECT id FROM agent_actions WHERE conversation_id = :c AND status = 'PENDING_CONFIRMATION' "
                                                            "AND created_at > now() - interval '15 minutes'"), {"c": cid})).mappings()}
    plan_rows = {str(p["id"]): plans.plan_view(dict(p)) for p in
                 (await s.execute(text("SELECT * FROM agent_plans WHERE conversation_id = :c"), {"c": cid})).mappings()}
    items = []
    for r in rows:
        meta = r["metadata"] or {}
        if r["role"] == "USER":
            items.append({"kind": "user", "text": r["content"]})
        elif r["role"] == "ASSISTANT" and r["content"].strip():
            items.append({"kind": "assistant", "text": r["content"], "agent": meta.get("agent"), "link": meta.get("link"), "guard": bool(meta.get("guard"))})
        elif r["role"] == "SYSTEM" and meta.get("handoff"):
            items.append({"kind": "handoff", **meta["handoff"]})
        elif r["role"] == "TOOL":
            status = meta.get("status")
            if status == "pending_confirmation" and meta.get("actionId") not in pending:
                status = "expired"
            items.append({"kind": "tool", "name": r["tool_name"], "label": meta.get("label"), "ok": meta.get("ok"), "summary": meta.get("summary"),
                          "link": meta.get("link"), "citations": meta.get("citations"), "status": status, "decision": meta.get("decision"),
                          "actionId": meta.get("actionId"),
                          "candidates": (meta.get("resolution") or {}).get("candidates") if (meta.get("resolution") or {}).get("status") == "ambiguous" else None})
            if meta.get("planId") in plan_rows:
                items.append({"kind": "plan", "plan": plan_rows[meta["planId"]]})
    revisions = [p for pid, p in plan_rows.items() if p["revision"] > 0 and not any(i.get("plan", {}).get("id") == pid for i in items)]
    items += [{"kind": "plan", "plan": p} for p in revisions]
    return {"conversation": {"id": cid, "title": conv["title"],
                             "meeting": {"code": conv["join_code"], "title": conv["meeting_title"]} if conv["join_code"] else None},
            "items": items}


@router.delete("/api/assistant/conversations/{cid}")
async def delete_conversation(cid: str, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    r = await s.execute(text("DELETE FROM agent_conversations WHERE id = :c AND user_id = :u AND organization_id = :o"),
                        {"c": _uuid(cid), "u": ctx.user.id, "o": ctx.org.id})
    await s.commit()
    if r.rowcount == 0:
        raise HTTPException(404, "Conversation not found.")
    return {"ok": True}


class ActionIn(BaseModel):
    decision: str = Field(pattern="^(confirm|reject)$")


@router.post("/api/assistant/actions/{action_id}")
async def decide_action(action_id: str, body: ActionIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    rate_limit(f"assistant-decide:{ctx.user.id}", 60, 600)
    load()
    async with http_session() as http:
        try:
            return await engine.decide_action(s, http, asker(ctx), _uuid(action_id), body.decision)
        except ToolError as e:
            raise HTTPException(_STATUS.get(e.code, 400), e.message) from None


class PlanIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")


@router.post("/api/assistant/plans/{plan_id}")
async def decide_plan(plan_id: str, body: PlanIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    rate_limit(f"assistant-decide:{ctx.user.id}", 60, 600)
    load()
    async with http_session() as http:
        try:
            return await plans.decide_plan(s, http, asker(ctx), _uuid(plan_id), body.decision)
        except ToolError as e:
            raise HTTPException(_STATUS.get(e.code, 400), e.message) from None


# ------------------------------------------------------------------ voice: speech in, speech out
MAX_SPEECH_BYTES = 44 + 16000 * 2 * 30          # 30 s of 16 kHz mono 16-bit audio


@router.post("/api/assistant/transcribe")
async def transcribe(request: Request, lang: str = "en", ctx: Ctx = Depends(current)):
    """A spoken message (16 kHz mono 16-bit WAV, as the page records it) → its text, from Parakeet."""
    import io
    import wave

    import numpy as np

    from .. import asr
    rate_limit(f"assistant-speech:{ctx.user.id}", 60, 600)
    if lang not in ("en", "hi"):
        raise HTTPException(400, "Speak English or Hindi.")
    body = await request.body()
    if len(body) > MAX_SPEECH_BYTES:
        raise HTTPException(413, "That recording is too long (30 seconds at most).")
    try:
        with wave.open(io.BytesIO(body)) as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
                raise HTTPException(400, "Send 16 kHz mono 16-bit audio.")
            pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    except (wave.Error, EOFError):
        raise HTTPException(400, "That isn't a WAV recording.") from None
    if len(pcm) < 16000 * 0.3:
        return {"text": "", "ms": 0}
    try:
        async with http_session() as http:
            text_, ms = await asr.transcribe(http, pcm, lang)
    except Exception as e:  # noqa: BLE001
        log.warning("assistant transcription failed: %s", e)
        raise HTTPException(502, "Speech recognition isn't available right now.") from None
    return {"text": text_, "ms": round(ms)}


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    lang: str | None = Field(default=None, pattern="^(en|hi)$")


@router.post("/api/assistant/speak")
async def speak(body: SpeakIn, ctx: Ctx = Depends(current)):
    """The assistant's reply read aloud by Magpie (WAV), in English or Hindi (from its script if not given)."""
    import re

    import aiohttp as _aiohttp

    from .. import voices
    rate_limit(f"assistant-speech:{ctx.user.id}", 60, 600)
    if not voices.MAGPIE_TTS_URL:
        raise HTTPException(503, "Spoken replies aren't set up on this server.")
    said = llm.to_speech(body.text)[:1500]
    if not said:
        raise HTTPException(400, "Nothing to say.")
    lang = body.lang or ("hi" if re.search(r"[\u0900-\u097F]", said) else "en")
    by_lang = await voices.catalog()
    form = _aiohttp.FormData()
    form.add_field("language", voices.MAGPIE_LOCALE[lang])
    form.add_field("text", said)
    form.add_field("voice", voices.magpie_name(by_lang, lang, ASSISTANT_VOICE))
    form.add_field("sample_rate_hz", "22050")
    try:
        async with http_session() as http, http.post(f"{voices.MAGPIE_TTS_URL}/audio/synthesize", data=form) as r:
            if r.status != 200:
                raise HTTPException(502, "Spoken replies aren't available right now.")
            audio = await r.read()
    except _aiohttp.ClientError:
        raise HTTPException(502, "Spoken replies aren't available right now.") from None
    return Response(audio, media_type="audio/wav", headers={"cache-control": "no-store"})
