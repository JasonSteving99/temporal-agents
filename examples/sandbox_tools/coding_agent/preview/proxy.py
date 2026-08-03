"""
The proxying itself: parse the Host header into a sandbox, resume it if it's
paused, wait for its server to bind, then forward the request unchanged.

Also home to the Caddy `on_demand_tls` check, which decides which hostnames are
worth issuing a certificate for.
"""

import asyncio
import logging

from aiohttp import ClientSession, WSMsgType, web
from e2b import AsyncSandbox
from yarl import URL

from .config import (
    AGENT_ENABLED,
    AGENT_HOST,
    AGENT_UPSTREAM,
    AUTH_HOST,
    AUTO_STOP_MINUTES,
    E2B_API_KEY,
    PREVIEW_BASE_DOMAIN,
    SANDBOX_TIMEOUT_SECONDS,
)
from .pages import DENIED_PAGE, HELP_PAGE
from .registry import app_key, registry
from .screenshots import schedule_capture
from .session import can_use_agent, is_authed, redirect_to_login

logger = logging.getLogger(__name__)

# Headers that must not be blindly copied through a proxy.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# Reserved E2B ports to NEVER route to unless you mean it:
#   49983 = envd (the sandbox agent the SDK itself talks to), 50005 = MCP gateway.
RESERVED_PORTS = {49983, 50005}

# The header E2B requires on every request to a sandbox host when that sandbox was created
# with `allow_public_traffic=False`. It replaces Daytona's X-Daytona-Preview-Token, and is
# a straight improvement: Daytona's token travelled in preview links, whereas this one
# never leaves the proxy — so the sandbox's own e2b.app URL cannot be shared past the auth
# gate. Requests without it get 403.
TRAFFIC_TOKEN_HEADER = "e2b-traffic-access-token"


def preview_headers(token: str) -> dict[str, str]:
    """The auth header for upstream sandbox requests, or nothing.

    Empty when the sandbox allows public traffic (nothing to authenticate with) — which is
    what remote-box creates today, since its E2B config has no way to pass
    `allow_public_traffic=False`. Previews work either way; without it the sandbox's
    e2b.app URL is reachable by anyone who knows the sandbox id, bypassing this proxy's
    sign-in entirely.
    """
    return {TRAFFIC_TOKEN_HEADER: token} if token else {}


class Preview:
    """What the proxy needs to talk to one sandbox port: where, and with what header.

    Mirrors the shape Daytona's get_preview_link() returned (`.url` / `.token`) so the
    gallery, screenshotter and forwarders did not have to change with the backend.
    """

    __slots__ = ("token", "url")

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token


# Per-sandbox idle timers that pause the sandbox (see AUTO_STOP_MINUTES). Keyed by sandbox
# id; each request restarts its sandbox's timer.
_idle_pausers: dict[str, asyncio.Task] = {}


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
# We probe THROUGH the sandbox's real e2b.app URL so we test the real path.
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
                headers=preview_headers(token),
                allow_redirects=False,
            ) as resp:
                # Any HTTP response means something is listening. Tune if your
                # app returns e.g. 404 at "/" — you just want "not refused".
                if resp.status < 500 or resp.status == 404:
                    return
        except Exception:
            pass  # connection refused / not up yet — keep waiting
        await asyncio.sleep(0.5)
    raise RuntimeError(
        f"no server answered {url} within {timeout:.0f}s — is the agent's start.sh serving this port, "
        "bound to 0.0.0.0?"
    )


