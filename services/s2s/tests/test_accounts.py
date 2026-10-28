"""Accounts and organisations: sign up, sign in and out, sessions, organisation boundaries."""
from conftest import other, signup


async def test_signup_creates_an_org_and_the_owner_sees_its_join_code(client):
    r = await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    assert r["role"] == "OWNER"
    me = (await client.get("/api/me")).json()
    assert me["user"]["email"] == "asha@acme.test" and me["organization"]["name"] == "Acme"
    assert me["organization"]["joinCode"]


async def test_people_join_with_the_code_as_members_and_dont_see_it(client):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    code = (await client.get("/api/me")).json()["organization"]["joinCode"]
    async with other(client) as ben:
        r = await signup(ben, "Ben Das", "ben@acme.test", join_code=code)
        assert r["role"] == "MEMBER"
        assert "joinCode" not in (await ben.get("/api/me")).json()["organization"]
    names = [m["displayName"] for m in (await client.get("/api/org/members")).json()["members"]]
    assert names == ["Asha Rao", "Ben Das"]


async def test_signup_rules(client):
    base = {"name": "X", "email": "x@acme.test", "password": "correct-horse-1"}
    assert (await client.post("/api/auth/signup", json=base)).status_code == 400                              # no org choice
    assert (await client.post("/api/auth/signup", json={**base, "organization": "A", "join_code": "b"})).status_code == 400
    assert (await client.post("/api/auth/signup", json={**base, "join_code": "nope"})).status_code == 400
    assert (await client.post("/api/auth/signup", json={**base, "password": "short", "organization": "A"})).status_code == 422
    assert (await client.post("/api/auth/signup", json={**base, "email": "not-an-email", "organization": "A"})).status_code == 422
    await signup(client, "X", "x@acme.test", org="A")
    async with other(client) as again:
        r = await again.post("/api/auth/signup", json={**base, "email": "X@ACME.test", "organization": "B"})
        assert r.status_code == 409                                                                           # emails are case-insensitive


async def test_login_logout_and_sessions(client):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    async with other(client) as b:
        assert (await b.get("/api/me")).status_code == 401
        assert (await b.post("/api/auth/login", json={"email": "asha@acme.test", "password": "wrong-password"})).status_code == 401
        assert (await b.post("/api/auth/login", json={"email": "nobody@acme.test", "password": "x"})).status_code == 401
        r = await b.post("/api/auth/login", json={"email": "ASHA@acme.test", "password": "correct-horse-1"})
        assert r.status_code == 200
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
        assert (await b.get("/api/me")).status_code == 200
        assert (await b.post("/api/auth/logout")).status_code == 200
        assert (await b.get("/api/me")).status_code == 401
    assert (await client.get("/api/me")).status_code == 200                          # the other session is untouched


async def test_login_is_rate_limited(client):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    codes = [(await client.post("/api/auth/login", json={"email": "asha@acme.test", "password": "nope"})).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


async def test_cross_site_requests_are_refused(client):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    r = await client.post("/api/meetings", json={"title": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


async def test_members_are_only_your_organisation(client):
    await signup(client, "Asha Rao", "asha@acme.test", org="Acme")
    async with other(client) as z:
        await signup(z, "Zed Other", "zed@globex.test", org="Globex")
        assert [m["displayName"] for m in (await z.get("/api/org/members")).json()["members"]] == ["Zed Other"]
