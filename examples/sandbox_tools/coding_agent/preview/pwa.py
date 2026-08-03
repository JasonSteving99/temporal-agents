"""
Progressive-web-app plumbing for the gallery: manifest, service worker, icons.

CACHING RULE, and it is the whole design: **a load always reflects the server.**
The gallery's value is telling you what exists right now, so a stale shell is
worse than a slow one. The service worker is therefore network-first for
everything except screenshots — and screenshots are the one safe exception,
because the gallery requests them at `?v=<shot_at>`, so a given URL's bytes never
change. Those get cache-first, which is what makes the grid paint instantly on a
phone while the list itself stays honest.

Everything here is served WITHOUT auth. It is pure branding and plumbing — no app
list, no sandbox ids — and keeping it public avoids a pile of edge cases: the OS
fetching icons outside a browser session, the manifest being read before the
cookie is sent, an expired session turning the installed app into a white screen.
The gallery those assets decorate is still gated.

All paths live under `/__apps/`, NOT at the root. Routes here match on path across
every host (same as `/check` and `/__auth/*`), so a root-level `/sw.js` would
shadow the service worker of any agent-built site that ships one. The service
worker still needs to control `/`, which it gets from the `Service-Worker-Allowed`
header rather than from its own location.
"""

import hashlib
import os

from aiohttp import web

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Optional demo clips for the landing page. Nothing ships here — drop a file in
# and the landing page uses it instead of its CSS mock; leave it empty and the
# page is exactly as complete. See the README, "Landing-page demo clips".
MEDIA_DIR = os.path.join(STATIC_DIR, "media")

# Order is preference: a browser gets the first format it can play, so webm
# (smaller) is offered ahead of mp4 (universal), and a still image is the floor.
MEDIA_TYPES = {
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
}

ICONS = {
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-maskable-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "favicon.svg": "image/svg+xml",
}

THEME_COLOR = "#0b0d10"

SERVICE_WORKER = """// Preview gallery service worker. See preview/pwa.py for the rationale.
const CACHE = "gallery-__BUILD__";
const SHELL = "/";

// A new worker takes over immediately instead of idling until every tab closes,
// so a deploy reaches an installed app on its next launch rather than eventually.
self.addEventListener("install", (e) => {
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key !== CACHE) await caches.delete(key);   // drop every older build
    }
    await self.clients.claim();
  })());
});

async function cacheFirst(req) {
  const hit = await caches.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) (await caches.open(CACHE)).put(req, res.clone());
  return res;
}

async function networkFirst(req) {
  try {
    const res = await fetch(req);
    // Only bank real, final responses. Caching a 302-to-sign-in would pin an
    // expired session into the cache and lock the app out of itself.
    if (res.ok && res.type === "basic" && !res.redirected) {
      (await caches.open(CACHE)).put(req, res.clone());
    }
    return res;
  } catch (err) {
    const hit = await caches.match(req);
    if (hit) return hit;
    if (req.mode === "navigate") {
      const shell = await caches.match(SHELL);
      if (shell) return shell;
    }
    throw err;
  }
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Never let the worker sit in the middle of sign-in.
  if (url.pathname.startsWith("/__auth/")) return;

  // Screenshots are immutable per ?v=<shot_at>, so this is free freshness.
  if (url.pathname.startsWith("/__apps/shot/")) {
    e.respondWith(cacheFirst(req));
    return;
  }

  e.respondWith(networkFirst(req));
});
"""


_BUILD_ID: "str | None" = None


def build_id() -> str:
    """Changes whenever the shipped app changes, so the browser sees a new worker.

    A byte-identical service worker is treated as "no update" by the browser, so
    the cache name has to move with the code. Hashing the shipped markup and the
    shared stylesheet means a redeploy that changed nothing doesn't needlessly
    evict everyone's screenshots — and, since theme.py is in the hash, a
    colour-only change still reaches installed apps.

    Computed on first use rather than at import: landing.py imports this module,
    so hashing it from module scope here would be a cycle.
    """
    global _BUILD_ID
    if _BUILD_ID is None:
        from .landing import _CSS, _TEMPLATE
        from .pages import ADMIN_PAGE, HOME_PAGE
        from .theme import BASE_CSS

        h = hashlib.sha256()
        for part in (HOME_PAGE, ADMIN_PAGE, _TEMPLATE, _CSS, BASE_CSS, SERVICE_WORKER):
            h.update(part.encode())
        _BUILD_ID = h.hexdigest()[:12]
    return _BUILD_ID


