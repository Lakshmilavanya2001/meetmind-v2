"""Bookings (sandbox provider): restaurants and food orders, cabs, tables, movie tickets, listing and
cancelling, grounding of places, and the /api/bookings routes."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import other, signup
from sqlalchemy import text, update

from app import db
from app.assistant.tools.base import ToolContext, ToolError
from app.assistant.tools.bookings import TOOLS
from app.commerce import service as bk
from app.commerce.catalog import sandbox_commerce
from app.commerce.service import Booking

T = {t.name: t for t in TOOLS}
IST = ZoneInfo("Asia/Kolkata")


def _install_router():
    """web.py doesn't include the bookings router yet: put its routes ahead of the static mount."""
    from app.commerce.api import router
    from app.web import app
    if not any(getattr(r, "path", None) == "/api/bookings" for r in app.router.routes):
        for r in reversed(router.routes):
            app.router.routes.insert(0, r)


_install_router()


async def _ctx(c, name, email, said=(), org="Acme"):
    """Sign up, open a conversation where the user said `said`, and return a ToolContext."""
    me = await signup(c, name, email, org=org)
    uid, oid = uuid.UUID(me["user"]["id"]), uuid.UUID(me["organization"]["id"])
    s = db.sessionmaker()()
    conv = (await s.execute(text("INSERT INTO agent_conversations (organization_id, user_id, title) VALUES (:o, :u, 'Chat') RETURNING id"),
                            {"o": oid, "u": uid})).scalar_one()
    for line in said:
        await s.execute(text("INSERT INTO agent_messages (conversation_id, role, content) VALUES (:c, 'USER', :t)"), {"c": conv, "t": line})
    await s.commit()
    return ToolContext(s=s, user_id=uid, org_id=oid, user_name=name, user_email=email, timezone="Asia/Kolkata", role="OWNER",
                       conversation_id=conv, meeting_code=None, now=datetime.now(timezone.utc))


def _clone(t: ToolContext) -> ToolContext:
    """The same person and conversation on a fresh database session."""
    return ToolContext(**{**t.__dict__, "s": db.sessionmaker()(), "now": datetime.now(timezone.utc)})


async def call(t, name, **args):
    tool = T[name]
    a = tool.args.model_validate(args)
    if tool.preflight:
        await tool.preflight(t, a)
    return await tool.run(t, a)


def _tomorrow() -> str:
    return (datetime.now(IST) + timedelta(days=1)).strftime("%Y-%m-%d")


def test_rupees_and_catalogue():
    assert bk.rupees(2000000) == "₹20,000" and bk.rupees(12345678) == "₹1,23,456.78" and bk.rupees(29950) == "₹299.50"
    assert bk.rupees(4000) == "₹40"
    assert {r.id for r in sandbox_commerce.search_restaurants("pizza")} == {"r-pasta-street", "r-slice-republic"}
    assert [r.id for r in sandbox_commerce.search_restaurants(cuisine="south indian")] == ["r-dosa-corner"]
    assert "r-coastal-curry" not in {r.id for r in sandbox_commerce.search_restaurants(mode="delivery")}
    assert "r-biryani-house" not in {r.id for r in sandbox_commerce.search_restaurants(mode="dine_in")}
    assert [r.id for r in sandbox_commerce.search_restaurants("koramangala dosa")] == ["r-dosa-corner"]


async def test_search_and_menu_tools(client):
    t = await _ctx(client, "Asha Rao", "asha@acme.test")
    out = await call(t, "search_restaurants", query="biryani")
    assert out.data["sandbox"] is True and out.data["restaurants"][0]["name"] == "Hyderabad Biryani House"
    assert out.data["restaurants"][0]["delivers"] and not out.data["restaurants"][0]["takesReservations"]
    assert "Sandbox" in T["search_restaurants"].description
    menu = await call(t, "get_menu", restaurant_id="r-dosa-corner")
    assert menu.data["items"][0] == {"id": "m-masala-dosa", "name": "Masala Dosa", "price": "₹120", "veg": True}
    with pytest.raises(ToolError) as e:
        await call(t, "get_menu", restaurant_id="r-nope")
    assert e.value.code == "NOT_FOUND"
    await t.s.close()


