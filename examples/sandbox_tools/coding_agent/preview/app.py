"""
App wiring: the route table, plus the shared HTTP session and SDK lifecycle.

Route ORDER is load-bearing — see build_app().
"""

from aiohttp import ClientSession, ClientTimeout, web

from . import auth, proxy


async def on_startup(app: web.Application) -> None:
    app["session"] = ClientSession(timeout=ClientTimeout(total=None))


async def on_cleanup(app: web.Application) -> None:
    await app["session"].close()
    await proxy.daytona.close()   # closes the SDK's shared state-streaming websocket


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Everything below MUST be added before the catch-all: aiohttp's
    # UrlDispatcher.resolve() matches resources in registration order, and the
    # catch-all's "/{tail:.*}" would otherwise swallow these too (it matches on
    # path only, same as every other request path).
    app.router.add_get("/check", proxy.check)

    # The auth routes are deliberately NOT behind the gate — they're how you get
    # through it. Like /check they match on path across every host, so a previewed
    # site can't serve its own "/__auth/*"; the dunder name keeps that from
    # mattering in practice. Each handler re-checks its own access (login is
    # public, the admin pair is admin-only), so this table grants nothing by
    # itself.
    app.router.add_get("/__auth/login", auth.login_page)
    app.router.add_post("/__auth/session", auth.create_session)
    app.router.add_get("/__auth/logout", auth.logout)
    app.router.add_get("/__auth/admin", auth.admin_page)
    app.router.add_post("/__auth/admin/guests", auth.admin_guests)

    # Catch-all: Host decides the sandbox, the path is forwarded verbatim.
    app.router.add_route("*", "/{tail:.*}", proxy.handler)
    return app
