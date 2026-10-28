"""Safety guards (ported from meetmind's src/server/ai/guards.ts).

- People: the model may not act on someone returned as one of several matches for a name until the
  user picks them (by email, id, or a distinctive word of their title or team).
- Emails: an address may only come from what the user typed.
- Places: an address or pickup point must come from the conversation (two thirds of its words).
- Claims: "I've scheduled…" with no successful scheduling tool this turn gets a correction.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Candidate:
    id: str
    displayName: str
    email: str
    title: str | None = None
    department: str | None = None


@dataclass
class SearchResolution:
    status: Literal["resolved", "ambiguous", "not_found"]
    candidates: list[Candidate] = field(default_factory=list)


@dataclass
class HistoryEntry:
    role: Literal["USER", "ASSISTANT", "TOOL"]
    content: str = ""
    tool_name: str = ""
    resolution: SearchResolution | None = None


_STOP = {"the", "and", "for", "lead", "head", "team", "senior", "junior", "one", "who"}


def _words(s: str | None) -> set[str]:
    return {w for w in re.split(r"[^\w]+", (s or "").lower()) if len(w) >= 3 and w not in _STOP}


def _picked_by_distinctive_word(text: str, c: Candidate, group: list[Candidate]) -> bool:
    said = _words(text)
    own = _words(c.title) | _words(c.department)
    others = set().union(*[(_words(g.title) | _words(g.department)) for g in group if g.id != c.id]) if len(group) > 1 else set()
    distinctive = [w for w in own if w not in others and w in said]
    if not distinctive:
        return False
    return not any(any(w in said and w not in own for w in (_words(g.title) | _words(g.department))) for g in group if g.id != c.id)


def find_unconfirmed_people(history: list[HistoryEntry], user_ids: list[str]) -> list[tuple[str, list[Candidate]]]:
    state: dict[str, tuple[bool, list[Candidate]]] = {}
    for h in history:
        if h.role == "TOOL" and h.tool_name == "search_users" and h.resolution:
            if h.resolution.status == "resolved":
                for c in h.resolution.candidates:
                    state[c.id] = (True, h.resolution.candidates)
            elif h.resolution.status == "ambiguous":
                for c in h.resolution.candidates:
                    if not state.get(c.id, (False,))[0]:
                        state[c.id] = (False, h.resolution.candidates)
        elif h.role == "USER":
            text = h.content.lower()
            for cid, (ok, group) in list(state.items()):
                c = next((g for g in group if g.id == cid), None)
                if not ok and c and (c.email.lower() in text or cid.lower() in text or _picked_by_distinctive_word(text, c, group)):
                    state[cid] = (True, group)
    return [(uid, state[uid][1]) for uid in user_ids if uid in state and not state[uid][0]]


def email_provided_by_user(history: list[HistoryEntry], email: str) -> bool:
    e = email.strip().lower()
    return any(h.role == "USER" and e in h.content.lower() for h in history)


_CLAIMS = [
    (r"(invited|added|sent (?:the |an? )?(?:invit|link))", ["invite_participants", "create_meeting"], "invitation"),
    (r"(scheduled|booked|set up|created)", ["create_meeting", "start_instant_meeting", "book_cab", "book_uber", "reserve_table",
                                            "order_food", "create_reminder", "book_movie_tickets"], "creation or booking"),
    (r"(rescheduled|moved)", ["reschedule_meeting"], "rescheduling"),
    (r"(cancell?ed)", ["cancel_meeting", "cancel_booking", "cancel_reminder"], "cancellation"),
    (r"(ordered|placed (?:the |an |your )?order)", ["order_food"], "food order"),
    (r"(reserved)", ["reserve_table"], "table reservation"),
    (r"(set (?:a |the |your )?reminder|added (?:a |the )?reminder)", ["create_reminder"], "reminder"),
    (r"(removed|kicked)", ["remove_participant"], "removal"),
    (r"(ended)", ["end_meeting"], "ending the meeting"),
]
_COMPLETION = r"\b(?:i(?:'ve| have)?|we(?:'ve| have)?|has been|have been|was|were|is now|are now|successfully|done[,:]?|all set[,:]?)\s+(?:[a-z]+\s+){0,3}?"


def find_unbacked_claims(text: str, succeeded_tools: list[str]) -> list[str]:
    lower = text.lower()
    return [label for pat, tools, label in _CLAIMS
            if re.search(_COMPLETION + pat + r"\b", lower) and not any(t in succeeded_tools for t in tools)]


_PLACE_STOP = {"the", "and", "near", "for", "from", "with", "at", "to", "in", "of", "my", "our", "your"}


def _place_tokens(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s.lower())
    s = re.sub(r"'s\b", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return [w for w in s.split() if (len(w) >= 3 or w.isdigit()) and w not in _PLACE_STOP]


def place_is_grounded(place: str, conversation_text: str) -> bool:
    words = _place_tokens(place)
    if not words:
        return False
    known = set(_place_tokens(conversation_text))
    return sum(w in known for w in words) >= -(-len(words) * 2 // 3)
