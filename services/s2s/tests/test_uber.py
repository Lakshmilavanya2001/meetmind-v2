"""Uber: connecting your own account (OAuth, tokens encrypted at rest, refresh, reconnect), real options and
fares, booking held to the confirmed fare, following a test ride stage by stage, cancelling; the tools,
the routes, the "Open in Uber" link and the geocoder (meetmind's tests/integration/uber.test.ts)."""
import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import other, signup
from fake_uber import FakeUber
from sqlalchemy import func, select, text, update

from app import db
from app.assistant.tools.base import ToolContext, ToolError
from app.assistant.tools.uber import TOOLS
from app.crypto import CryptoError, decrypt, encrypt
from app.uber import geocode, service
from app.uber.links import LinkPlace, classify_result_link, uber_link_for, uber_ride_link
from app.uber.service import UberConnection, UberRide

ROUTE = {"pickup": "Indiranagar, Bengaluru", "drop": "Kempegowda Airport"}
TOOL = {t.name: t for t in TOOLS}


@pytest.fixture
async def uber(client, monkeypatch):
    from app.uber.api import router
    from app.web import app
    if not any(getattr(r, "path", "") == "/api/integrations/uber" for r in app.routes):
        # until web.py includes it: ahead of the static files mounted at "/"
        n = len(app.router.routes)
        app.include_router(router)
        added = app.router.routes[n:]
        del app.router.routes[n:]
        app.router.routes[:0] = added
    fake = await FakeUber().start(monkeypatch)
    monkeypatch.setattr(geocode, "MIN_GAP_S", 0)
    geocode.reset_cache()
    service.reset_quotes()
    yield fake
    await fake.stop()


async def _people(client):
    """Aisha and Priya in Acme (their clients and ctx-likes)."""
    await signup(client, "Aisha Khan", "aisha@acme.test", org="Acme")
    code = (await client.get("/api/me")).json()["organization"]["joinCode"]
    priya_c = other(client)
    await signup(priya_c, "Priya Patel", "priya@acme.test", join_code=code)

    async def ctx(c):
        me = (await c.get("/api/me")).json()
        return SimpleNamespace(org_id=uuid.UUID(me["organization"]["id"]), user_id=uuid.UUID(me["user"]["id"]))
    return await ctx(client), priya_c, await ctx(priya_c)


def _s():
    return db.sessionmaker()()


async def _connect(who):
    async with _s() as s:
        await service.finish_connect(s, who, "good-code")


async def _tool_ctx(s, who, said: str) -> ToolContext:
    conv = (await s.execute(text("INSERT INTO agent_conversations (organization_id, user_id, title) VALUES (:o, :u, 'chat') RETURNING id"),
                            {"o": who.org_id, "u": who.user_id})).scalar_one()
    await s.execute(text("INSERT INTO agent_messages (conversation_id, role, content) VALUES (:c, 'USER', :t)"), {"c": conv, "t": said})
    await s.commit()
    return ToolContext(s=s, user_id=who.user_id, org_id=who.org_id, user_name="Aisha Khan", user_email="aisha@acme.test",
                       timezone="Asia/Kolkata", role="OWNER", conversation_id=conv, meeting_code=None, now=datetime.now(timezone.utc))


# ─── Connect Uber ────────────────────────────────────────────────────────
async def test_not_configured_says_so_and_the_assistant_falls_back_to_the_sandbox(client, uber, monkeypatch):
    monkeypatch.setenv("UBER_CLIENT_ID", "")
    aisha, priya_c, _ = await _people(client)
    st = (await client.get("/api/integrations/uber")).json()
    assert st["configured"] is False and st["connected"] is False
    with pytest.raises(ToolError, match="isn't set up"):
        service.start_connect(aisha)
    assert (await client.get("/api/integrations/uber/connect")).status_code == 503
    async with _s() as s:
        out = await TOOL["get_uber_options"].run(await _tool_ctx(s, aisha, "cab"), TOOL["get_uber_options"].args(**ROUTE))
    assert out.data["connected"] is False and "get_cab_options" in out.data["message"] and out.link == "/settings"
    await priya_c.aclose()