async def test_food_quote_limit_and_order(client):
    t = await _ctx(client, "Asha Rao", "asha@acme.test", said=["Order 2 masala dosa and a filter coffee to 12th Main, Indiranagar"])
    q = bk.quote_food_order("r-dosa-corner", [("m-masala-dosa", 1), ("m-filter-coffee", 1), ("m-masala-dosa", 1)], "Indiranagar")
    assert [(l.name, l.quantity) for l in q.lines] == [("Masala Dosa", 2), ("Filter Coffee", 1)]
    assert (q.subtotal_paise, q.taxes_paise, q.delivery_fee_paise, q.total_paise) == (28000, 1400, 2500, 31900)
    with pytest.raises(ToolError, match="above the ₹20,000 limit"):
        bk.quote_food_order("r-pasta-street", [("m-margherita", 20), ("m-arrabbiata", 20), ("m-tiramisu", 20)], "MG Road")
    with pytest.raises(ToolError, match="doesn't deliver"):
        bk.quote_food_order("r-coastal-curry", [("m-fish-curry", 1)], "Jayanagar")
    with pytest.raises(ToolError, match="isn't on Dosa Corner's menu"):
        bk.quote_food_order("r-dosa-corner", [("m-tiramisu", 1)], "Jayanagar")

    args = dict(restaurant_id="r-dosa-corner", items=[{"item_id": "m-masala-dosa", "quantity": 2}, {"item_id": "m-filter-coffee", "quantity": 1}],
                delivery_address="12th Main, Indiranagar")
    a = T["order_food"].args.model_validate(args)
    sentence = await T["order_food"].preflight(t, a)
    assert sentence.startswith("Order from Dosa Corner: 2× Masala Dosa (₹240), 1× Filter Coffee (₹40).")
    assert "total ₹319" in sentence and sentence.endswith(bk.SANDBOX_NOTE)
    out = await T["order_food"].run(t, a)
    assert out.data["total"] == "₹319" and out.data["reference"].startswith("FD-") and out.data["note"] == bk.SANDBOX_NOTE
    assert out.data["etaMinutes"] == 25 and out.link == "/bookings"
    b = (await t.s.execute(Booking.__table__.select())).one()
    assert b.kind == "FOOD" and b.status == "CONFIRMED" and b.total_paise == 31900 and b.details["address"] == "12th Main, Indiranagar"
    await t.s.close()


async def test_ungrounded_places_are_refused(client):
    t = await _ctx(client, "Asha Rao", "asha@acme.test", said=["Get me a cab from Koramangala 5th Block to the airport", "deliver to Indiranagar"])
    with pytest.raises(ToolError) as e:
        await call(t, "order_food", restaurant_id="r-dosa-corner", items=[{"item_id": "m-masala-dosa", "quantity": 1}],
                   delivery_address="42 Park Avenue, Mumbai")
    assert e.value.details == {"reason": "UNGROUNDED_PLACE"} and "delivery address" in e.value.message
    with pytest.raises(ToolError, match="drop-off point"):
        await call(t, "book_cab", pickup="Koramangala 5th Block", drop="Kempegowda International Terminal 2", cab_type="MINI")
    # what the user said is fine
    assert (await call(t, "book_cab", pickup="Koramangala 5th Block", drop="the airport", cab_type="MINI")).data["reference"].startswith("CB-")
    await t.s.close()


