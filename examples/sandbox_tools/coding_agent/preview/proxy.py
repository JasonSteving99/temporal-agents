"""
The proxying itself: parse the Host header into a sandbox, wake it if it's
stopped, wait for its server to bind, then forward the request unchanged.

Also home to the Caddy `on_demand_tls` check, which decides which hostnames are
worth issuing a certificate for.
"""

import asyncio

from aiohttp import ClientSession, WSMsgType, web
from daytona import AsyncDaytona, DaytonaConfig, SandboxState
from yarl import URL

from .config import (
    AGENT_ENABLED,
    AGENT_HOST,
    AGENT_UPSTREAM,
    AUTH_HOST,
    AUTO_STOP_MINUTES,
    DAYTONA_API_KEY,
    DAYTONA_TARGET,
    PREVIEW_BASE_DOMAIN,
)
from .pages import DENIED_PAGE
from .registry import app_key, registry
from .screenshots import schedule_capture
from .session import can_use_agent, is_authed, redirect_to_login


def _make_daytona() -> AsyncDaytona:
    # One shared client for the whole process. The async client opens a single
    # state-streaming websocket that all sandboxes share, so do NOT construct one
    # per request — build it once and close it on shutdown.
    kwargs = {"api_key": DAYTONA_API_KEY}
    if DAYTONA_TARGET:
        kwargs["target"] = DAYTONA_TARGET
    return AsyncDaytona(DaytonaConfig(**kwargs))


daytona = _make_daytona()

# Headers that must not be blindly copied through a proxy.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# Reserved Daytona ports to NEVER route to unless you mean it:
#   22222 = web terminal, 2280 = toolbox, 33333 = recording dashboard.
RESERVED_PORTS = {22222, 2280, 33333}

# Sandbox ids we've already applied AUTO_STOP_MINUTES to. Daytona persists the
# setting on the sandbox, so once per id is enough; setting it on every request
# would add a needless API round-trip. A restarted proxy just re-applies on the
# first request it sees for each sandbox.
_autostop_configured: set[str] = set()


# ---------------------------------------------------------------------------
# Host parsing: pull (sandbox_id, port) out of the Host header. Returns None
# for anything that isn't a "<sandboxId>-<port>.<PREVIEW_BASE_DOMAIN>" host
# (the bare base domain, an IP, a stray Host) — those get the help page.
# ---------------------------------------------------------------------------
def parse_preview_host(host_header: str) -> "tuple[str, int] | None":
    if not PREVIEW_BASE_DOMAIN or not host_header:
        return None
    host = host_header.strip().lower()
    # Drop an optional ":port" (present when hit directly rather than via a 443 reverse proxy).
    # Preview hosts are never IPv6 literals, so a bracket means "not one of ours".
    if host.startswith("["):
        return None
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    suffix = "." + PREVIEW_BASE_DOMAIN
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    # Must be a single DNS label so a one-level wildcard cert (*.<base>) covers it.
    if not label or "." in label:
        return None
    sandbox_id, sep, port_str = label.rpartition("-")
    if not sep or not sandbox_id or not port_str.isdigit():
        return None
    return sandbox_id, int(port_str)


# ---------------------------------------------------------------------------
# Readiness gate: after the sandbox reports "started", the server inside it
# still needs a moment to bind the port. Poll until it answers, or we 503.
# We probe THROUGH Daytona's preview URL so we test the real path.
# ---------------------------------------------------------------------------
async def wait_for_server(
    session: ClientSession, url: str, token: str, timeout: float = 30.0
) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            async with session.head(
                url,
                headers={"x-daytona-preview-token": token},
                allow_redirects=False,
            ) as resp:
                # Any HTTP response means something is listening. Tune if your
                # app returns e.g. 404 at "/" — you just want "not refused".
                if resp.status < 500 or resp.status == 404:
                    return
        except Exception:
            pass  # connection refused / not up yet — keep waiting
        await asyncio.sleep(0.5)
    raise RuntimeError("server did not become ready in time")


