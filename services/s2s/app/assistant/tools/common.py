"""Helpers shared by tools."""
from __future__ import annotations

from sqlalchemy import text

from ..guards import place_is_grounded
from .base import ToolContext, ToolError


async def conversation_text(t: ToolContext) -> str:
    """What the user said and what tools returned in this conversation (newest 80 messages): the only
    places an address or a pickup point may come from."""
    rows = (await t.s.execute(text(
        "SELECT content FROM agent_messages WHERE conversation_id = :c AND role IN ('USER', 'TOOL') ORDER BY id DESC LIMIT 80"),
        {"c": t.conversation_id})).scalars().all()
    return "\n".join(rows)


async def assert_places_given(t: ToolContext, places: list[tuple[str, str]]):
    """Each (label, value) must be what the user or a tool said, never invented (meetmind's assertPlacesGiven)."""
    said = await conversation_text(t)
    for label, value in places:
        if not place_is_grounded(value, said):
            raise ToolError("VALIDATION", f'The {label} "{value}" isn\'t what the user said. Use the place exactly as the user or a tool '
                                          'gave it (an area name like "Indiranagar" is fine), or ask the user if they gave none. '
                                          "Never invent or embellish addresses.", {"reason": "UNGROUNDED_PLACE"})
