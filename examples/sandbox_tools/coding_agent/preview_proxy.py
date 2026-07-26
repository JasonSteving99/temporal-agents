"""
Daytona custom preview proxy for the sandboxed coding agent — SUBDOMAIN routing.

Purpose: sit in front of Daytona's preview endpoint so a stopped CONTAINER
sandbox is woken on demand and the server (relaunched by the snapshot's
supervise.sh entrypoint) is actually listening before we forward. That
start-and-wait gate is what makes a stopped-then-woken sandbox serve traffic
seamlessly, so you can build a web app inside the sandbox via the chat agent and
preview it live.

Routing is by SUBDOMAIN (parsed from the Host header), not by path:

    https://<sandboxId>-<port>.<PREVIEW_BASE_DOMAIN>/<path...>?<query>

e.g. https://abc123-3000.preview.example.com/assets/app.js . The leftmost DNS
label encodes the target: `<sandboxId>-<port>` (split on the LAST hyphen, since
Daytona ids may contain hyphens and the port is always the trailing number).
The request path is forwarded UNCHANGED to the sandbox, so a site that
references absolute roots (`/assets/app.js`, `/style.css`) works exactly as it
would when served normally — no `<base href>`, no relative-path gymnastics, no
prefix rewriting. That's the whole reason for subdomain routing.

Deployment (see deploy/README.md): point a WILDCARD DNS record
`*.<PREVIEW_BASE_DOMAIN>` at the host, and run a reverse proxy (Caddy/Traefik/
nginx) that terminates wildcard TLS and forwards to this proxy PRESERVING the
Host header. This proxy itself speaks plain HTTP and only reads Host.

This is deliberately SELF-CONTAINED to the example: it is its own aiohttp server
and touches nothing in `temporal_agent_harness.web`. It is a teaching skeleton,
not production code — start from the official samples for anything real:
https://github.com/daytonaio/daytona-proxy-samples

Env:   DAYTONA_API_KEY (required); PREVIEW_BASE_DOMAIN (required, e.g.
       "preview.example.com"); DAYTONA_TARGET (optional region, e.g. "us");
       PREVIEW_PROXY_PORT (optional, default 8080).
"""

import asyncio
import os

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from yarl import URL

from daytona import AsyncDaytona, DaytonaConfig, SandboxState


def _make_daytona() -> AsyncDaytona:
    # One shared client for the whole process. The async client opens a single
    # state-streaming websocket that all sandboxes share, so do NOT construct one
    # per request — build it once and close it on shutdown.
    kwargs = {"api_key": os.environ["DAYTONA_API_KEY"]}
    target = os.environ.get("DAYTONA_TARGET")  # e.g. "us" — omit to use org default
    if target:
        kwargs["target"] = target
    return AsyncDaytona(DaytonaConfig(**kwargs))


daytona = _make_daytona()

# The base domain preview subdomains hang off of, e.g. "preview.example.com". A request to
# "<sandboxId>-<port>.<this>" is routed to that sandbox's port. Required — with it unset every
# request just gets the help page (there's nothing to parse a sandbox out of).
PREVIEW_BASE_DOMAIN = os.environ.get("PREVIEW_BASE_DOMAIN", "").strip().lower()

# Headers that must not be blindly copied through a proxy.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# Reserved Daytona ports to NEVER route to unless you mean it:
#   22222 = web terminal, 2280 = toolbox, 33333 = recording dashboard.
RESERVED_PORTS = {22222, 2280, 33333}

# Idle cost cap. Without this, a woken sandbox keeps billing for compute until
# something else stops it — and preview traffic alone never will. So the proxy
# tells Daytona to auto-stop the sandbox after this many idle minutes. This is a
# PROXY-scoped concern on purpose: the harness that CREATES the sandbox is left
# untouched. Set 0 to leave the sandbox's auto-stop as-is (don't manage it).
AUTO_STOP_MINUTES = int(os.environ.get("PREVIEW_AUTO_STOP_MINUTES", "3"))

# Sandbox ids we've already applied AUTO_STOP_MINUTES to. Daytona persists the
# setting on the sandbox, so once per id is enough; setting it on every request
# would add a needless API round-trip. A restarted proxy just re-applies on the
# first request it sees for each sandbox.
_autostop_configured: set[str] = set()


# ---------------------------------------------------------------------------
# 0. Host parsing: pull (sandbox_id, port) out of the Host header. Returns None
#    for anything that isn't a "<sandboxId>-<port>.<PREVIEW_BASE_DOMAIN>" host
#    (the bare base domain, an IP, a stray Host) — those get the help page.
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
# 1. Readiness gate: after the sandbox reports "started", the server inside it
#    still needs a moment to bind the port. Poll until it answers, or we 503.
#    We probe THROUGH Daytona's preview URL so we test the real path.
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
# 2. Wake the sandbox if needed, then get a FRESH preview link + token.
#    Order matters: a standard preview token is invalidated when the sandbox
#    restarts, so a token cached from before the stop is dead. Fetch it AFTER
#    start(). get_preview_link also auto-opens the port if it's closed.
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
# 3. Rebuild the upstream URL. The path is forwarded UNCHANGED (subdomain
#    routing means there's no prefix to strip) so absolute asset paths just work.
# ---------------------------------------------------------------------------
def upstream_url(preview_url: str, path: str, query: "URL") -> URL:
    base = URL(preview_url)
    return (base.origin() / path.lstrip("/")).with_query(query)


# ---------------------------------------------------------------------------
# 4. WebSocket passthrough (HMR, live reload). Daytona auto-detects the upgrade
#    and skips its warning page; we just bridge client <-> upstream and carry
#    the token + forwarded host.
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
# 5. Main handler: parse Host -> (help page if not a preview host) -> (ws?) ->
#    ensure ready -> forward the request path unchanged.
# ---------------------------------------------------------------------------
async def handler(request: web.Request):
    # 5a. YOUR auth would go here. This demo proxy is intentionally open.
    session: ClientSession = request.app["session"]

    route = parse_preview_host(request.host)
    if route is None:
        return await index(request)   # bare base domain / unknown host -> usage help
    sandbox_id, port = route

    if port in RESERVED_PORTS:
        return web.Response(status=403, text=f"Port {port} is reserved by Daytona.")

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
# 6. Help page for the bare base domain (or any non-preview host). No sandbox
#    picker (routing is host-in-the-URL by design) — just usage text.
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


# ---------------------------------------------------------------------------
# 7. App wiring: shared HTTP session + SDK lifecycle.
# ---------------------------------------------------------------------------
async def on_startup(app):
    app["session"] = ClientSession(timeout=ClientTimeout(total=None))


async def on_cleanup(app):
    await app["session"].close()
    await daytona.close()   # closes the SDK's shared state-streaming websocket


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    # One catch-all: Host decides the sandbox, the path is forwarded verbatim.
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), port=int(os.environ.get("PREVIEW_PROXY_PORT", "8080")))
