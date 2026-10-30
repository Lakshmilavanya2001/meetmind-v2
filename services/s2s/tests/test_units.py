"""Pure functions: dates, guards, voice yes/no, reasoning-marker stripping (no database)."""
import asyncio
from datetime import datetime, timezone

import pytest

from app.assistant import llm
from app.assistant.dates import resolve
from app.assistant.guards import (Candidate, HistoryEntry, SearchResolution, email_provided_by_user, find_unbacked_claims,
                                  find_unconfirmed_people, place_is_grounded)
from app.assistant.voice import NO, YES

NOW = datetime(2026, 10, 28, 7, 30, tzinfo=timezone.utc)        # Wednesday 13:00 in Kolkata


@pytest.mark.parametrize("said,expected", [
    ("tomorrow at 4 pm", "Thursday 29 October 2026, 16:00"),
    ("tomorrow at 4", "Thursday 29 October 2026, 16:00"),             # no am/pm, 1-7: afternoon
    ("Friday at 11", "Friday 30 October 2026, 11:00"),
    ("at 3", "Wednesday 28 October 2026, 15:00"),                     # a time today, not March
    ("at 11", "Thursday 29 October 2026, 11:00"),                     # already past today: tomorrow
    ("next Monday at 9:30", "Monday 02 November 2026, 09:30"),
    ("next week Tuesday at 2 pm", "Tuesday 03 November 2026, 14:00"),
    ("tonight", "Wednesday 28 October 2026, 20:00"),
    ("tomorrow morning", "Thursday 29 October 2026, 09:00"),
    ("in 2 hours", "Wednesday 28 October 2026, 15:00"),
])
def test_dates(said, expected):
    r = resolve(said, "Asia/Kolkata", NOW)
    assert r["ok"] and r["timeSpecified"] and r["startLocal"].startswith(expected), r


def test_dates_without_a_time_or_duration():
    r = resolve("Friday", "Asia/Kolkata", NOW)
    assert r["ok"] and not r["timeSpecified"] and "no time given" in r["startLocal"]
    r = resolve("Friday 11am for 45 minutes", "Asia/Kolkata", NOW)
    assert r["durationMinutes"] == 45 and r["end"].startswith("2026-10-30T11:45")
    assert resolve("tomorrow at 4", "Asia/Kolkata", NOW)["note"].startswith("No am/pm given")
    assert not resolve("blah blah", "Asia/Kolkata", NOW)["ok"]


def test_people_need_choosing_when_ambiguous():
    a, b = Candidate("1", "Priya Shah", "ps@x.test", "Designer"), Candidate("2", "Priya Nair", "pn@x.test", "Engineer")
    hist = [HistoryEntry("TOOL", tool_name="search_users", resolution=SearchResolution("ambiguous", [a, b]))]
    assert [u for u, _ in find_unconfirmed_people(hist, ["1"])] == ["1"]
    assert find_unconfirmed_people(hist + [HistoryEntry("USER", "ps@x.test")], ["1"]) == []
    assert find_unconfirmed_people(hist + [HistoryEntry("USER", "the designer")], ["1"]) == []
    assert find_unconfirmed_people(hist, ["99"]) == []                 # never searched: other checks apply


def test_claims_places_and_emails():
    assert find_unbacked_claims("I've scheduled the review.", []) == ["creation or booking"]
    assert find_unbacked_claims("I've scheduled the review.", ["create_meeting"]) == []
    assert find_unbacked_claims("I'll schedule it once you confirm.", []) == []
    assert "cancellation" in find_unbacked_claims("The meeting has been cancelled.", [])
    assert place_is_grounded("12 MG Road", "deliver to 12 MG Road, Bengaluru please")
    assert not place_is_grounded("221B Baker Street", "deliver to my office")
    assert email_provided_by_user([HistoryEntry("USER", "invite ravi@ext.test")], "Ravi@ext.test")
    assert not email_provided_by_user([HistoryEntry("ASSISTANT", "ravi@ext.test?")], "ravi@ext.test")


@pytest.mark.parametrize("said", ["yes", "Yes, go ahead.", "sure", "okay", "हाँ", "हां, कर दो", "जी हाँ", "haan kar do", "confirm"])
def test_voice_yes(said):
    assert YES.search(said) and not NO.search(said)


@pytest.mark.parametrize("said", ["no", "No thanks", "cancel that", "don't", "नहीं", "नहीं, रहने दो", "never mind"])
def test_voice_no(said):
    assert NO.search(said)


def test_voice_other_words_are_neither():
    for said in ["what time is it", "schedule a meeting", "nothing much"]:
        assert not YES.search(said) and not NO.search(said)


def test_reasoning_markers_are_stripped(monkeypatch):
    class R:
        status = 200

        def __init__(self, c):
            self.c = c

        async def json(self):
            return {"choices": [{"message": {"content": self.c}}]}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class H:
        def __init__(self, c):
            self.c = c

        def post(self, *a, **k):
            return R(self.c)
    monkeypatch.setattr(llm, "LLM_URL", "http://x")
    for raw, want in [("A. </think> A.", "A."), ("<think>hmm</think>Answer", "Answer"), ("plain", "plain")]:
        assert asyncio.run(llm.chat(H(raw), []))["content"] == want