# ---------------------------------------------------------------------------
# Wake the sandbox if needed, then get a FRESH preview link + token.
# Order matters: a standard preview token is invalidated when the sandbox
# restarts, so a token cached from before the stop is dead. Fetch it AFTER
# start(). get_preview_link also auto-opens the port if it's closed.
# ---------------------------------------------------------------------------
async def ensure_ready(sandbox_id: str, port: int, session: ClientSession):
    """Returns (preview, woken) — `woken` is True if we had to start the sandbox.

    Callers use `woken` to decide whether the site may have changed since its last
    screenshot: a restart re-runs start.sh, which can pick up a newer build.
    """
    woken = False
    sandbox = await daytona.get(sandbox_id)
    if sandbox.state != SandboxState.STARTED:
        # This is where the snapshot entrypoint supervisor (supervise.sh) re-runs
        # /home/daytona/project/start.sh and relaunches the server. start() waits until
        # the sandbox itself is "started" (not until the server binds).
        await sandbox.start()
        woken = True
    # Cap the compute bill: auto-stop after AUTO_STOP_MINUTES of no SDK activity.
    # Daytona counts SDK interactions (state changes, process.exec, etc.) as
    # activity but NOT preview HTTP traffic — so the agent's own tool calls keep an
    # active chat turn alive, while a sandbox left idle (e.g. a preview tab open
    # but quiet) stops itself. A later request just wakes it again (brief
    # "warming up"). Set once per id; Daytona persists it.
    if AUTO_STOP_MINUTES and sandbox_id not in _autostop_configured:
        await sandbox.set_autostop_interval(AUTO_STOP_MINUTES)
        _autostop_configured.add(sandbox_id)
    preview = await sandbox.get_preview_link(port)   # -> .url, .token
    await wait_for_server(session, preview.url, preview.token)
    return preview, woken


# ---------------------------------------------------------------------------
# Rebuild the upstream URL. The path is forwarded UNCHANGED (subdomain
# routing means there's no prefix to strip) so absolute asset paths just work.
# ---------------------------------------------------------------------------
def upstream_url(preview_url: str, path: str, query: "URL") -> URL:
    base = URL(preview_url)
    return (base.origin() / path.lstrip("/")).with_query(query)


# ---------------------------------------------------------------------------
# Byte-for-byte forwarding, shared by the sandbox previews and the agent app.
#
# Streams the response as it arrives rather than buffering it, which is what makes
# Server-Sent Events work — the agent UI holds a text/event-stream open on
# /api/attach for the life of a chat, and a proxy that waited for EOF would show
# the user nothing until the turn finished. Content-Length is dropped with the
# other hop-by-hop headers, so a streamed body stays chunked.
# ---------------------------------------------------------------------------
async def forward(
    request: web.Request,
    session: ClientSession,
    target: "URL | str",
    extra_headers: "dict[str, str] | None" = None,
) -> web.StreamResponse:
    out_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP
    }
    out_headers.update(extra_headers or {})

    body = await request.read()
    async with session.request(
        request.method, str(target), headers=out_headers, data=body,
        allow_redirects=False,
    ) as upstream:
        resp_headers = {
            k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP
        }
        resp = web.StreamResponse(status=upstream.status, headers=resp_headers)
        await resp.prepare(request)
        async for chunk in upstream.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
        return resp


