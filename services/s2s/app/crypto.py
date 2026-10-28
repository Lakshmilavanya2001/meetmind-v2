"""AES-256-GCM for secrets at rest (e.g. Uber tokens), like meetmind's src/server/crypto.ts.

Format: "v1.<iv>.<auth tag>.<ciphertext>", each base64url without padding (the same as meetmind's, so
values written by one can be read by the other with the same key). The key is ENCRYPTION_KEY:
32 random bytes, base64-encoded (openssl rand -base64 32).
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CryptoError(Exception):
    pass


def _key() -> bytes:
    raw = os.environ.get("ENCRYPTION_KEY", "")
    try:
        k = base64.b64decode(raw, validate=False) if raw else b""
    except (binascii.Error, ValueError):
        k = b""
    if len(k) != 32:
        raise CryptoError("ENCRYPTION_KEY must be 32 random bytes, base64-encoded (openssl rand -base64 32).")
    return k


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encrypt(plaintext: str) -> str:
    iv = os.urandom(12)
    sealed = AESGCM(_key()).encrypt(iv, plaintext.encode(), None)     # ciphertext + 16-byte tag
    ct, tag = sealed[:-16], sealed[-16:]
    return ".".join(["v1", _b64(iv), _b64(tag), _b64(ct)])


def decrypt(payload: str) -> str:
    parts = payload.split(".")
    if len(parts) != 4 or parts[0] != "v1" or not all(parts[1:3]):
        raise CryptoError("Unrecognised encrypted secret format.")
    try:
        iv, tag, ct = (_unb64(p) for p in parts[1:])
        return AESGCM(_key()).decrypt(iv, ct + tag, None).decode()
    except (InvalidTag, binascii.Error, ValueError):
        raise CryptoError("Could not decrypt secret (wrong ENCRYPTION_KEY or tampered data).") from None


def encrypt_json(value: Any) -> str:
    return encrypt(json.dumps(value))


def decrypt_json(payload: str) -> Any:
    return json.loads(decrypt(payload))