async def test_connects_through_uber_sign_in_with_a_state_bound_to_the_user(client, uber):
    aisha, priya_c, priya = await _people(client)
    r = await client.get("/api/integrations/uber/connect", follow_redirects=False)
    assert r.status_code == 302
    to = urlparse(r.headers["location"])
    assert f"{to.scheme}://{to.netloc}{to.path}" == f"{uber.base}/auth/oauth/v2/authorize"
    q = parse_qs(to.query)
    assert q["scope"] == ["ride_request.estimate ride_request.ride_booking"]
    assert q["redirect_uri"] == ["https://meet.test/api/integrations/uber/callback"]
    state = q["state"][0]
    set_cookie = r.headers["set-cookie"]
    assert "mm2_uber_state=" in set_cookie and "HttpOnly" in set_cookie and "samesite=lax" in set_cookie.lower()
    assert "Path=/api/integrations/uber" in set_cookie
    state_cookie = re.search(r"mm2_uber_state=([^;]+)", set_cookie)[1].strip('"')
    session = client.cookies.get("mm2_session")
    priya_session = priya_c.cookies.get("mm2_session")

    async def back(query, cookie=state_cookie, sess=session):
        bare = other(client)
        try:
            res = await bare.get(f"/api/integrations/uber/callback?{query}", headers={"cookie": f"mm2_session={sess}; mm2_uber_state={cookie}"})
        finally:
            await bare.aclose()
        assert res.status_code == 302
        return res.headers["location"]

    # A different state (e.g. a link from another site) or another user's session is refused.
    assert "uber=expired" in await back("code=good-code&state=forged")
    assert "uber=expired" in await back(f"code=good-code&state={state}", sess=priya_session)
    assert "uber=denied" in await back(f"error=access_denied&state={state}")
    assert "uber=signin" in await back(f"code=good-code&state={state}", sess="nope")
    assert (await client.get("/api/integrations/uber")).json()["connected"] is False
    assert "uber=error" in await back(f"code=bad-code&state={state}")

    assert await back(f"code=good-code&state={state}") == "https://meet.test/settings?uber=connected"
    st = (await client.get("/api/integrations/uber")).json()
    assert st["configured"] is True and st["env"] == "sandbox" and st["connected"] is True
    async with _s() as s:
        row = (await s.execute(select(UberConnection).where(UberConnection.user_id == aisha.user_id))).scalar_one()
    # Stored encrypted: no plaintext field names, and it decrypts to the tokens (checking for "A1" itself in
    # random base64 ciphertext fails now and then).
    from app.crypto import decrypt_json
    assert row.encrypted_tokens.startswith("v1.") and "accessToken" not in row.encrypted_tokens
    assert decrypt_json(row.encrypted_tokens) == {"accessToken": "A1", "refreshToken": "R1"}
    assert row.scopes == ["ride_request.estimate", "ride_request.ride_booking"] and row.status == "ACTIVE"
    assert (await priya_c.get("/api/integrations/uber")).json()["connected"] is False
    await priya_c.aclose()


async def test_refreshes_an_expiring_login_a_refused_refresh_asks_to_reconnect_disconnect_revokes(client, uber):
    aisha, priya_c, _ = await _people(client)
    uber.expires_in = 30                                   # expires within a minute
    await _connect(aisha)
    async with _s() as s:
        await service.uber_options(s, aisha, ROUTE["pickup"], ROUTE["drop"])
    assert uber.refreshed == 1 and uber.tokens[-1] == "Bearer A2"

    async with _s() as s:
        await s.execute(update(UberConnection).values(expires_at=func.now()))
        await s.commit()
    uber.fail_token = True
    async with _s() as s:
        with pytest.raises(ToolError, match="Reconnect Uber"):
            await service.uber_options(s, aisha, ROUTE["pickup"], ROUTE["drop"])
    st = (await client.get("/api/integrations/uber")).json()
    assert st["connected"] is False and st["needsReconnect"] is True

    assert (await client.post("/api/integrations/uber/disconnect")).json() == {"connected": False}
    assert uber.revoked == 1
    async with _s() as s:
        assert (await s.execute(select(func.count()).select_from(UberConnection))).scalar() == 0
    await priya_c.aclose()


