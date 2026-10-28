"""The wake phrase, as speech recognition writes it (ported from meetmind's tests/unit/voice.test.ts)."""
import pytest

from app.wake import may_become_wake, strip_wake_phrase

CMD = "इस कॉल में कौन कौन है?"


@pytest.mark.parametrize("text,command", [
    ("Hey Mind, add Rahul to this call.", "add Rahul to this call."),
    ("hey mine who is in this meeting", "who is in this meeting"),
    ("Okay meet mind schedule a call", "schedule a call"),
    ("Hey Mind", ""),
    ("हेमाइंड इस कॉल में कौन कौन है?", CMD),
    ("हिमाइंड इस कॉल में कौन कौन है?", CMD),
    ("माइ। माइंड! इस कॉल में कौन कौन है?", CMD),
    ("माइंड, मीटिंग कब है?", "मीटिंग कब है?"),
    ("पेमाइंड इस कॉल में कौन कौन है?", CMD),
    ("पाइमाइंड इस कॉल में कौन कौन है?", CMD),
    ("फेमाइंड इस कॉल में कौन कौन है?", CMD),
    ("पे माइंड, इस कॉल में कौन कौन है?", CMD),
    ("पमाइंड इस कॉल में कौन कौन है?", CMD),
    ("पह माइंड इस कॉल में कौन कौन है?", CMD),
    ("हे माइंड, राहुल को जोड़ो", "राहुल को जोड़ो"),
    ("Pay mind, who is in this call?", "who is in this call?"),       # Hindi "हे माइंड" written in Latin
    ("హే మైండ్, ఎయిర్‌పోర్ట్‌కి క్యాబ్ బుక్ చేయి", "ఎయిర్‌పోర్ట్‌కి క్యాబ్ బుక్ చేయి"),      # Telugu
    ("ഹേ മൈൻഡ്, ഒരു ക്യാബ് ബുക്ക് ചെയ്യൂ", "ഒരു ക്യാബ് ബുക്ക് ചെയ്യൂ"),               # Malayalam
])
def test_wakes_and_keeps_the_command(text, command):
    assert strip_wake_phrase(text) == (True, command)


@pytest.mark.parametrize("text", [
    "हिमालय की यात्रा अगले महीने है",       # "Himalaya"
    "माइंड इस बात का ध्यान रखना",
    "मेरा माइंड बदल गया।",
    "पेमेंट हो गया है।",                      # "payment"
    "परमानेंट समाधान चाहिए।",                 # "permanent"
    "I don't mind if we move it to Friday",
    "hey everyone, mind the gap",
])
def test_ordinary_speech_is_not_the_wake_phrase(text):
    assert strip_wake_phrase(text)[0] is False


def test_partials_that_may_become_the_wake_phrase_are_held():
    for t in ["Hey", "hey,", "हे", "पे", "ok", "Hey my", "Pay mi", "हे मा"]:   # partials seen in a real call
        assert may_become_wake(t), t
    for t in ["Hello everyone", "हेलो सब लोग", "We finished", "Hey my friend", "Pay the bill"]:
        assert not may_become_wake(t), t