# ---------------------------------------------------------------------------
# Resume the sandbox if needed, then build its preview URL + traffic token.
# ---------------------------------------------------------------------------
async def _sandbox_state_and_deadline(sandbox_id: str):
    """(state, end_at) for a sandbox, or (None, None) if E2B wouldn't say.

    `end_at` is when E2B would KILL the sandbox for hitting its `timeout`. Nobody here wants
    that to happen — it is read as a HEARTBEAT, because every party that touches a sandbox
    rewrites it: remote-box calls `set_timeout` before each tool call, and our own `connect()`
    in ensure_ready sets it too. So an `end_at` that CHANGED since we last looked means
    "somebody used this sandbox", and one that didn't means "nobody did".

    Changed, not advanced: the two parties set different lifetimes (the worker's
    `sandbox_ttl_seconds` vs this proxy's SANDBOX_TIMEOUT_SECONDS), so a genuine touch can
    move the deadline BACKWARD — reading that as "idle" would pause a sandbox mid-turn, which
    is the exact failure this whole mechanism exists to prevent.
    """
    try:
        info = await AsyncSandbox.get_info(sandbox_id=sandbox_id, api_key=E2B_API_KEY)
    except Exception as e:
        logger.info("could not read state of %s — %s: %s", sandbox_id, type(e).__name__, e)
        return None, None
    return str(getattr(info, "state", "")).lower(), getattr(info, "end_at", None)


async def _pause_when_idle(sandbox_id: str) -> None:
    """Pause the sandbox once NOBODY has used it for AUTO_STOP_MINUTES — viewer or agent.

    E2B has no server-side idle stop, and its `timeout` KILLS rather than stops, so idling
    is managed here. pause() preserves memory as well as disk, so the agent's server is
    still running when the next request resumes it — no relaunch, no warm-up beyond the
    resume itself.

    THE AGENT'S ACTIVITY COUNTS, and it has to be read out of E2B rather than seen directly:
    the agent's tool calls go worker -> E2B, never through this proxy, so preview traffic
    alone is a false idle signal. Timing off preview traffic alone is what broke sessions —
    a visit armed the timer, the visitor went back to the chat to watch the agent work, and
    the pause landed squarely in the middle of a turn that was busy the whole time.

    And a pause under a running turn is NOT harmless, despite what this docstring used to
    claim. remote-box's session only auto-resumes a sandbox IT paused (`RemoteSession._paused`
    is per-session state, and `_resume_if_paused` is a no-op when it is False), so a pause
    that came from out here is invisible to it: the next tool call goes straight to
    `set_timeout` + `commands.run` against a paused sandbox and fails, and so does every
    retry. Hence the heartbeat check below, which is the whole point of the loop.

    The lifetime E2B reports (`end_at`) is that heartbeat — see
    :func:`_sandbox_state_and_deadline`. Each pass compares it to the previous pass; if it
    moved, the sandbox was in use and we simply wait another window.

    One limit worth knowing: the heartbeat ticks once per tool call, so a SINGLE tool call
    that runs longer than the whole idle window still looks idle. Keep AUTO_STOP_MINUTES
    comfortably above the longest tool activity timeout (tools.py's `bash` is 3 minutes) —
    the default leaves margin for exactly this reason.
    """
    window = AUTO_STOP_MINUTES * 60
    try:
        _state, last_seen = await _sandbox_state_and_deadline(sandbox_id)
        while True:
            await asyncio.sleep(window)
            state, end_at = await _sandbox_state_and_deadline(sandbox_id)
            if state is None:
                return  # gone, or E2B isn't answering — either way there's nothing to pause
            if state.endswith("paused"):
                return  # somebody else got there first (the agent pauses between turns too)
            if end_at is not None and end_at != last_seen:
                # Used since the last pass — not idle, go round again. Also the path taken
                # when `last_seen` is None because the very first read failed: adopt this
                # deadline as the baseline rather than pausing on no evidence.
                last_seen = end_at
                continue
            await AsyncSandbox.pause(sandbox_id=sandbox_id, api_key=E2B_API_KEY)
            return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Already paused, killed, or gone — nothing to salvage, and this must never take
        # down the request path that scheduled it.
        logger.info("idle pause of %s did not apply — %s: %s", sandbox_id, type(e).__name__, e)
    finally:
        _idle_pausers.pop(sandbox_id, None)


