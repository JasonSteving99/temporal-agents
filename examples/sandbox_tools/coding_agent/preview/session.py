"""
The gate: signed session cookies, and the predicates every request is checked
against.

Firebase's job ends at sign-in — it hands the browser an ID token, we verify it
once (auth.py), and then we mint our OWN cookie so that steady-state requests
cost no network calls. This module is that cookie: an HMAC-signed
`<payload>.<signature>` pair carrying the email and an expiry.

The payload is readable by anyone — base64 is not secrecy. The signature is what
matters: without PREVIEW_SESSION_SECRET a visitor could simply type a cookie
claiming to be you. That key is also why there's no session database; the cookie
carries its own state, and the signature is what makes that state trustworthy.
"""

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode, urlsplit

from aiohttp import web

from .allowlist import allowlist
from .config import (
    ADMIN_EMAILS,
    AUTH_ENABLED,
    AUTH_HOST,
    COOKIE_NAME,
    PREVIEW_BASE_DOMAIN,
    SESSION_SECRET,
    SESSION_TTL,
)


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sign(payload: str) -> str:
    return _b64u(hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).digest())


def make_session(email: str) -> str:
    payload = _b64u(json.dumps({"e": email, "x": int(time.time()) + SESSION_TTL}).encode())
    return f"{payload}.{_sign(payload)}"


def session_email(cookie: str) -> "str | None":
    """Return the signed-in email, or None if the cookie is absent/forged/expired."""
    try:
        payload, sig = cookie.split(".", 1)
        # compare_digest, not `==`: a plain comparison leaks the correct signature
        # a byte at a time to anyone willing to time their requests.
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if data["x"] < time.time():
            return None
        return data["e"]
    except Exception:
        return None


def request_email(request: web.Request) -> "str | None":
    """The signed-in email, or None. Does NOT check whether they're still allowed."""
    return session_email(request.cookies.get(COOKIE_NAME, ""))


def is_authed(request: web.Request) -> bool:
    if not AUTH_ENABLED:
        return True
    # Re-check the allowlist on EVERY request, not just at sign-in: the cookie
    # stays valid for SESSION_TTL, so a login-time-only check would leave someone
    # you just removed with access for days. Checking here means removing a guest
    # in the panel cuts them off on their very next request — no restart, no
    # waiting out the cookie. Costs nothing: the email rides in the cookie and the
    # list is cached in memory.
    email = request_email(request)
    return email is not None and allowlist.allows(email)


def can_use_agent(request: web.Request) -> bool:
    """May they run the agent? Admins, plus members granted the "agent" role.

    Re-checked per request like is_authed, so an admin demoting someone in the
    panel cuts off their agent access on the next click rather than whenever their
    cookie expires. This is the check that stands between a signed-in guest and
    your token spend.
    """
    if not AUTH_ENABLED:
        return True
    email = request_email(request)
    return email is not None and allowlist.allows_agent(email)


def is_admin(request: web.Request) -> bool:
    # Note the missing `if not AUTH_ENABLED: return True` that is_authed has. With
    # the gate off, everyone is "authed" — inheriting that here would expose the
    # admin panel to the entire internet on an unconfigured deployment.
    if not AUTH_ENABLED:
        return False
    email = request_email(request)
    return email is not None and email in ADMIN_EMAILS


def request_url(request: web.Request) -> str:
    # We sit behind TLS-terminating Caddy, so request.scheme is http here; trust
    # its X-Forwarded-Proto and default to https (nothing reaches us any other way).
    proto = request.headers.get("X-Forwarded-Proto", "https")
    return f"{proto}://{request.host}{request.rel_url}"


def safe_next(url: str) -> str:
    """Clamp the post-login redirect to our own domain (open-redirect guard)."""
    fallback = f"https://{PREVIEW_BASE_DOMAIN}/"
    try:
        parts = urlsplit(url)
    except Exception:
        return fallback
    host = parts.hostname or ""
    if parts.scheme != "https":
        return fallback
    if host != PREVIEW_BASE_DOMAIN and not host.endswith("." + PREVIEW_BASE_DOMAIN):
        return fallback
    return url


def redirect_to_login(request: web.Request) -> web.Response:
    nxt = urlencode({"next": request_url(request)})
    return web.HTTPFound(f"https://{AUTH_HOST}/__auth/login?{nxt}")