async def test_cab_options_deterministic_and_booking(client):
    t = await _ctx(client, "Asha Rao", "asha@acme.test", said=["cab from Indiranagar to Whitefield please"])
    a = await call(t, "get_cab_options", pickup="Indiranagar", drop="Whitefield")
    b = await call(t, "get_cab_options", pickup="whitefield ", drop="INDIRANAGAR")
    assert a.data == b.data and [o["type"] for o in a.data["options"]] == ["AUTO", "MINI", "SEDAN", "SUV"]
    km = a.data["options"][0]["distanceKm"]
    assert 3 <= km <= 25 and all(o["distanceKm"] == km for o in a.data["options"])
    opts = bk.cab_options("Indiranagar", "Whitefield")
    assert all(o.fare_paise % 100 == 0 for o in opts) and opts[0].fare_paise < opts[3].fare_paise
    with pytest.raises(ToolError, match="same place"):
        bk.cab_options("Indiranagar", " indiranagar")

    args = T["book_cab"].args.model_validate({"pickup": "Indiranagar", "drop": "Whitefield", "cab_type": "SEDAN"})
    sentence = await T["book_cab"].preflight(t, args)
    assert sentence.startswith("Book a Sedan from “Indiranagar” to “Whitefield”, pickup now (driver ~") and bk.SANDBOX_NOTE in sentence
    out = await T["book_cab"].run(t, args)
    assert out.data["fare"] == bk.rupees(opts[2].fare_paise) and "sandbox driver" in out.data["driver"]
    late = T["book_cab"].args.model_validate({"pickup": "Indiranagar", "drop": "Whitefield", "cab_type": "AUTO",
                                              "pickup_time": (t.now + timedelta(days=8)).isoformat()})
    with pytest.raises(ToolError, match="up to 7 days"):
        await T["book_cab"].preflight(t, late)
    past = late.model_copy(update={"pickup_time": t.now - timedelta(hours=1)})
    with pytest.raises(ToolError, match="already passed"):
        await T["book_cab"].preflight(t, past)
    await t.s.close()


async def _free_slot(t, restaurant_id, date):
    out = await call(t, "find_table_slots", restaurant_id=restaurant_id, date=date, party_size=2)
    return [s["start_time"] for s in out.data["slots"] if s["available"]], out


async def test_table_slots_fill_up(client):
    t = await _ctx(client, "Asha Rao", "asha@acme.test")
    free, out = await _free_slot(t, "r-green-bowl", _tomorrow())                       # 3 tables per slot
    assert out.data["timezone"] == "Asia/Kolkata" and len(out.data["slots"]) == 12
    slot = free[0]
    args = dict(restaurant_id="r-green-bowl", start_time=slot, party_size=2)
    a = T["reserve_table"].args.model_validate(args)
    sentence = await T["reserve_table"].preflight(t, a)
    assert sentence.startswith("Reserve a table for 2 at The Green Bowl (Whitefield) on ") and sentence.endswith(bk.SANDBOX_NOTE)
    for _ in range(3):
        r = await call(t, "reserve_table", **args)
        assert r.data["reference"].startswith("TB-")
    with pytest.raises(ToolError) as e:
        await call(t, "reserve_table", **args)
    assert e.value.code == "CONFLICT" and "is full at" in e.value.message
    free_after, _ = await _free_slot(t, "r-green-bowl", _tomorrow())
    assert slot not in free_after
    # Delivery-only restaurants and unknown times
    with pytest.raises(ToolError, match="delivery-only"):
        await call(t, "find_table_slots", restaurant_id="r-slice-republic", date=_tomorrow(), party_size=2)
    with pytest.raises(ToolError, match="no seating at that time"):
        await call(t, "reserve_table", restaurant_id="r-green-bowl", start_time=f"{_tomorrow()}T16:00:00+05:30", party_size=2)
    await t.s.close()


async def test_no_double_booking_under_concurrency(client):
    t = await _ctx(client, "Asha Rao", "asha@acme.test")
    date = (datetime.now(IST) + timedelta(days=2)).strftime("%Y-%m-%d")
    slot = (await _free_slot(t, "r-green-bowl", date))[0][0]
    tool = T["reserve_table"]
    a = tool.args.model_validate(dict(restaurant_id="r-green-bowl", start_time=slot, party_size=4))
    ctxs = [_clone(t) for _ in range(6)]
    results = await asyncio.gather(*(tool.run(c, a) for c in ctxs), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, ToolError)]
    assert len(ok) == 3 and len(refused) == 3 and all(r.code == "CONFLICT" for r in refused)
    n = (await t.s.execute(text("SELECT count(*) FROM bookings WHERE kind='TABLE' AND status='CONFIRMED'"))).scalar_one()
    assert n == 3
    for c in ctxs:
        await c.s.close()
    await t.s.close()