def cancel_idle_timers() -> None:
    """Drop every pending idle-pause task. Called on shutdown.

    Deliberately does NOT pause the sandboxes it was tracking: a proxy restart is not a
    reason to suspend a session the agent may be actively using. The consequence is that a
    sandbox resumed just before a restart keeps running until SANDBOX_TIMEOUT_SECONDS — the
    backstop that exists for exactly this case.
    """
    for task in list(_idle_pausers.values()):
        task.cancel()
    _idle_pausers.clear()


def _restart_idle_timer(sandbox_id: str) -> None:
    if not AUTO_STOP_MINUTES:
        return
    existing = _idle_pausers.pop(sandbox_id, None)
    if existing:
        existing.cancel()
    _idle_pausers[sandbox_id] = asyncio.create_task(_pause_when_idle(sandbox_id))


async def ensure_ready(sandbox_id: str, port: int, session: ClientSession):
    """Returns (preview, woken) — `woken` is True if the sandbox had to be resumed.

    Callers use `woken` to decide whether the site may have changed since its last
    screenshot.

    connect() both resumes a paused sandbox and refreshes its lifetime, so it is the whole
    wake step. The state is read FIRST only to report `woken`; connect() alone can't tell
    us whether it resumed anything.
    """
    woken = False
    try:
        info = await AsyncSandbox.get_info(sandbox_id=sandbox_id, api_key=E2B_API_KEY)
        woken = str(getattr(info, "state", "")).lower().endswith("paused")
    except Exception:
        # Info is a nicety; a failure here shouldn't cost us the preview. connect() below
        # is the call that must work, and it raises usefully if the sandbox is truly gone.
        pass

    sandbox = await AsyncSandbox.connect(
        sandbox_id, timeout=SANDBOX_TIMEOUT_SECONDS, api_key=E2B_API_KEY
    )
    # Empty unless the sandbox was created with allow_public_traffic=False — see
    # preview_headers().
    preview = Preview(
        url=f"https://{sandbox.get_host(port)}",
        token=sandbox.traffic_access_token or "",
    )
    _restart_idle_timer(sandbox_id)
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
# tokens and sandbox compute, so viewing previews is not enough: the caller must
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
# WebSocket passthrough (HMR, live reload): bridge client <-> upstream, carrying
# the traffic token + forwarded host.
# ---------------------------------------------------------------------------
async def proxy_ws(request, sandbox_id, port, path, session):
    try:
        preview, _woken = await ensure_ready(sandbox_id, port, session)
    except Exception as e:
        logger.warning("preview websocket unavailable for %s:%s — %s: %s",
                       sandbox_id, port, type(e).__name__, e)
        return web.Response(status=503, text="Warming up…")

    client_ws = web.WebSocketResponse()
    await client_ws.prepare(request)

    up_url = upstream_url(preview.url, path, request.rel_url.query)
    up_url = up_url.with_scheme("wss" if up_url.scheme == "https" else "ws")

    async with session.ws_connect(
        str(up_url),
        headers={
            **preview_headers(preview.token),
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
        return web.Response(status=403, text=f"Port {port} is reserved by E2B.")

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
        # Log it: the page below is the only other place this appears, and reading it requires being
        # signed in and looking at the right tab at the right moment. A preview that "just doesn't
        # load" is otherwise indistinguishable from DNS, TLS or auth trouble, none of which are here.
        logger.warning("preview unavailable for %s:%s — %s: %s",
                       sandbox_id, port, type(e).__name__, e)
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
        # Apps behind the proxy see the public host, not the e2b.app one, so generated
        # absolute URLs point back through the gate.
        "X-Forwarded-Host": request.headers.get("Host", ""),
        **preview_headers(preview.token),
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
        # Raises if the sandbox id doesn't exist, so Caddy never issues a certificate for a
        # hostname nobody can serve. A PAUSED sandbox is still a real one — get_info answers
        # for it, and connect() would resume it — so previews survive the idle pause.
        await AsyncSandbox.get_info(sandbox_id=sandbox_id, api_key=E2B_API_KEY)
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
        text=HELP_PAGE.replace("__BASE__", base),
    )