# ---------------------------------------------------------------------------
# The agent chat app (see config.AGENT_HOST). Every message here spends model
# tokens and Daytona compute, so viewing previews is not enough: the caller must
# be an admin or hold the "agent" role an admin granted them.
# ---------------------------------------------------------------------------
async def proxy_agent(request: web.Request, session: ClientSession) -> web.StreamResponse:
    if not can_use_agent(request):
        if is_authed(request):
            # Signed in, just not allowed here. A redirect to sign-in would loop
            # them straight back, so say plainly what happened.
            return web.Response(status=403, content_type="text/html", text=DENIED_PAGE)
        return redirect_to_login(request)
    # No WebSocket branch: the harness web app streams with SSE, not websockets, and
    # SSE is a plain streamed GET that forward() already handles. If it ever grows a
    # websocket endpoint, this needs a bridge like proxy_ws below.
    target = URL(AGENT_UPSTREAM + request.rel_url.path).with_query(request.rel_url.query)
    return await forward(request, session, target, {
        # Let the app build correct absolute URLs even though it sees a plain HTTP hop.
        "X-Forwarded-Host": request.headers.get("Host", ""),
        "X-Forwarded-Proto": request.headers.get("X-Forwarded-Proto", "https"),
    })


# ---------------------------------------------------------------------------
# WebSocket passthrough (HMR, live reload). Daytona auto-detects the upgrade
# and skips its warning page; we just bridge client <-> upstream and carry
# the token + forwarded host.
# ---------------------------------------------------------------------------
async def proxy_ws(request, sandbox_id, port, path, session):
    try:
        preview, _woken = await ensure_ready(sandbox_id, port, session)
    except Exception:
        return web.Response(status=503, text="Warming up…")

    client_ws = web.WebSocketResponse()
    await client_ws.prepare(request)

    up_url = upstream_url(preview.url, path, request.rel_url.query)
    up_url = up_url.with_scheme("wss" if up_url.scheme == "https" else "ws")

    async with session.ws_connect(
        str(up_url),
        headers={
            "X-Daytona-Preview-Token": preview.token,
            "X-Forwarded-Host": request.headers.get("Host", ""),
        },
    ) as upstream_ws:

        async def client_to_upstream():
            async for msg in client_ws:
                if msg.type == WSMsgType.TEXT:
                    await upstream_ws.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await upstream_ws.send_bytes(msg.data)

        async def upstream_to_client():
            async for msg in upstream_ws:
                if msg.type == WSMsgType.TEXT:
                    await client_ws.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await client_ws.send_bytes(msg.data)

        await asyncio.gather(client_to_upstream(), upstream_to_client())

    return client_ws