async def test_tables_need_30_minutes_notice(client):
    t = await _ctx(client, "Asha Rao", "asha@acme.test")
    slot = (await _free_slot(t, "r-dosa-corner", _tomorrow()))[0][0]
    t.now = datetime.fromisoformat(slot) - timedelta(minutes=10)
    with pytest.raises(ToolError, match="at least 30 minutes ahead"):
        await call(t, "reserve_table", restaurant_id="r-dosa-corner", start_time=slot, party_size=2)
    out = await call(t, "find_table_slots", restaurant_id="r-dosa-corner", date=_tomorrow(), party_size=2)
    assert slot not in [s["start_time"] for s in out.data["slots"]]                    # too soon to be offered
    await t.s.close()


async def _a_show(t):
    for m in (await call(t, "search_movies")).data["films"]:
        shows = (await call(t, "get_showtimes", movie_id=m["id"], date=_tomorrow())).data["shows"]
        if shows:
            return shows[0]["show_id"]
    raise AssertionError("no shows tomorrow")


async def test_movies_seat_map_and_booking(client):
    t = await _ctx(client, "Asha Rao", "asha@acme.test")
    films = (await call(t, "search_movies", language="tamil")).data["films"]
    assert [f["title"] for f in films] == ["Kadhal Paadhai"] and films[0]["runtime"] == "2h 21m"
    show = await _a_show(t)
    m = await call(t, "get_seat_map", show_id=show, party_size=3)
    seats = m.data["suggestedSeats"]
    assert len(seats) == 3 and {c["name"] for c in m.data["categories"]} == {"Classic", "Prime", "Recliner"}
    free_before = m.data["freeSeats"]
    a = T["book_movie_tickets"].args.model_validate({"show_id": show, "seats": [s.lower() for s in seats]})
    sentence = await T["book_movie_tickets"].preflight(t, a)
    assert sentence.startswith("Book 3 tickets for ") and f"seats {', '.join(seats)}" in sentence and sentence.endswith(bk.SANDBOX_NOTE)
    out = await T["book_movie_tickets"].run(t, a)
    assert out.data["seats"] == seats and out.data["reference"].startswith("MV-")
    after = await call(t, "get_seat_map", show_id=show, party_size=3)
    assert after.data["freeSeats"] == free_before - 3
    # The same seats again, a seat sold to "others", and a seat that doesn't exist
    with pytest.raises(ToolError) as e:
        await call(t, "book_movie_tickets", show_id=show, seats=[seats[0]])
    assert e.value.code == "CONFLICT" and "already taken" in e.value.message
    sold = next(s for s in (await bk.movie_seat_map(t.s, show))["taken"] if s not in seats)
    with pytest.raises(ToolError, match="already taken"):
        await call(t, "book_movie_tickets", show_id=show, seats=[sold])
    with pytest.raises(ToolError, match="isn't a seat"):
        await call(t, "book_movie_tickets", show_id=show, seats=["K1"])
    with pytest.raises(ToolError, match="Unknown show"):
        await call(t, "get_seat_map", show_id="cn-nowhere~mv-x~20260101~1030")
    await t.s.close()


