"""
The root of the login host, and the routes that back it.

    GET  /              (on the login host)  the gallery — or, signed out,
                                             the landing page (landing.py)
    GET  /__apps/shot/{key}                  one screenshot
    POST /__apps/refresh                     re-screenshot one app
    POST /__apps/label                       rename one app          (admin)
    POST /__apps/pin                         pin/unpin one app       (admin)
    POST /__apps/forget                      drop one app            (admin)

Signed in, "/" is a gallery of every preview site the proxy has ever served.
Signed out it is a page that explains what that gallery is before asking anyone
to authenticate into it.

It lives at the root of AUTH_HOST — by default the base domain itself, so the
gallery is just `https://<PREVIEW_BASE_DOMAIN>/`. Signing in therefore lands you
somewhere useful, and the admin panel is reachable by a link rather than a
memorised path. (`*.<base>` doesn't match `<base>`, so that host needs its own
DNS record and Caddy block; config.AUTH_HOST explains the fallback.)

Everything here is behind `is_authed`. The gallery lists every sandbox id you own
— that's a map of exactly what to visit, so it must never be public even though
each individual site is separately gated.
"""

import json
import os

from aiohttp import web

from . import landing
from .config import (
    AGENT_ENABLED,
    AGENT_HOST,
    AUTH_HOST,
    AUTO_STOP_MINUTES,
    PREVIEW_BASE_DOMAIN,
)
from .pages import HOME_PAGE
from .pwa import head_tags
from .registry import registry
from .screenshots import screenshotter, shot_path
from .session import (
    can_use_agent,
    is_admin,
    is_authed,
    request_email,
)


async def home_page(request: web.Request) -> web.Response:
    """The gallery, or the landing page. Callers have established this is the login host."""
    if not is_authed(request):
        # The landing page, NOT a bounce to sign-in. Someone arriving here has no
        # idea what this is yet, and the sign-in card is the wrong place to find
        # out; `redirect_to_login` still handles anyone who followed a link to an
        # actual preview, because there we know exactly where to send them back to.
        return web.Response(
            content_type="text/html",
            text=landing.render(PREVIEW_BASE_DOMAIN),
            headers={"Cache-Control": "no-store"},
        )
    admin = is_admin(request)
    # Shown to whoever may actually use it — admins and the "agent" role — so a
    # granted member discovers it without being told the hostname.
    agent_url = f"https://{AGENT_HOST}/" if (AGENT_ENABLED and can_use_agent(request)) else ""
    body = (
        HOME_PAGE.replace("__APPS__", json.dumps(registry.list()))
        .replace("__AGENT_URL__", json.dumps(agent_url))
        .replace("__IS_ADMIN__", "true" if admin else "false")
        .replace("__BASE__", json.dumps(PREVIEW_BASE_DOMAIN))
        # The page infers awake/asleep from this window, so it has to be the same
        # number the proxy actually pauses on rather than a hardcoded guess.
        .replace("__IDLE_MIN__", json.dumps(AUTO_STOP_MINUTES))
        # json.dumps, not html.escape: this lands in an HTML text node, and dumps
        # gives us a quoted string whose contents can't close the surrounding tag.
        .replace("__EMAIL__", json.dumps(request_email(request) or "")[1:-1])
        .replace("__PWA_HEAD__", head_tags())
    )
    # no-store, belt to the service worker's network-first braces: the embedded app
    # list is only as good as the moment it was rendered, so the browser must never
    # replay this document from its own HTTP cache.
    return web.Response(
        content_type="text/html", text=body, headers={"Cache-Control": "no-store"}
    )


async def app_shot(request: web.Request) -> web.Response:
    """Serve one screenshot from the volume."""
    if not is_authed(request):
        return web.Response(status=404, text="Not found")
    key = request.match_info.get("key", "")
    # Path traversal is impossible by construction: the key must be one the
    # registry already knows, and those are only ever minted from a parsed
    # "<sandboxId>-<port>" host. A "../.." key simply isn't in the dict.
    if key not in registry:
        return web.Response(status=404, text="Not found")
    path = shot_path(key)
    if not os.path.isfile(path):
        return web.Response(status=404, text="No screenshot yet")
    # FileResponse streams via sendfile instead of reading the jpeg into the event
    # loop — a gallery of 20 tiles is 20 of these requests at once.
    return web.FileResponse(
        path,
        headers={
            "Content-Type": "image/jpeg",
            # The gallery cache-busts with ?v=<shot_at>, so each version is immutable.
            "Cache-Control": "private, max-age=86400",
        },
    )


