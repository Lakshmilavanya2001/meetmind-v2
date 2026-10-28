"""Magpie's voices and tones, and which one a listener hears a speaker in (ported from meetmind's magpie.ts).

A speaker chooses a voice, optionally with a tone ("Sofia" or "Sofia.Calm"); everyone hears their
translated speech in it. Each listener's language may not have that tone, or that voice: then they
hear the same voice without the tone, else the language's default. The catalogue comes from Magpie's
GET /audio/list_voices (names like "Magpie-Multilingual.EN-US.Sofia.Calm"), cached for 10 minutes.

The original choices "female" and "male" still work: they mean Louise and Leo.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

import aiohttp

log = logging.getLogger("s2s.voices")
MAGPIE_TTS_URL = os.environ.get("MAGPIE_TTS_URL", "").rstrip("/")
MAGPIE_LOCALE = {"en": "en-US", "hi": "hi-IN"}                    # the call languages Magpie voices
LEGACY = {"female": os.environ.get("MAGPIE_FEMALE_VOICE", "Louise"), "male": os.environ.get("MAGPIE_MALE_VOICE", "Leo")}
DEFAULT_VOICE = LEGACY["female"]
VOICE_ID = re.compile(r"^[A-Za-z]{2,20}(\.[A-Za-z]{2,24})?$")
_NAME = re.compile(r"^Magpie-Multilingual\.([A-Z]{2}-[A-Z]{2})\.([A-Za-z]+)(?:\.([A-Za-z]+))?$")
CATALOG_TTL_S = 600
TONE_LABEL = {"PleasantSurprised": "Pleasantly surprised"}

_catalog: dict[str, list[dict]] = {}
_catalog_at = 0.0
_lock = asyncio.Lock()


def parse(body: dict) -> dict[str, list[dict]]:
    """list_voices' answer → {app language: [{id, speaker, tone}]}, for the call languages only."""
    to_lang = {loc.upper(): lang for lang, loc in MAGPIE_LOCALE.items()}
    by_lang: dict[str, list[dict]] = {}
    for group in body.values():
        for name in (group or {}).get("voices", []):
            m = _NAME.match(name)
            lang = to_lang.get(m.group(1)) if m else None
            if not lang:
                continue
            speaker, tone = m.group(2), m.group(3)
            vid = f"{speaker}.{tone}" if tone else speaker
            lst = by_lang.setdefault(lang, [])
            if not any(v["id"] == vid for v in lst):
                lst.append({"id": vid, "speaker": speaker, "tone": tone})
    return by_lang


async def catalog(http: aiohttp.ClientSession | None = None) -> dict[str, list[dict]]:
    """The voices per call language; the last good list (or empty) if Magpie can't be reached."""
    global _catalog, _catalog_at
    if not MAGPIE_TTS_URL:
        return {}
    if _catalog and time.time() - _catalog_at < CATALOG_TTL_S:
        return _catalog
    async with _lock:
        if _catalog and time.time() - _catalog_at < CATALOG_TTL_S:
            return _catalog
        own = http is None
        http = http or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        try:
            async with http.get(f"{MAGPIE_TTS_URL}/audio/list_voices", timeout=aiohttp.ClientTimeout(total=5)) as r:
                r.raise_for_status()
                parsed = parse(await r.json())
            if parsed:
                _catalog, _catalog_at = parsed, time.time()
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't list Magpie voices: %s", e)
            _catalog_at = time.time() - CATALOG_TTL_S + 30          # try again in 30 s, keep the old list
        finally:
            if own:
                await http.close()
    return _catalog


def all_voices(by_lang: dict[str, list[dict]], first: str | None = None) -> list[dict]:
    """Every voice and tone on offer in any call language, each once (the speaker's language first)."""
    out: dict[str, dict] = {}
    langs = ([first] if first in by_lang else []) + [l for l in by_lang if l != first]
    for lang in langs:
        for v in by_lang[lang]:
            out.setdefault(v["id"], v)
    return list(out.values())


def normalise(voice: str | None) -> str | None:
    """A stored or requested choice → a voice id ("female" → "Louise"); None for anything malformed."""
    if not voice:
        return None
    voice = LEGACY.get(voice, voice)
    return voice if VOICE_ID.match(voice) else None


def voice_in(by_lang: dict[str, list[dict]], lang: str, voice: str | None) -> str:
    """The voice listeners in `lang` hear the speaker in: theirs with the tone, else without it, else the default."""
    v = normalise(voice) or DEFAULT_VOICE
    ids = {x["id"] for x in by_lang.get(lang, [])}
    if not ids:                                       # no catalogue (Magpie down at start): trust the name
        return v
    if v in ids:
        return v
    speaker = v.split(".")[0]
    return speaker if speaker in ids else (DEFAULT_VOICE if DEFAULT_VOICE in ids else sorted(ids)[0])


def magpie_name(by_lang: dict[str, list[dict]], lang: str, voice: str | None) -> str:
    return f"Magpie-Multilingual.{MAGPIE_LOCALE[lang].upper()}.{voice_in(by_lang, lang, voice)}"


def label(voice: str | None) -> str:
    """"Sofia.Calm" → "Sofia · Calm"."""
    v = normalise(voice) or DEFAULT_VOICE
    speaker, _, tone = v.partition(".")
    return f"{speaker} · {TONE_LABEL.get(tone, tone)}" if tone else speaker
