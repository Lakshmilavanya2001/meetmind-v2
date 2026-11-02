"""Parakeet ignores the language it is sent and sometimes answers in one nobody in the call speaks."""
from app.asr import expected_script


def test_text_in_a_language_nobody_speaks_is_dropped():
    # heard in a Hindi/English call on 2026-11-02
    assert expected_script("Бахуточа Дания вадребин", "hi") == ""
    assert expected_script("Десять", "en") == ""
    assert expected_script("Капец зовет", "en") == ""
    assert expected_script("は い。", "en") == ""


def test_the_speakers_own_language_and_english_are_kept():
    hindi = "हाँ सही हम चाहते हैं कि दोनों नंबर एक ही बिल में आ जाए।"
    assert expected_script(hindi, "hi") == hindi
    assert expected_script("Okay, now i'm, you know.", "hi") == "Okay, now i'm, you know."   # English in Hindi speech
    assert expected_script("Harry बिन पी शुरू कर", "hi") == "Harry बिन पी शुरू कर"
    assert expected_script("Hello priya, 3 pm?", "en") == "Hello priya, 3 pm?"
    # English speech partly in Devanagari is put back into English later (english_only), not dropped
    assert expected_script("I एडेड दैट stop word", "en") == "I एडेड दैट stop word"
    assert expected_script("", "hi") == ""


def test_a_stray_foreign_word_is_left_out():
    assert expected_script("हम दोनों एक ही प्लान पर आना चाहिए Да", "hi") == "हम दोनों एक ही प्लान पर आना चाहिए"
    # Tamil letters aren't Hindi's either
    assert expected_script("வணக்கம் நண்பா", "hi") == ""