# ─── Uber rides (test mode) ──────────────────────────────────────────────
async def test_real_options_and_fares_for_the_route(client, uber):
    aisha, priya_c, _ = await _people(client)
    async with _s() as s:
        with pytest.raises(ToolError, match="isn't connected"):
            await service.uber_options(s, aisha, ROUTE["pickup"], ROUTE["drop"])
    await _connect(aisha)
    async with _s() as s:
        r = await service.uber_options(s, aisha, ROUTE["pickup"], ROUTE["drop"])
        assert r["env"] == "sandbox" and "test mode" in r["note"]
        assert r["options"][0] == {"product_id": "go", "name": "UberGo", "seats": 4, "fare": "₹240-290", "surge": None,
                                   "pickupEtaMin": 4, "tripMin": 40, "distanceKm": 34.4}
        with pytest.raises(ToolError, match="Couldn't find the pickup point"):
            await service.uber_options(s, aisha, "Nowhere land", ROUTE["drop"])
        # the tool says the same, with connected: true
        out = await TOOL["get_uber_options"].run(await _tool_ctx(s, aisha, "cab"), TOOL["get_uber_options"].args(**ROUTE))
        assert out.data["connected"] is True and out.summary == "2 Uber options"
    await priya_c.aclose()


async def test_books_only_after_a_quote_holds_the_fare_then_follows_the_ride_stage_by_stage(client, uber):
    aisha, priya_c, _ = await _people(client)
    await _connect(aisha)
    args = (ROUTE["pickup"], ROUTE["drop"], "go")
    async with _s() as s:
        with pytest.raises(ToolError, match="fare has expired"):
            await service.book_ride(s, aisha, *args)
        q = await service.quote_ride(s, aisha, *args)
        assert service.describe_ride(q) == ("Book an Uber UberGo from “Indiranagar, Bengaluru” to “Kempegowda Airport”, pickup now, "
                                            "driver ~4 min away, ~40 min trip. Fare ₹250. Uber test mode — no real car comes and nothing is charged.")
        uber.fare = 400                                     # went up while the user was deciding
        with pytest.raises(ToolError, match="went up from ₹250 to ₹400"):
            await service.book_ride(s, aisha, *args)
        uber.fare = 260
        await service.quote_ride(s, aisha, *args)
        uber.fare = 265                                     # within 10%
        b = await service.book_ride(s, aisha, *args)
        ride = b["ride"]
        assert ride["provider"] == "uber-sandbox" and ride["kind"] == "CAB" and ride["title"] == "Uber UberGo to Kempegowda Airport"
        assert ride["total"] == "₹265" and b["status"]["status"] == "processing" and b["status"]["label"] == "Finding a driver"
        row = await s.get(UberRide, uuid.UUID(ride["id"]))
        assert row.fare_paise == 26500 and row.request_id == "req-1-abcdef"
        assert uber.rides["req-1-abcdef"]["body"]["fare_id"] == "fare-265"

        st = await service.get_ride_status(s, aisha, ride["id"])
        assert st["label"] == "Finding a driver" and st["driver"] is None
        st = await service.advance_test_ride(s, aisha, ride["id"])
        assert {k: st[k] for k in ("status", "label", "driver", "vehicle", "etaMin")} == {
            "status": "accepted", "label": "Driver on the way", "driver": "Ravi (★ 4.9)", "vehicle": "Maruti Dzire KA01AB1234", "etaMin": 3}
        assert (await service.advance_test_ride(s, aisha, ride["id"]))["status"] == "arriving"

    async with _s() as s:
        listed = (await service.list_rides(s, aisha.user_id))[0]
    assert listed["uber"] is True and listed["uberTest"] is True and listed["sandbox"] is False and listed["total"] == "₹265"
    assert listed["ride"] == {"status": "arriving", "label": "Driver arriving", "driver": "Ravi (★ 4.9)", "vehicle": "Maruti Dzire KA01AB1234"}
    assert (await client.get("/api/rides")).json()["rides"][0]["id"] == ride["id"]

    async with _s() as s:
        await service.advance_test_ride(s, aisha, ride["id"])
        with pytest.raises(ToolError, match="already under way"):
            await service.cancel_ride(s, aisha, ride["id"])
        await service.advance_test_ride(s, aisha, ride["id"])
        with pytest.raises(ToolError, match="no next stage"):
            await service.advance_test_ride(s, aisha, ride["id"])
        st = await service.get_ride_status(s, aisha, ride["id"])
        assert st["status"] == "completed" and st["final"] is True
    await priya_c.aclose()


