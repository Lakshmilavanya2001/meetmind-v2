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
