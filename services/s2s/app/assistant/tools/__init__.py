"""The assistant's tools. Each module defines `TOOLS` (a list of Tool); load() registers them all
once. See base.py for what a tool is."""
from __future__ import annotations

from .base import REGISTRY, register

_loaded = False


def load():
    global _loaded
    if _loaded:
        return
    from .. import plans
    from . import bookings, knowledge, meetings, reminders, uber
    for m in (meetings, knowledge, bookings, reminders, uber, plans):
        register(m.TOOLS)
    _loaded = True
    from ..agents import SPECIALISTS
    missing = sorted({n for s in SPECIALISTS for n in s.tools} - set(REGISTRY))
    if missing:
        raise RuntimeError(f"agents name tools that don't exist: {missing}")
