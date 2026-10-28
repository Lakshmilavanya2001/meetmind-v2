"""Speaker-owned voices and tones: Magpie's catalogue, what each listener language hears, and the routes."""
import pytest
from conftest import signup

from app import voices

LIST = {"en-US,hi-IN,fr-FR": {"voices": [
    "Magpie-Multilingual.EN-US.Louise", "Magpie-Multilingual.EN-US.Leo", "Magpie-Multilingual.EN-US.Mia",
    "Magpie-Multilingual.EN-US.Mia.Calm", "Magpie-Multilingual.EN-US.Mia.Angry", "Magpie-Multilingual.EN-US.Leo.Calm",
    "Magpie-Multilingual.HI-IN.Louise", "Magpie-Multilingual.HI-IN.Leo", "Magpie-Multilingual.HI-IN.Leo.Calm",
    "Magpie-Multilingual.FR-FR.Pascal", "not-a-voice"]}}
CAT = voices.parse(LIST)


def test_catalogue_keeps_call_languages_with_tones():
    assert set(CAT) == {"en", "hi"}                                    # French isn't a call language here
    assert {"id": "Mia.Calm", "speaker": "Mia", "tone": "Calm"} in CAT["en"]
    assert [v["id"] for v in CAT["hi"]] == ["Louise", "Leo", "Leo.Calm"]
    ids = [v["id"] for v in voices.all_voices(CAT, "hi")]
    assert ids[:3] == ["Louise", "Leo", "Leo.Calm"] and "Mia.Angry" in ids and len(ids) == len(set(ids))


@pytest.mark.parametrize("lang,chosen,heard", [
    ("en", "Mia.Calm", "Mia.Calm"),          # the language has the voice and tone
    ("hi", "Leo.Calm", "Leo.Calm"),
    ("hi", "Mia.Calm", "Louise"),            # Hindi has no Mia: the default voice
    ("en", "Louise.Calm", "Louise"),         # no such tone: the same voice without it
    ("hi", "male", "Leo"),                   # the old choices still work
    ("en", "female", "Louise"),
    ("en", None, "Louise"),
    ("en", "../etc", "Louise"),              # malformed: the default
])
def test_what_each_listener_hears(lang, chosen, heard):
    assert voices.voice_in(CAT, lang, chosen) == heard


def test_names_and_labels():
    assert voices.magpie_name(CAT, "hi", "Leo.Calm") == "Magpie-Multilingual.HI-IN.Leo.Calm"
    assert voices.label("Mia.Calm") == "Mia · Calm" and voices.label("male") == "Leo"
    assert voices.voice_in({}, "en", "Mia.Calm") == "Mia.Calm"        # no list (Magpie down at start): trust it


@pytest.fixture
def magpie(monkeypatch):
    async def catalog(http=None):
        return CAT
    monkeypatch.setattr(voices, "catalog", catalog)


async def test_join_with_a_voice_and_tone_and_change_it_in_the_call(client, magpie):
    from conftest import jwt_claims
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    r = await client.get("/api/voices?lang=hi")
    assert r.status_code == 200 and r.json()["voices"][0]["id"] == "Louise" and r.json()["default"] == "Louise"
    code = (await client.post("/api/meetings", json={"title": "Review"})).json()["meeting"]["joinCode"]
    bad = await client.post(f"/api/meetings/{code}/join", json={"speak": "en", "hear": "hi", "voice": "Nobody.Calm"})
    assert bad.status_code == 400
    r = await client.post(f"/api/meetings/{code}/join", json={"speak": "en", "hear": "hi", "voice": "Mia.Calm"})
    assert r.status_code == 200 and jwt_claims(r.json()["token"])["attributes"]["voice"] == "Mia.Calm"
    r = await client.patch(f"/api/meetings/{code}/preferences", json={"voice": "Leo.Calm", "noise": "enhanced"})
    assert r.json()["myPreferences"] == {"speak": "en", "hear": "hi", "voice": "Leo.Calm", "noise": "enhanced"}
    assert (await client.get(f"/api/meetings/{code}")).json()["myPreferences"]["voice"] == "Leo.Calm"
    r = await client.post(f"/api/meetings/{code}/join", json={"speak": "en", "hear": "hi", "voice": "male"})
    assert jwt_claims(r.json()["token"])["attributes"]["voice"] == "Leo"
    assert (await client.patch(f"/api/meetings/{code}/preferences", json={"noise": "loud"})).status_code == 400


async def test_preferences_need_a_participant(client, magpie):
    from conftest import other
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    org_code = (await client.get("/api/me")).json()["organization"]["joinCode"]
    code = (await client.post("/api/meetings", json={"title": "Review"})).json()["meeting"]["joinCode"]
    ben = other(client)
    await signup(ben, "Ben Das", "ben@acme.test", join_code=org_code)
    assert (await ben.patch(f"/api/meetings/{code}/preferences", json={"voice": "Leo"})).status_code == 403
    assert (await other(client).get("/api/voices")).status_code == 401
