"""A stand-in for Uber (auth + Riders API sandbox) and the geocoder, answering as they do (meetmind's
tests/fake-uber.ts). A real aiohttp server on 127.0.0.1: /auth is Uber's sign-in server, /api the Riders API,
/geo a Nominatim-style geocoder. `start()` points the app at it through the environment."""
from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field

from aiohttp import web

REDIRECT_URI = "https://meet.test/api/integrations/uber/callback"


@dataclass
class FakeUber:
    fare: float = 250
    rides: dict[str, dict] = field(default_factory=dict)
    tokens: list[str] = field(default_factory=list)      # Authorization headers seen
    refreshed: int = 0
    revoked: int = 0
    expires_in: int = 3600
    fail_token: bool = False
    geocoded: list[str] = field(default_factory=list)    # places looked up
    geo_countries: list[str] = field(default_factory=list)
    runner: web.AppRunner | None = None
    base: str = ""

    def _json(self, body, status=200):
        return web.json_response(body, status=status)

    async def geo(self, req: web.Request):
        q = req.query.get("q", "")
        self.geocoded.append(q)
        self.geo_countries.append(req.query.get("countrycodes", ""))
        if re.search("nowhere", q, re.I):
            return self._json([])
        airport = "Airport" in q
        return self._json([{"lat": "13.1986" if airport else "12.9733", "lon": "77.7066" if airport else "77.6405", "display_name": q}])

    async def revoke(self, req: web.Request):
        self.revoked += 1
        return self._json({})

    async def token(self, req: web.Request):
        body = await req.post()
        if body.get("client_secret") != "s3cret":
            return self._json({"error": "invalid_client"}, 401)
        if body.get("grant_type") == "authorization_code":
            if body.get("code") != "good-code" or body.get("redirect_uri") != REDIRECT_URI:
                return self._json({"error": "invalid_grant"}, 400)
            return self._json({"access_token": "A1", "refresh_token": "R1", "expires_in": self.expires_in,
                               "scope": "ride_request.estimate ride_request.ride_booking"})
        if body.get("grant_type") == "refresh_token":
            if self.fail_token:
                return self._json({"error": "invalid_grant"}, 400)
            self.refreshed += 1
            return self._json({"access_token": "A2", "refresh_token": "R2", "expires_in": 3600})
        return self._json({"error": "unsupported_grant_type"}, 400)

    async def api(self, req: web.Request):
        auth = req.headers.get("authorization", "")
        self.tokens.append(auth)
        if not auth.startswith("Bearer A"):
            return self._json({"message": "Invalid OAuth 2.0 credentials"}, 401)
        p, method = "/" + req.match_info["path"], req.method
        if p == "/v1.2/products":
            return self._json({"products": [{"product_id": "go", "display_name": "UberGo", "capacity": 4},
                                            {"product_id": "premier", "display_name": "Premier", "capacity": 4}]})
        if p == "/v1.2/estimates/price":
            return self._json({"prices": [
                {"product_id": "go", "display_name": "UberGo", "estimate": "₹240-290", "duration": 2400, "distance": 21.4},
                {"product_id": "premier", "display_name": "Premier", "estimate": "₹380-450", "duration": 2400, "distance": 21.4}]})
        if p == "/v1.2/estimates/time":
            return self._json({"times": [{"product_id": "go", "estimate": 240}]})
        if p == "/v1.2/requests/estimate":
            f = self.fare
            return self._json({"fare": {"fare_id": f"fare-{f:g}", "value": f, "display": f"₹{f:g}", "currency_code": "INR"},
                               "trip": {"duration_estimate": 2400}, "pickup_estimate": 4})
        if p == "/v1.2/requests" and method == "POST":
            rid = f"req-{len(self.rides) + 1}-abcdef"
            self.rides[rid] = {"status": "processing", "body": await req.json()}
            return self._json({"request_id": rid, "status": "processing"}, 202)
        m = re.fullmatch(r"/v1\.2/requests/([^/]+)", p)
        if m:
            r = self.rides.get(m[1])
            if r is None:
                return self._json({"message": "not found"}, 404)
            if method == "DELETE":
                r["status"] = "rider_canceled"
                return web.Response(status=204)
            assigned = r["status"] not in ("processing", "no_drivers_available", "rider_canceled")
            return self._json({"request_id": m[1], "status": r["status"],
                               "driver": {"name": "Ravi", "rating": 4.9} if assigned else None,
                               "vehicle": {"make": "Maruti", "model": "Dzire", "license_plate": "KA01AB1234"} if assigned else None,
                               "pickup": {"eta": 3 if assigned else None}})
        m = re.fullmatch(r"/v1/sandbox/requests/([^/]+)", p)
        if m and method == "PUT":
            self.rides[m[1]]["status"] = (await req.json())["status"]
            return web.Response(status=204)
        return self._json({"message": f"unexpected {method} {p}"}, 500)

    async def start(self, monkeypatch):
        app = web.Application()
        app.router.add_get("/geo/search", self.geo)
        app.router.add_post("/auth/oauth/v2/token", self.token)
        app.router.add_post("/auth/oauth/v2/revoke", self.revoke)
        app.router.add_route("*", "/api/{path:.*}", self.api)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        for k, v in {"UBER_CLIENT_ID": "cid", "UBER_CLIENT_SECRET": "s3cret", "UBER_ENV": "sandbox",
                     "UBER_API_URL": f"{self.base}/api", "UBER_AUTH_URL": f"{self.base}/auth",
                     "GEOCODER_URL": f"{self.base}/geo", "PUBLIC_URL": "https://meet.test",
                     "ENCRYPTION_KEY": base64.b64encode(os.urandom(32)).decode()}.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("UBER_REDIRECT_URI", raising=False)
        return self

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