async def test_list_only_own_and_cancel_rules(client):
    t = await _ctx(client, "Asha Rao", "asha@acme.test", said=["deliver to Indiranagar", "cab from Indiranagar to MG Road"])
    zed = other(client)
    z = await _ctx(zed, "Zed Other", "zed@globex.test", said=["deliver to Jayanagar"], org="Globex")
    food = lambda: call(t, "order_food", restaurant_id="r-dosa-corner", items=[{"item_id": "m-idli-vada", "quantity": 1}],  # noqa: E731
                        delivery_address="Indiranagar")
    await food()
    await food()
    await call(t, "book_cab", pickup="Indiranagar", drop="MG Road", cab_type="AUTO")
    await call(z, "order_food", restaurant_id="r-dosa-corner", items=[{"item_id": "m-pongal", "quantity": 1}], delivery_address="Jayanagar")
    mine = (await call(t, "list_bookings")).data["bookings"]
    assert len(mine) == 3 and {b["kind"] for b in mine} == {"FOOD", "CAB"} and all(b["sandbox"] for b in mine)
    assert len((await call(z, "list_bookings")).data["bookings"]) == 1
    cab = next(b for b in mine if b["kind"] == "CAB")
    f1, f2 = [b for b in mine if b["kind"] == "FOOD"]

    # Food within 5 minutes: fine; after 5 minutes: refused.
    sentence = await T["cancel_booking"].preflight(t, T["cancel_booking"].args.model_validate({"booking_id": f1["id"]}))
    assert sentence == f"Cancel Order from Dosa Corner (ref {f1['reference']}). Sandbox booking."
    assert (await call(t, "cancel_booking", booking_id=f1["id"])).data["canceled"] is True
    with pytest.raises(ToolError, match="already cancelled"):
        await call(t, "cancel_booking", booking_id=f1["id"])
    await t.s.execute(update(Booking).where(Booking.id == uuid.UUID(f2["id"])).values(created_at=datetime.now(timezone.utc) - timedelta(minutes=6)))
    await t.s.commit()
    with pytest.raises(ToolError, match="within 5 minutes"):
        await call(t, "cancel_booking", booking_id=f2["id"])
    # A booking whose time has passed
    await t.s.execute(update(Booking).where(Booking.id == uuid.UUID(cab["id"])).values(scheduled_for=datetime.now(timezone.utc) - timedelta(hours=1)))
    await t.s.commit()
    with pytest.raises(ToolError, match="already passed"):
        await call(t, "cancel_booking", booking_id=cab["id"])
    # Someone else's booking, or no booking at all
    for bid in (f2["id"], "not-a-uuid"):
        with pytest.raises(ToolError) as e:
            await call(z, "cancel_booking", booking_id=bid)
        assert e.value.code == "NOT_FOUND"
    statuses = {b["id"]: b["status"] for b in (await call(t, "list_bookings")).data["bookings"]}
    assert statuses[f1["id"]] == "CANCELED" and statuses[f2["id"]] == "CONFIRMED"
    await t.s.close()
    await z.s.close()
    await zed.aclose()


async def test_api_routes(client):
    anon = other(client)
    assert (await anon.get("/api/bookings")).status_code == 401
    t = await _ctx(client, "Asha Rao", "asha@acme.test")
    date = _tomorrow()
    slot = (await _free_slot(t, "r-spice-route", date))[0][0]
    booked = await call(t, "reserve_table", restaurant_id="r-spice-route", start_time=slot, party_size=2)
    assert booked.data["title"] == "Table for 2 at Spice Route Kitchen"
    r = await client.get("/api/bookings")
    assert r.status_code == 200
    [b] = r.json()["bookings"]
    assert b["kind"] == "TABLE" and b["sandbox"] and b["status"] == "CONFIRMED" and b["when"]
    assert (await anon.post(f"/api/bookings/{b['id']}/cancel")).status_code == 401
    zed = other(client)
    await signup(zed, "Zed Other", "zed@globex.test", org="Globex")
    assert (await zed.get("/api/bookings")).json() == {"bookings": []}
    assert (await zed.post(f"/api/bookings/{b['id']}/cancel")).status_code == 404
    r = await client.post(f"/api/bookings/{b['id']}/cancel")
    assert r.status_code == 200 and r.json()["canceled"] is True
    assert (await client.post(f"/api/bookings/{b['id']}/cancel")).status_code == 400
    assert (await client.get("/api/bookings")).json()["bookings"][0]["status"] == "CANCELED"
    # the sandbox partners' view
    assert (await client.get("/api/partners")).json()["cinemas"][0]["id"] == "cn-galaxy-kor"
    assert (await client.get("/api/partners/food?restaurant=r-dosa-corner")).json()["orders"] == []
    assert (await client.get("/api/partners/food?restaurant=nope")).status_code == 404
    await t.s.close()
    await anon.aclose()
    await zed.aclose()