async def test_cancels_with_uber_and_other_users_cant_see_or_touch_the_ride(client, uber):
    aisha, priya_c, priya = await _people(client)
    await _connect(aisha)
    async with _s() as s:
        await service.quote_ride(s, aisha, ROUTE["pickup"], ROUTE["drop"], "go")
        ride = (await service.book_ride(s, aisha, ROUTE["pickup"], ROUTE["drop"], "go"))["ride"]
        with pytest.raises(ToolError, match="No Uber ride"):
            await service.get_ride_status(s, priya, ride["id"])
        with pytest.raises(ToolError, match="No Uber ride"):
            await service.advance_test_ride(s, priya, ride["id"])
        with pytest.raises(ToolError, match="No booking"):
            await service.cancel_ride(s, priya, ride["id"])
    assert (await priya_c.get(f"/api/rides/{ride['id']}")).status_code == 404
    assert (await priya_c.post(f"/api/rides/{ride['id']}/cancel")).status_code == 404
    assert (await priya_c.get("/api/rides")).json() == {"rides": []}

    r = await client.post(f"/api/rides/{ride['id']}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["canceled"] is True
    assert uber.rides["req-1-abcdef"]["status"] == "rider_canceled"
    async with _s() as s:
        row = await s.get(UberRide, uuid.UUID(ride["id"]))
        assert row.status == "CANCELED" and row.ride_status == "rider_canceled" and row.canceled_at is not None
        with pytest.raises(ToolError, match="already cancelled"):
            await service.cancel_ride(s, aisha, ride["id"])
    r = await client.post(f"/api/rides/{ride['id']}/cancel")
    assert r.status_code == 400 and "already cancelled" in r.json()["detail"]
    await priya_c.aclose()


async def test_moving_a_ride_along_by_hand_is_refused_outside_test_mode(client, uber):
    aisha, priya_c, _ = await _people(client)
    await _connect(aisha)
    async with _s() as s:
        await service.quote_ride(s, aisha, ROUTE["pickup"], ROUTE["drop"], "go")
        ride = (await service.book_ride(s, aisha, ROUTE["pickup"], ROUTE["drop"], "go"))["ride"]
        await s.execute(update(UberRide).values(env="production"))
        await s.commit()
    r = await client.post(f"/api/rides/{ride['id']}/advance")
    assert r.status_code == 403 and "Only Uber test-mode rides" in r.json()["detail"]
    await priya_c.aclose()


# ─── Tools ───────────────────────────────────────────────────────────────
async def test_book_uber_tool_confirms_grounded_places_then_books(client, uber):
    aisha, priya_c, _ = await _people(client)
    await _connect(aisha)
    book, status, advance = TOOL["book_uber"], TOOL["get_ride_status"], TOOL["advance_test_ride"]
    assert book.kind == "sensitive" and advance.kind == "write" and status.kind == "read"
    assert set(book.spec()["function"]["parameters"]["properties"]) == {"pickup", "drop", "product_id"}
    async with _s() as s:
        t = await _tool_ctx(s, aisha, "Get me an Uber from Indiranagar, Bengaluru to Kempegowda Airport")
        with pytest.raises(ToolError, match="isn't what the user said"):
            await book.preflight(t, book.args(pickup="12 MG Road, Bengaluru", drop=ROUTE["drop"], product_id="go"))
        a = book.args(**ROUTE, product_id="go")
        sentence = await book.preflight(t, a)
        assert sentence.startswith("Book an Uber UberGo from “Indiranagar, Bengaluru”") and "Fare ₹250" in sentence
        out = await book.run(t, a)
        assert out.summary == "Uber UberGo to Kempegowda Airport · Finding a driver" and out.link == "/settings"
        rid = out.data["booking_id"]
        assert (await advance.run(t, advance.args(booking_id=rid))).summary == "Uber UberGo to Kempegowda Airport: Driver on the way"
        assert (await status.run(t, status.args(booking_id=rid))).data["driver"] == "Ravi (★ 4.9)"
        with pytest.raises(ToolError, match="No Uber ride"):
            await status.run(t, status.args(booking_id="not-an-id"))
    await priya_c.aclose()


# ─── Links, geocoder, crypto ─────────────────────────────────────────────
def test_open_in_uber_link_and_which_links_the_chat_may_show():
    link = uber_ride_link(LinkPlace("Indiranagar", 12.9733, 77.6405), LinkPlace("Airport"), "cid")
    u = urlparse(link)
    q = parse_qs(u.query)
    assert u.scheme == "https" and u.hostname == "m.uber.com" and u.path == "/ul/"
    assert q["action"] == ["setPickup"] and q["client_id"] == ["cid"]
    assert q["pickup[latitude]"] == ["12.973300"] and q["pickup[nickname]"] == ["Indiranagar"]
    assert "dropoff[latitude]" not in q and q["dropoff[formatted_address]"] == ["Airport"]
    assert classify_result_link(link) == "uber"
    assert classify_result_link("/settings") == "internal"
    for bad in ("//evil.test/x", "http://m.uber.com/ul/", "https://m.uber.com.evil.test/", "javascript:alert(1)", "", None):
        assert classify_result_link(bad) is None


async def test_geocoder_caches_limits_to_countries_and_the_link_gets_coordinates(client, uber, monkeypatch):
    monkeypatch.setenv("GEOCODER_COUNTRIES", "in")
    link = await uber_link_for(ROUTE["pickup"], ROUTE["drop"])
    assert "pickup%5Blatitude%5D=12.973300" in link and "dropoff%5Blatitude%5D=13.198600" in link
    assert await geocode.geocode("indiranagar, bengaluru ") is not None
    assert uber.geocoded == [ROUTE["pickup"], ROUTE["drop"]]           # the third was cached
    assert uber.geo_countries == ["in", "in"]
    assert await geocode.geocode("Nowhere land") is None
    monkeypatch.setenv("GEOCODER_URL", "")
    geocode.reset_cache()
    assert await geocode.geocode(ROUTE["pickup"]) is None               # off without GEOCODER_URL
    assert "latitude" not in await uber_link_for(ROUTE["pickup"], ROUTE["drop"])


def test_secrets_are_encrypted_with_aes_gcm(monkeypatch):
    import base64
    import os
    monkeypatch.setenv("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    box = encrypt("hello")
    assert box.startswith("v1.") and len(box.split(".")) == 4 and "=" not in box and box != encrypt("hello")
    assert decrypt(box) == "hello"
    v, iv, tag, ct = box.split(".")
    with pytest.raises(CryptoError):
        decrypt(".".join([v, iv, tag, ("A" if ct[0] != "A" else "B") + ct[1:]]))
    monkeypatch.setenv("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(CryptoError, match="wrong ENCRYPTION_KEY"):
        decrypt(box)
    monkeypatch.setenv("ENCRYPTION_KEY", "short")
    with pytest.raises(CryptoError, match="32 random bytes"):
        encrypt("x")