def _payload(message: str) -> dict:
    return {"message": message, "apps": registry.list()}


async def app_refresh(request: web.Request) -> web.Response:
    """Re-screenshot one app, waking its sandbox if necessary.

    Any signed-in user may do this. It grants no capability they don't have
    already — clicking Open would wake the same sandbox the same way — and the
    button says so.
    """
    if not is_authed(request):
        return web.json_response({"message": "Not found"}, status=404)
    if request.headers.get("Origin", "") != f"https://{AUTH_HOST}":
        return web.json_response({"message": "Bad origin"}, status=403)
    try:
        key = (await request.json()).get("key", "")
    except Exception:
        return web.json_response({"message": "Expected JSON"}, status=400)

    app = registry.get(key)
    if app is None:
        return web.json_response({"message": "Unknown app."}, status=404)

    # Imported here rather than at module scope: proxy.py serves this module's
    # gallery for non-preview hosts, so a top-level import would be a cycle.
    from .proxy import ensure_ready

    session = request.app["session"]
    try:
        preview, _woken = await ensure_ready(app["sandbox_id"], app["port"], session)
    except Exception as exc:
        return web.json_response(
            {"message": f"Could not reach that sandbox: {exc}", "apps": registry.list()},
            status=502,
        )

    # Awaited, not fire-and-forget: the user pressed a button and is watching.
    if await screenshotter.capture(key, preview.url, preview.token):
        return web.json_response(_payload("Screenshot updated."))
    reason = screenshotter.last_error or "capture already in progress"
    return web.json_response(
        {"message": f"Could not screenshot: {reason}", "apps": registry.list()}, status=502
    )


async def _admin_body(request: web.Request) -> "dict | web.Response":
    """Shared preamble for the admin-only gallery mutations.

    Returns the parsed body, or the Response to send instead. The three checks are
    the same every time and getting one wrong is the whole security story, so they
    live in one place: admin (404, never 403 — see auth.py), same-origin, JSON.
    """
    if not is_admin(request):
        return web.json_response({"message": "Not found"}, status=404)
    if request.headers.get("Origin", "") != f"https://{AUTH_HOST}":
        return web.json_response({"message": "Bad origin"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"message": "Expected JSON"}, status=400)
    return body if isinstance(body, dict) else {}


async def app_label(request: web.Request) -> web.Response:
    """Name one app by hand. Admin-only: the registry is shared, so is the name.

    Sites arrive named after whatever `<title>` the agent happened to write, which
    is often "Vite + React". A label is how a gallery of twenty stays legible.
    """
    body = await _admin_body(request)
    if isinstance(body, web.Response):
        return body
    if not await registry.set_label(body.get("key", ""), str(body.get("label", ""))):
        return web.json_response({"message": "Unknown app."}, status=404)
    return web.json_response(_payload("Renamed."))


async def app_pin(request: web.Request) -> web.Response:
    """Pin or unpin one app, which is the only ordering a human controls."""
    body = await _admin_body(request)
    if isinstance(body, web.Response):
        return body
    pinned = bool(body.get("pinned"))
    if not await registry.set_pinned(body.get("key", ""), pinned):
        return web.json_response({"message": "Unknown app."}, status=404)
    return web.json_response(_payload("Pinned." if pinned else "Unpinned."))


async def app_forget(request: web.Request) -> web.Response:
    """Drop one app from the gallery. Admin-only — the registry is shared state.

    Removes the record and its screenshot, never the sandbox itself. The app
    re-registers on its own if anyone visits it again.
    """
    body = await _admin_body(request)
    if isinstance(body, web.Response):
        return body
    key = body.get("key", "")

    if not await registry.forget(key):
        return web.json_response({"message": "Unknown app."}, status=404)
    try:
        os.remove(shot_path(key))
    except OSError:
        pass   # no screenshot to clean up
    return web.json_response(_payload("Removed."))
