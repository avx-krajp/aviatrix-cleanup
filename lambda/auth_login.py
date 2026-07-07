"""
auth_login.py — Login + logout endpoints
  POST /api/login   { "password": "..." } -> 200 + Set-Cookie cm_auth=<signed token>
  POST /api/logout                         -> 200 + Set-Cookie cm_auth= (cleared)

The cookie is HMAC-SHA256 signed with AUTH_SIGNING_KEY. The CloudFront
Function (for static files) and the auth_authorizer Lambda (for /api/cleanup*)
both verify the same signature.
"""

import base64
import hashlib
import hmac
import json
import os
import time

LOGIN_PASSWORD     = os.environ.get("LOGIN_PASSWORD", "")
AUTH_SIGNING_KEY   = os.environ.get("AUTH_SIGNING_KEY", "")
SESSION_TTL_SECS   = int(os.environ.get("SESSION_TTL_SECS", "28800"))   # 8 hours
COOKIE_NAME        = "cm_auth"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign(payload_b64: str) -> str:
    sig = hmac.new(AUTH_SIGNING_KEY.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return _b64url(sig)


def _make_token(now: int) -> str:
    payload = json.dumps({"exp": now + SESSION_TTL_SECS}, separators=(",", ":"))
    payload_b64 = _b64url(payload.encode())
    return f"{payload_b64}.{_sign(payload_b64)}"


def _resp(status: int, body: dict, set_cookie: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    out = {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(body),
    }
    if set_cookie:
        out["cookies"] = [set_cookie]
    return out


def _cookie(value: str, max_age: int) -> str:
    return (
        f"{COOKIE_NAME}={value}; "
        f"Max-Age={max_age}; "
        "Path=/; HttpOnly; Secure; SameSite=Strict"
    )


def handler(event: dict, _context) -> dict:
    route = event.get("routeKey", "")
    http_ctx = (event.get("requestContext") or {}).get("http") or {}
    path = event.get("rawPath") or http_ctx.get("path", "")

    is_login  = route == "POST /api/login"  or path.endswith("/api/login")
    is_logout = route == "POST /api/logout" or path.endswith("/api/logout")

    if is_logout:
        return _resp(200, {"ok": True}, set_cookie=_cookie("", 0))

    if not is_login:
        return _resp(404, {"error": f"No route for {route or path}"})

    if not LOGIN_PASSWORD or not AUTH_SIGNING_KEY:
        return _resp(500, {"error": "Auth not configured (LOGIN_PASSWORD or AUTH_SIGNING_KEY missing)"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "Invalid JSON"})

    submitted = (body.get("password") or "")

    # Constant-time comparison to avoid timing attacks
    if not hmac.compare_digest(submitted.encode(), LOGIN_PASSWORD.encode()):
        return _resp(401, {"error": "Incorrect passphrase"})

    token = _make_token(int(time.time()))
    return _resp(200, {"ok": True}, set_cookie=_cookie(token, SESSION_TTL_SECS))
