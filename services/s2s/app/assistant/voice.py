""""Hey Mind" in a meeting, through the assistant's agents (voice mode).

Each caller has one assistant conversation per meeting (it also shows on their assistant page,
attached to the meeting). Replies are one or two spoken sentences. An action that needs confirmation
is read out; the caller answers "yes" or "no" (no wake phrase needed for a short while), and only
then is it done. Plans can't be approved by voice: they wait on the assistant page.

Rooms without accounts (the open demo) keep the quick call-only answer (call.answer).
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

import aiohttp
from sqlalchemy import text

from .. import db, transcript_store
from . import call, engine, llm
from .tools import load
from .tools.base import ToolError

log = logging.getLogger("s2s.assistant")
YES = re.compile(r"^\W*(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|confirm(ed)?|do it|please do|correct|right|haan?|ha|हाँ|हां|जी हाँ|जी|ठीक है|कर दो|कीजिए)(?!\w)", re.I)
# (?!\w), not \b: a Devanagari word can end in a vowel sign (हाँ, नहीं), where \b never matches.
NO = re.compile(r"^\W*(no|nope|nah|cancel|don'?t|do not|stop|never ?mind|nahi|nahin|नहीं|मत|रहने दो|रद्द)(?!\w)", re.I)
SAY_YES = {"en": "Say yes to go ahead, or no to cancel.", "hi": "आगे बढ़ने के लिए हाँ कहें, या रद्द करने के लिए ना।"}


@dataclass
class Reply:
    text: str
    expects_answer: bool = False        # keep listening for a short while without the wake phrase


class VoiceAssistant:
    """One per call: who is who, and each caller's conversation."""

    def __init__(self, room_name: str):
        self.room_name = room_name
        self.conversations: dict[str, str] = {}

    async def _asker(self, s, identity: str):
        try:
            uid = uuid.UUID(identity)
        except ValueError:
            return None, None
        row = (await s.execute(text(
            "SELECT u.id, u.display_name, u.email, u.timezone, u.preferred_language, ms.role, o.id AS org_id, o.name AS org_name, m.join_code "
            "FROM meetings m JOIN meeting_participants p ON p.meeting_id = m.id AND p.user_id = :u AND p.removed_at IS NULL "
            "JOIN users u ON u.id = p.user_id JOIN memberships ms ON ms.user_id = u.id AND ms.organization_id = m.organization_id "
            "JOIN organizations o ON o.id = m.organization_id WHERE m.room_name = :r AND u.is_active"),
            {"u": uid, "r": self.room_name})).mappings().first()
        if row is None:
            return None, None
        return engine.Asker(row["id"], row["org_id"], row["display_name"], row["email"], row["timezone"], row["role"], row["org_name"],
                            row["preferred_language"]), row["join_code"]

    async def handle(self, http: aiohttp.ClientSession, identity: str, name: str, lang: str, question: str, people: list[str],
                     memory: call.CallMemory) -> Reply:
        if not transcript_store.enabled() or not llm.configured():
            return Reply(await call.answer(http, question, name, lang, people, memory))
        load()
        async with db.sessionmaker()() as s:
            a, code = await self._asker(s, identity)
            if a is None:                                 # not an account (open demo room)
                return Reply(await call.answer(http, question, name, lang, people, memory))
            cid = self.conversations.get(identity)
            if cid:
                pending = (await s.execute(text("SELECT id FROM agent_actions WHERE conversation_id = :c AND status = 'PENDING_CONFIRMATION' "
                                                "AND created_at > now() - interval '15 minutes' ORDER BY created_at DESC LIMIT 1"),
                                           {"c": cid})).scalar()
                if pending and (YES.search(question) or NO.search(question)):
                    decision = "confirm" if YES.search(question) and not NO.search(question) else "reject"
                    try:
                        r = await engine.decide_action(s, http, a, pending, decision)
                        said = r["message"]["content"].replace("✓ Done:", "Done:")
                    except ToolError as e:
                        said = e.message
                    return Reply(llm.to_speech(said))
            texts, waiting = [], False
            async for ev in engine.run_turn(s, http, a, question, cid, code, channel="voice", language=lang, call_memory=memory):
                if ev["type"] == "conversation":
                    self.conversations[identity] = ev["id"]
                elif ev["type"] == "text":
                    texts.append(ev["text"])
                elif ev["type"] == "guard":
                    texts.append(ev["message"])
                elif ev["type"] == "confirmation":
                    waiting = True
            said = llm.to_speech(" ".join(texts[-2:]) if texts else "Sorry, I didn't get that.")
            if waiting:
                said = f"{said} {SAY_YES.get(lang, SAY_YES['en'])}"
            return Reply(said, expects_answer=waiting or said.rstrip().endswith("?"))
