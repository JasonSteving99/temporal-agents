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
    AUTH_ENABLED,
    AUTH_HOST,
    AUTO_STOP_MINUTES,
    DAYTONA_API_KEY,
    DAYTONA_TARGET,
    PREVIEW_BASE_DOMAIN,
)
from .session import is_authed, redirect_to_login


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
    sandbox = await daytona.get(sandbox_id)
    if sandbox.state != SandboxState.STARTED:
        # This is where the snapshot entrypoint supervisor (supervise.sh) re-runs
        # /home/daytona/project/start.sh and relaunches the server. start() waits until
        # the sandbox itself is "started" (not until the server binds).
        await sandbox.start()
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
    return preview


# ---------------------------------------------------------------------------
# Rebuild the upstream URL. The path is forwarded UNCHANGED (subdomain
# routing means there's no prefix to strip) so absolute asset paths just work.
# ---------------------------------------------------------------------------
def upstream_url(preview_url: str, path: str, query: "URL") -> URL:
    base = URL(preview_url)
    return (base.origin() / path.lstrip("/")).with_query(query)


# ---------------------------------------------------------------------------
# WebSocket passthrough (HMR, live reload). Daytona auto-detects the upgrade
# and skips its warning page; we just bridge client <-> upstream and carry
# the token + forwarded host.
# ---------------------------------------------------------------------------
async def proxy_ws(request, sandbox_id, port, path, session):
    try:
        preview = await ensure_ready(sandbox_id, port, session)
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
        preview = await ensure_ready(sandbox_id, port, session)
    except Exception as e:
        # Serve a branded "warming up" page instead of a raw 502.
        return web.Response(
            status=503, content_type="text/html",
            text=f"<h1>Warming up…</h1><p>{e}</p>",
        )

    target = upstream_url(preview.url, path, request.rel_url.query)

    out_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP
    }
    out_headers["X-Forwarded-Host"] = request.headers.get("Host", "")  # required by Daytona
    out_headers["X-Daytona-Preview-Token"] = preview.token             # fresh, post-start
    out_headers["X-Daytona-Skip-Preview-Warning"] = "true"             # we own the UX

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
# Caddy `on_demand_tls` gate: https://caddyserver.com/docs/caddyfile/options#on-demand-tls
#     Caddy calls this BEFORE issuing/renewing a cert for a hostname it doesn't already have
#     one for, as `GET /check?domain=<hostname>`, and only proceeds on a 2xx. Caddy does no
#     rate limiting of its own here, so this is the only thing standing between a public
#     wildcard preview domain and burning the ACME weekly cert quota on scanner-guessed
#     subdomains — a non-2xx must be the default for anything that isn't a live sandbox.
# ---------------------------------------------------------------------------
async def check(request: web.Request) -> web.Response:
    domain = request.query.get("domain", "").strip().lower()
    # The sign-in host is a real site we serve, so Caddy must be allowed a cert for
    # it — without this, login.<base> is unreachable and nobody can get past the gate.
    if AUTH_ENABLED and domain == AUTH_HOST:
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
# Help page for the bare base domain (or any non-preview host). No sandbox
# picker (routing is host-in-the-URL by design) — just usage text.
# ---------------------------------------------------------------------------
async def index(request: web.Request):
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
