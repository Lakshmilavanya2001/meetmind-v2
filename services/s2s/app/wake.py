""""Hey Mind": spotting the wake phrase at the start of a transcript, in any call language.

Indian-language speech recognition writes everything in the speaker's own script, including the
English words "Hey Mind" (Telugu "హే మైండ్", Malayalam "ഹേ മൈൻഡ്"). Bengali, Gurmukhi, Gujarati,
Odia, Tamil, Telugu, Kannada and Malayalam share Devanagari's Unicode layout, so they're mapped
letter for letter onto Devanagari and one set of patterns is matched. The mapping keeps length,
so the command is cut from the original text. (Ported from meetmind's src/lib/voice-commands.ts,
with the spellings collected from Parakeet in real calls.)
"""
from __future__ import annotations

import re

# Malayalam chillu letters (consonants without a vowel) sit outside the shared layout.
_CHILLU = {0x0D7A: 0x0923, 0x0D7B: 0x0928, 0x0D7C: 0x0930, 0x0D7D: 0x0932, 0x0D7E: 0x0933, 0x0D7F: 0x0915}


def to_devanagari(text: str) -> str:
    out = []
    for ch in text:
        c = ord(ch)
        if c in _CHILLU:
            out.append(chr(_CHILLU[c]))
        elif 0x0980 <= c <= 0x0D7F:
            out.append(chr(c - (c & 0xFF80) + 0x0900))
        else:
            out.append(ch)
    return "".join(out)


# Latin: "Hey Mind". Recognisers also write "hey mine", "hey meet mind", "hi mind", "okay mind".
# Hindi speakers' "हे माइंड" can come back in Latin letters too, as "Pay mind" (Parakeet's partials).
_WAKE_LATIN = re.compile(r"^\s*(?:hey|hi|hay|okay|ok|hello|pay|pe)[\s,!.]*(?:meet\s*mind|mind|mine|mined|minds)\b[\s,!.:?-]*", re.I)
# Parakeet often hears Hindi "हे" before "माइंड" as "पे", "पाइ", "फे" or "पह"; none is a word. Those forms
# need the full "माइंड" sound, so "पेमेंट" (payment) doesn't count.
_WAKE_INDIC_PAY = re.compile(r"^\s*(?:पे|पै|पाइ|पाई|पह|पहे|फे|फै|फाइ|फह|प|फ)्?[\s,।!.]*मा(?:इ|ई)ं?न?्?ड्?(?=$|[\s,।!.?:-])[\s,।!.?:-]*")
# हे/हाय/ओके + (मीट) + माइंड/मैंड/मैण्ट/मैनड…; a clipped first syllable made only of the sounds in "hey" counts.
_WAKE_INDIC = re.compile(r"^\s*(?:(?:हे|है|हाय|हेय|हॆ|हॆय|हाइ|हाई|हि|ओके|ओकॆ|हलो|हेलो)्?|[हयटेैाइि्]{1,3})[\s,।!.]*(?:मीट\s*)?म(?:ा|ै|ाइ|ाई|ॆ|े)?(?:ं|ँ|न्?|ण्?)(?:ड|ट|द)्?(?=$|[\s,।!.?:-])[\s,।!.?:-]*")
# "Hey" dropped altogether, "Mind" punctuated at the very start ("माइंड! …", "माइ। माइंड! …").
_WAKE_INDIC_BARE = re.compile(r"^\s*(?:मा(?:इ|ई)[\s,।!.]+)?मा(?:इ|ई)ं?(?:न्)?ड्?[,।!.]+\s*")
# What a transcript can start with while the wake phrase is still being said (partial captions).
# ("Hey", "Hey my", "Pay mi", "हे", "हे मा"): a wake-like start with at most the beginning of "mind".
_MAYBE_WAKE = re.compile(r"^\s*(?:hey|hi|hay|ok|okay|hello|pay|pe|हे|है|हाय|हेय|हाइ|हाई|ओके|हलो|हेलो|पे|पाइ|फे|पह)"
                         r"(?:[\s,!.।]+(?:m|mi|my|min|mine|mai|मा|माइ|मै|मैं))?[\s,!.।]*$", re.I)


def strip_wake_phrase(text: str) -> tuple[bool, str]:
    """(woke, command): whether the text starts with "Hey Mind", and what follows it."""
    dev = to_devanagari(text)
    m = _WAKE_LATIN.match(text) or _WAKE_INDIC.match(dev) or _WAKE_INDIC_PAY.match(dev) or _WAKE_INDIC_BARE.match(dev)
    return (True, text[m.end():].strip()) if m else (False, text.strip())


def may_become_wake(text: str) -> bool:
    """A partial transcript that is only the start of the wake phrase ("Hey", "हे"): hold it back
    from the others' captions until it's clear whether it's for the assistant."""
    return bool(_MAYBE_WAKE.match(to_devanagari(text)))