def test_minds_private_answer_is_echo_only_for_its_listener():
    import time as _t

    import numpy as np

    from app.agent import OUT_SR, PlayoutTimeline
    tl = PlayoutTimeline()
    now = _t.time()
    loud = (np.ones(960) * 8000).astype(np.int16).tobytes()
    tl.add(now, loud, owner="asha")                    # Mind answering Asha on her own track
    tl.add(now + 0.02, loud)                           # a translation everyone may hear
    asha = np.frombuffer(tl.read(now, int(OUT_SR * 0.04), "asha"), np.int16)
    ben = np.frombuffer(tl.read(now, int(OUT_SR * 0.04), "ben"), np.int16)
    assert asha[:960].any() and asha[960:1920].any()
    assert not ben[:960].any() and ben[960:1920].any()


@pytest.mark.parametrize("said,ends", [
    ("Thanks Mind.", True), ("Thank you.", True), ("That's all", True), ("Okay thanks, Mind", True), ("Bye", True),
    ("Stop", True), ("Stop Mind", True), ("Okay stop it, Mind.", True), ("Mind, stop listening", True), ("Stop now please", True),
    ("धन्यवाद", True), ("बस", True), ("माइंड रुको", True), ("Done.", True),
    ("Thanks for booking the cab, now order lunch", False), ("Stop the transcript", False), ("Yes, go ahead.", False),
    ("No", False), ("Stop the meeting for everyone", False), ("What is the budget", False),
])
def test_what_ends_a_conversation_with_mind(said, ends):
    from app.agent import is_mind_exit
    assert is_mind_exit(said) is ends


async def test_a_conversation_with_mind_stays_open_while_it_answers_and_after():
    import time as _t
    from types import SimpleNamespace

    from app import agent
    call = agent.CallTranslator(SimpleNamespace(name="mm-test-room"))
    sent = []

    async def send(msg, to=None):
        sent.append((msg, to))
    call.send = send
    assert not call.awake("ben")
    call.mind_busy["ben"] = 1                                   # a question is being answered
    call.open_mind("ben", _t.time() + 120)
    assert call.awake("ben")
    call.mind_spoken("ben", _t.time(), "answer", 0)             # ...and has been spoken: 20 s from now, not 120
    assert call.awake("ben") and abs(call.awake_until["ben"] - (_t.time() + agent.MIND_FOLLOWUP_S)) < 1
    # Replies repeat Mind's words; that alone isn't echo (only 4+ words mostly repeating Mind are).
    call.mind_last["ben"] = (_t.time(), call._words("Options: Auto, Mini, Sedan. Say yes to go ahead, or no to cancel."))
    assert not call.repeats_mind("ben", "Yes, go ahead.")
    assert not call.repeats_mind("ben", "I would prefer mini")
    assert call.repeats_mind("ben", "say yes to go ahead or no to cancel")
    # "Stop" while another answer is still coming: closed at once, and that answer doesn't reopen it.
    call.mind_busy["ben"] = 1
    call.end_mind("ben")
    assert not call.awake("ben")
    call.mind_spoken("ben", _t.time(), "answer", 0)             # the old answer (epoch 0) finishing
    assert not call.awake("ben")
    await __import__("asyncio").sleep(0.05)
    assert [m["active"] for m, to in sent if m.get("type") == "mind_session"][-1] is False


def test_english_speech_spelled_in_devanagari_is_put_back_into_english():
    """Parakeet, asked for English, sometimes writes the second half in Devanagari."""
    from types import SimpleNamespace
    from app import agent
    text = "Ribin also you mentioned it and now we have. एडेड दैट स्टॉप वर्ड एंड सो"
    runs = agent.indic_runs(text)
    assert [(text[a:b], lang) for a, b, lang in runs] == [("एडेड दैट स्टॉप वर्ड एंड सो", "hi")]
    assert agent.indic_runs("All in English, no change.") == []
    assert [lang for _, _, lang in agent.indic_runs("one தமிழ் two")] == ["ta"]
    q = "हे माइंड, व्हाट मीटिंग्स?"
    assert [q[a:b] for a, b, _ in agent.indic_runs(q)] == [q]                # the end mark goes with the run

    asked = []

    class FakeTranslator:
        async def to_english(self, t, src, on_delta=None):
            asked.append((t, src))
            return "added that stop word and so"

    call = agent.CallTranslator(SimpleNamespace(name="mm-test-room"))
    call.translator = FakeTranslator()
    fixed = asyncio.run(call.english_only(text))
    assert fixed == "Ribin also you mentioned it and now we have. added that stop word and so"
    assert asked == [("एडेड दैट स्टॉप वर्ड एंड सो", "hi")]
    assert asyncio.run(call.english_only("Hey Mind, what's next?")) == "Hey Mind, what's next?"