# ---------------------------------------------------------------------------
# Main handler: parse Host -> (help page if not a preview host) -> auth gate ->
# (ws?) -> ensure ready -> forward the request path unchanged.
# ---------------------------------------------------------------------------
async def handler(request: web.Request):
    session: ClientSession = request.app["session"]

    # The agent app gets its own host, checked before sandbox parsing. It can't be
    # mistaken for a sandbox anyway (no "-<port>" suffix), but keeping it first makes
    # the precedence explicit.
    if AGENT_ENABLED and request.host.split(":")[0].lower() == AGENT_HOST:
        return await proxy_agent(request, session)

    route = parse_preview_host(request.host)
    if route is None:
        return await index(request)   # bare base domain / unknown host -> usage help
    sandbox_id, port = route

    if port in RESERVED_PORTS:
        return web.Response(status=403, text=f"Port {port} is reserved by Daytona.")

    # The auth gate (session.py). Checked BEFORE we wake anything: an unauthenticated
    # scanner must not be able to spin up sandboxes, let alone read them.
    if not is_authed(request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return web.Response(status=401, text="not signed in")  # a WS can't follow a 302
        return redirect_to_login(request)

    path = request.rel_url.path

    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_ws(request, sandbox_id, port, path, session)

    try:
        preview, woken = await ensure_ready(sandbox_id, port, session)
    except Exception as e:
        # Serve a branded "warming up" page instead of a raw 502.
        return web.Response(
            status=503, content_type="text/html",
            text=f"<h1>Warming up…</h1><p>{e}</p>",
        )

    # Dynamic registration: this app demonstrably exists and serves traffic, so
    # remember it for the gallery (registry.py). Memory-only for a known app.
    is_new = registry.touch(sandbox_id, port)

    # Refresh the screenshot when the site may have changed — on first sight, or
    # after a wake (which re-runs start.sh and can pick up a newer build). This is
    # fire-and-forget: it must not delay the response the visitor is waiting on.
    # It's also free, because the sandbox is awake right now either way; the
    # gallery never wakes anything by itself.
    if is_new or woken:
        schedule_capture(app_key(sandbox_id, port), preview.url, preview.token)

    target = upstream_url(preview.url, path, request.rel_url.query)
    return await forward(request, session, target, {
        "X-Forwarded-Host": request.headers.get("Host", ""),   # required by Daytona
        "X-Daytona-Preview-Token": preview.token,              # fresh, post-start
        "X-Daytona-Skip-Preview-Warning": "true",              # we own the UX
    })


# ---------------------------------------------------------------------------
# Caddy `on_demand_tls` gate: https://caddyserver.com/docs/caddyfile/options#on-demand-tls
#     Caddy calls this BEFORE issuing/renewing a cert for a hostname it doesn't already have
#     one for, as `GET /check?domain=<hostname>`, and only proceeds on a 2xx. Caddy does no
#     rate limiting of its own here, so this is the only thing standing between a public
#     wildcard preview domain and burning the ACME weekly cert quota on scanner-guessed
#     subdomains — a non-2xx must be the default for anything that isn't a live sandbox.
# ---------------------------------------------------------------------------
async def check(request: web.Request) -> web.Response:
    domain = request.query.get("domain", "").strip().lower()
    # AUTH_HOST is a real site we serve (gallery + sign-in + admin), so Caddy must be
    # allowed a cert for it. Not gated on AUTH_ENABLED: the gallery is served there
    # whether or not the auth gate is switched on. Only relevant if you put AUTH_HOST
    # in an on-demand block — a dedicated site block gets a normal cert and never asks.
    if AUTH_HOST and domain == AUTH_HOST:
        return web.Response(status=200)
    # Same for the agent host — it's a name we serve, not a sandbox, so the
    # sandbox-existence check below would reject it.
    if AGENT_ENABLED and domain == AGENT_HOST:
        return web.Response(status=200)
    route = parse_preview_host(domain)
    if route is None:
        return web.Response(status=403)
    sandbox_id, port = route
    if port in RESERVED_PORTS:
        return web.Response(status=403)
    try:
        await daytona.get(sandbox_id)   # raises if the sandbox id doesn't exist
    except Exception:
        return web.Response(status=403)
    return web.Response(status=200)


# ---------------------------------------------------------------------------
# Anything that isn't a preview host. On the login host that's the gallery
# (home.py); anywhere else it's usage text, since routing is host-in-the-URL by
# design and there's nothing to pick from.
#
# Note this is reached through the catch-all rather than a registered "/" route:
# registering "/" would shadow the ROOT PATH OF EVERY PREVIEW SITE, since routes
# match on path across all hosts.
# ---------------------------------------------------------------------------
async def index(request: web.Request):
    if AUTH_HOST and request.host.split(":")[0].lower() == AUTH_HOST:
        # Local import: home.py reaches back into ensure_ready for its Refresh
        # button, so importing it at module scope would be a cycle.
        from .home import home_page
        return await home_page(request)

    base = PREVIEW_BASE_DOMAIN or "&lt;PREVIEW_BASE_DOMAIN unset&gt;"
    return web.Response(
        content_type="text/html",
        text=(
            "<h1>Sandbox preview proxy</h1>"
            f"<p>Open <code>https://&lt;sandboxId&gt;-&lt;port&gt;.{base}/</code>. The chat "
            "agent prints the full URL after it builds a site — it reads the id from "
            "<code>$DAYTONA_SANDBOX_ID</code> inside the sandbox.</p>"
        ),
    )