async def manifest(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "name": "Preview gallery",
            "short_name": "Previews",
            "description": "Sites built by the coding agent.",
            # Scope and start_url are the ROOT even though this file is served from
            # /__apps/ — the installed app should open the gallery, not this path.
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "any",
            "background_color": THEME_COLOR,
            "theme_color": THEME_COLOR,
            "icons": [
                {"src": "/__apps/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/__apps/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {
                    "src": "/__apps/static/icon-maskable-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
        },
        headers={"Cache-Control": "public, max-age=3600"},
        content_type="application/manifest+json",
    )


async def service_worker(request: web.Request) -> web.Response:
    return web.Response(
        text=SERVICE_WORKER.replace("__BUILD__", build_id()),
        content_type="application/javascript",
        headers={
            # Widen the worker's scope beyond its own directory. Without this the
            # browser refuses to let a /__apps/ script control "/".
            "Service-Worker-Allowed": "/",
            # The worker is how updates arrive, so it must never be served stale.
            "Cache-Control": "no-cache",
        },
    )


def media_tag(slot: str, fallback: str) -> str:
    """Markup for one landing-page visual: a real recording if one exists, else `fallback`.

    Looked up per render rather than at import, so dropping `hero.mp4` into
    `static/media/` takes effect on the next page load instead of the next
    restart. The directory is normally absent and this simply returns `fallback`.

    Videos are muted, looping and inline — the three attributes that let a clip
    autoplay on iOS at all — and carry no controls, because the slot is an
    illustration, not something to operate.
    """
    found = [
        (ext, mime) for ext, mime in MEDIA_TYPES.items()
        if os.path.isfile(os.path.join(MEDIA_DIR, slot + ext))
    ]
    if not found:
        return fallback
    videos = [(e, m) for e, m in found if m.startswith("video/")]
    if videos:
        sources = "".join(
            f'<source src="/__apps/media/{slot}{ext}" type="{mime}">' for ext, mime in videos
        )
        return (
            '<video autoplay muted loop playsinline preload="metadata" '
            f'aria-hidden="true">{sources}</video>'
        )
    ext = found[0][0]
    return f'<img src="/__apps/media/{slot}{ext}" alt="">'


async def media_asset(request: web.Request) -> web.Response:
    """Serve one landing-page clip. Public, like the icons — it's marketing."""
    name = request.match_info.get("name", "")
    stem, ext = os.path.splitext(name)
    content_type = MEDIA_TYPES.get(ext.lower())
    # Allowlist both halves: the extension by type, the stem by shape. Neither can
    # contain a separator, so the join below cannot escape MEDIA_DIR.
    if content_type is None or not stem.replace("-", "").replace("_", "").isalnum():
        return web.Response(status=404, text="Not found")
    path = os.path.join(MEDIA_DIR, name)
    if not os.path.isfile(path):
        return web.Response(status=404, text="Not found")
    return web.FileResponse(
        path,
        headers={"Content-Type": content_type, "Cache-Control": "public, max-age=86400"},
    )


async def static_asset(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "")
    content_type = ICONS.get(name)
    if content_type is None:      # allowlist, so the path can't be traversed
        return web.Response(status=404, text="Not found")
    path = os.path.join(STATIC_DIR, name)
    if not os.path.isfile(path):
        return web.Response(status=404, text="Not found")
    return web.FileResponse(
        path,
        headers={"Content-Type": content_type, "Cache-Control": "public, max-age=604800"},
    )


def head_tags() -> str:
    """The <head> block shared by the gallery (and any page we want installable)."""
    return (
        f'<meta name="theme-color" content="{THEME_COLOR}">'
        '<link rel="manifest" href="/__apps/manifest.webmanifest">'
        '<link rel="icon" href="/__apps/static/favicon.svg" type="image/svg+xml">'
        '<link rel="apple-touch-icon" href="/__apps/static/apple-touch-icon.png">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-title" content="Previews">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
    )
