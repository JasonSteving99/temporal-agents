"""
The HTTP handlers behind `/__auth/*`: signing in, signing out, and the admin
panel that edits the guest list.

Sign-in flow — an unauthenticated request to any sandbox subdomain is redirected
here, and the round trip is:

    GET  /__auth/login    -> the Firebase sign-in page (AUTH_HOST only)
    POST /__auth/session  <- {idToken, next}; verified, then our cookie is set
    GET  <next>              back to whatever they were trying to reach

The ID token is verified server-side by handing it to Google's Identity Toolkit
`accounts:lookup` REST endpoint — which takes the same public web API key the
page already ships, so there is NO firebase-admin dependency and no
service-account key to store. That happens exactly once per sign-in; afterwards
session.py's cookie carries the session.

ADMIN PANEL. Access control is `is_admin` and nothing else. Two things worth
noting about how it responds to everyone else:

  * Non-admins get 404, not 403. A 403 confirms "there IS an admin panel here,
    you're just not on the list", which is a hint worth not giving out. To a
    signed-in friend poking at URLs, this endpoint simply does not exist.
  * The panel can only manage MEMBERS and their roles. There is no route that
    writes ADMIN_EMAILS, because it comes from the environment (allowlist.py) —
    so someone who reached these handlers could grant preview or agent access,
    but never admin, and so never the ability to grant.
"""

import html
import json
from urllib.parse import urlencode

from aiohttp import ClientSession, web

from .allowlist import ROLE_PREVIEW, ROLES, allowlist, clean_email
from .config import (
    ADMIN_EMAILS,
    AUTH_ENABLED,
    AUTH_HOST,
    COOKIE_NAME,
    FIREBASE_API_KEY,
    FIREBASE_AUTH_DOMAIN,
    FIREBASE_PROJECT_ID,
    PREVIEW_BASE_DOMAIN,
    SESSION_TTL,
)
from .pages import ADMIN_PAGE, LOGIN_PAGE
from .session import is_admin, is_authed, make_session, request_email, safe_next


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------
async def login_page(request: web.Request) -> web.Response:
    if not AUTH_ENABLED:
        return web.Response(status=404, text="auth is not configured")
    nxt = safe_next(request.query.get("next", ""))
    # Keep sign-in on the one authorized origin, wherever the link was followed from.
    if request.host.split(":")[0].lower() != AUTH_HOST:
        return web.HTTPFound(f"https://{AUTH_HOST}/__auth/login?{urlencode({'next': nxt})}")
    if is_authed(request):
        return web.HTTPFound(nxt)
    config = {
        "apiKey": FIREBASE_API_KEY,
        "authDomain": FIREBASE_AUTH_DOMAIN,
        "projectId": FIREBASE_PROJECT_ID,
    }
    body = LOGIN_PAGE.replace("__CONFIG__", json.dumps(config)).replace(
        "__NEXT__", json.dumps(nxt)
    )
    return web.Response(content_type="text/html", text=body)


async def verify_id_token(session: ClientSession, id_token: str) -> "str | None":
    """Validate a Firebase ID token via Identity Toolkit; return its verified email."""
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={FIREBASE_API_KEY}"
    async with session.post(url, json={"idToken": id_token}) as resp:
        if resp.status != 200:   # 400 INVALID_ID_TOKEN / expired / wrong project
            return None
        users = (await resp.json()).get("users") or []
    if not users:
        return None
    user = users[0]
    # emailVerified is always true for Google sign-in; the check matters only if you
    # later enable email/password, where an unverified address must not pass.
    if not user.get("emailVerified"):
        return None
    return (user.get("email") or "").lower() or None


async def create_session(request: web.Request) -> web.Response:
    """Exchange a verified Firebase ID token for our signed session cookie."""
    if not AUTH_ENABLED:
        return web.Response(status=404, text="auth is not configured")
    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="expected JSON")

    email = await verify_id_token(request.app["session"], payload.get("idToken") or "")
    if not email:
        return web.Response(status=401, text="Sign-in could not be verified.")
    if not allowlist.allows(email):
        return web.Response(status=403, text=f"{email} is not allowed to view previews.")

    resp = web.json_response({"redirect": safe_next(payload.get("next") or "")})
    resp.set_cookie(
        COOKIE_NAME,
        make_session(email),
        # Domain=<base> (no leading subdomain) is what makes one sign-in cover every
        # <sandboxId>-<port> subdomain as well as this login host.
        domain=PREVIEW_BASE_DOMAIN,
        path="/",
        max_age=SESSION_TTL,
        secure=True,
        httponly=True,   # the proxy reads this, never page JS
        samesite="Lax",
    )
    return resp


async def logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound(f"https://{AUTH_HOST}/__auth/login")
    resp.del_cookie(COOKIE_NAME, domain=PREVIEW_BASE_DOMAIN, path="/")
    return resp


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------
async def admin_page(request: web.Request) -> web.Response:
    if not is_admin(request):
        # 404, not 403 — see the module docstring.
        return web.Response(status=404, text="Not found")
    if request.host.split(":")[0].lower() != AUTH_HOST:
        return web.HTTPFound(f"https://{AUTH_HOST}/__auth/admin")
    body = (
        ADMIN_PAGE.replace("__MEMBERS__", json.dumps(allowlist.members()))
        .replace("__ADMINS__", json.dumps(sorted(ADMIN_EMAILS)))
        .replace("__EMAIL__", html.escape(request_email(request) or ""))
    )
    return web.Response(content_type="text/html", text=body)


async def admin_guests(request: web.Request) -> web.Response:
    """Add/remove one member, or change their role. Admin-only, same-origin-only.

    `set_role` is the one action that can hand out spending power (the "agent"
    role), so it lives behind the same is_admin check as everything else here —
    a member can never change their own role, or anyone else's.
    """
    if not is_admin(request):
        return web.json_response({"message": "Not found"}, status=404)
    # CSRF: the session cookie is SameSite=Lax, which already stops a cross-site
    # POST from carrying it. This is the belt to that pair of braces — browsers
    # always send Origin on POST, and an attacker page cannot forge it.
    if request.headers.get("Origin", "") != f"https://{AUTH_HOST}":
        return web.json_response({"message": "Bad origin"}, status=403)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"message": "Expected JSON"}, status=400)

    email = clean_email(payload.get("email"))
    action = payload.get("action")
    role = payload.get("role", ROLE_PREVIEW)
    if not email:
        return web.json_response({"message": "That doesn't look like an email."}, status=400)
    if action not in ("add", "remove", "set_role"):
        return web.json_response({"message": "Unknown action."}, status=400)
    if action in ("add", "set_role") and role not in ROLES:
        return web.json_response({"message": "Unknown role."}, status=400)

    if action == "add":
        ok, message = await allowlist.add(email, role)
    elif action == "set_role":
        ok, message = await allowlist.set_role(email, role)
    else:
        ok, message = await allowlist.remove(email)
    return web.json_response(
        {"message": message, "members": allowlist.members()}, status=200 if ok else 409
    )
