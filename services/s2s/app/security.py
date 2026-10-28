"""Passwords, session tokens, rate limits and the cross-site request check."""
from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request

_hasher = PasswordHasher()                        # argon2id with the library's current defaults
# Verifying against this when the email is unknown keeps login time the same either way.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

SESSION_COOKIE = "mm2_session"
SESSION_DAYS = 30


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_token() -> tuple[str, str]:
    """(cookie value, the sha256 stored in the database)."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_buckets: dict[str, list[float]] = defaultdict(list)


def rate_limit(key: str, limit: int, window_s: float):
    """At most `limit` calls per `window_s` for this key, else 429 (in memory: one web process)."""
    now = time.time()
    hits = [t for t in _buckets[key] if now - t < window_s]
    if len(hits) >= limit:
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
    hits.append(now)
    _buckets[key] = hits
    if len(_buckets) > 50_000:                    # bounded memory
        _buckets.clear()


def reset_rate_limits():
    _buckets.clear()


def client_ip(request: Request) -> str:
    # The HTTPS proxy on this host is the only way in; it sets X-Forwarded-For.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "?")


def assert_same_origin(request: Request):
    """Reject state-changing requests from other sites (on top of SameSite=Lax cookies)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if not origin:
        return                                    # non-browser clients
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if origin.split("://", 1)[-1].rstrip("/") != host:
        raise HTTPException(403, "Cross-origin request rejected.")
