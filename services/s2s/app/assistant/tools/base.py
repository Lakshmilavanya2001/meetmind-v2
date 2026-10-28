"""What a tool is, and the registry.

A tool has a name, a description and a pydantic argument model (the JSON schema the model sees),
and a kind:
- "read": runs at once (looking things up).
- "write": runs at once (harmless changes, e.g. moving a test ride along).
- "sensitive": changes something that matters (scheduling, inviting, booking, reminders,
  cancelling). Its `preflight` checks permissions and returns one exact sentence of what will
  happen; the user is shown it with Confirm / Cancel, and only then does `run` happen.
- "plan": the Planner's proposal (handled by the engine).

Tools raise ToolError for anything the user or the model should hear ("Nobody called Priya");
any other exception is reported as an internal error and nothing is claimed.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal

import aiohttp
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..guards import HistoryEntry, SearchResolution

ToolKind = Literal["read", "write", "sensitive", "plan"]


class ToolError(Exception):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


@dataclass
class ToolContext:
    """Everything a tool may use: the database session, who is asking, the conversation."""
    s: AsyncSession
    user_id: uuid.UUID
    org_id: uuid.UUID
    user_name: str
    user_email: str
    timezone: str
    role: str                                   # the user's role in the organisation
    conversation_id: uuid.UUID
    meeting_code: str | None                    # the call this chat is attached to ("this meeting")
    now: datetime
    history: list[HistoryEntry] = field(default_factory=list)
    http: aiohttp.ClientSession | None = None
    agent: str | None = None
    call_memory: Any = None                     # in a call: what was just said (assistant.call.CallMemory)


@dataclass
class ToolOutput:
    data: Any                                   # what the model sees (JSON)
    summary: str                                # one line for the UI's tool chip
    link: str | None = None                     # a page in the app (or an Uber link)
    resolution: SearchResolution | None = None  # search_users: lets the guards know about ambiguity
    citations: list | None = None


@dataclass
class Tool:
    name: str
    kind: ToolKind
    description: str
    args: type[BaseModel]
    run: Callable[[ToolContext, Any], Awaitable[ToolOutput]]
    label: Callable[[Any], str] = lambda a: ""
    preflight: Callable[[ToolContext, Any], Awaitable[str]] | None = None

    def spec(self) -> dict:
        schema = self.args.model_json_schema()
        schema.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": schema}}


REGISTRY: dict[str, Tool] = {}


def register(tools: list[Tool]):
    for t in tools:
        if t.name in REGISTRY:
            raise RuntimeError(f"tool {t.name} registered twice")
        if t.kind == "sensitive" and t.preflight is None:
            raise RuntimeError(f"sensitive tool {t.name} needs a preflight")
        REGISTRY[t.name] = t


def specs(names: list[str]) -> list[dict]:
    return [REGISTRY[n].spec() for n in names if n in REGISTRY]
