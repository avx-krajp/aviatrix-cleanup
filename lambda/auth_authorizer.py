"""
auth_authorizer.py — HTTP API Lambda authorizer (simple response format).

Verifies the cm_auth cookie's HMAC signature and expiry. Used to gate
/api/cleanup and /api/cleanup/status. Uses the same signing key the
login Lambda uses to issue the cookie.

Returns { "isAuthorized": True/False }.
"""

import base64
import hashlib
import hmac
import json
import os
import time

AUTH_SIGNING_KEY = os.environ.get("AUTH_SIGNING_KEY", "")
COOKIE_NAME      = "cm_auth"


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _verify(token: str) -> bool:
    if not AUTH_SIGNING_KEY or not token or "." not in token:
        return False
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        expected = hmac.new(AUTH_SIGNING_KEY.encode(), payload_b64.encode(),
                            hashlib.sha256).digest()
        provided = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected, provided):
            return False
        payload = json.loads(_b64url_decode(payload_b64))
        return int(payload.get("exp", 0)) > int(time.time())
    except Exception:
        return False


def _extract_cookie(event: dict) -> str:
    # HTTP API v2 normalizes cookies into a list of "k=v" strings.
    for raw in event.get("cookies") or []:
        if raw.startswith(COOKIE_NAME + "="):
            return raw[len(COOKIE_NAME) + 1:]
    # Fallback: parse the Cookie header if present.
    headers = event.get("headers") or {}
    raw = headers.get("cookie") or headers.get("Cookie") or ""
    for piece in raw.split(";"):
        piece = piece.strip()
        if piece.startswith(COOKIE_NAME + "="):
            return piece[len(COOKIE_NAME) + 1:]
    return ""


def handler(event: dict, _context) -> dict:
    token = _extract_cookie(event)
    return {"isAuthorized": _verify(token)}
