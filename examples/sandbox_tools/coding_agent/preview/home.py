"""
The landing page: a gallery of every preview site the proxy has ever served,
plus the routes that back it.

    GET  /              (on the login host)  the gallery itself
    GET  /__apps/shot/{key}                  one screenshot
    POST /__apps/refresh                     re-screenshot one app
    POST /__apps/forget                      drop one app from the gallery

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

from .config import AUTH_HOST, PREVIEW_BASE_DOMAIN
from .pages import HOME_PAGE
from .registry import registry
from .screenshots import screenshotter, shot_path
from .session import is_admin, is_authed, redirect_to_login, request_email


async def home_page(request: web.Request) -> web.Response:
    """The gallery. Callers must have already established this is the login host."""
    if not is_authed(request):
        return redirect_to_login(request)
    body = (
        HOME_PAGE.replace("__APPS__", json.dumps(registry.list()))
        .replace("__IS_ADMIN__", "true" if is_admin(request) else "false")
        .replace("__BASE__", json.dumps(PREVIEW_BASE_DOMAIN))
        # json.dumps, not html.escape: this lands in an HTML text node, and dumps
        # gives us a quoted string whose contents can't close the surrounding tag.
        .replace("__EMAIL__", json.dumps(request_email(request) or "")[1:-1])
    )
    return web.Response(content_type="text/html", text=body)


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


async def app_forget(request: web.Request) -> web.Response:
    """Drop one app from the gallery. Admin-only — the registry is shared state.

    Removes the record and its screenshot, never the sandbox itself. The app
    re-registers on its own if anyone visits it again.
    """
    if not is_admin(request):
        return web.json_response({"message": "Not found"}, status=404)
    if request.headers.get("Origin", "") != f"https://{AUTH_HOST}":
        return web.json_response({"message": "Bad origin"}, status=403)
    try:
        key = (await request.json()).get("key", "")
    except Exception:
        return web.json_response({"message": "Expected JSON"}, status=400)

    if not await registry.forget(key):
        return web.json_response({"message": "Unknown app."}, status=404)
    try:
        os.remove(shot_path(key))
    except OSError:
        pass   # no screenshot to clean up
    return web.json_response(_payload("Removed."))
