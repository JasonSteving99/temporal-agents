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

Access is gated by Firebase Authentication (Google sign-in): sandbox previews are
otherwise readable by anyone who can guess a subdomain. See `session.py` for the
gate and `allowlist.py` for who gets through it. The base domain itself —
`https://<PREVIEW_BASE_DOMAIN>/` — is not forwarded anywhere; we serve the
gallery, sign-in and admin panel there ourselves (`home.py`).

Deployment (see deploy/README.md): point a WILDCARD DNS record
`*.<PREVIEW_BASE_DOMAIN>` at the host, and run a reverse proxy (Caddy/Traefik/
nginx) that terminates TLS per-subdomain (Caddy `on_demand_tls`, gated by the
`GET /check` route in `proxy.py`) and forwards to this proxy PRESERVING the Host
header. This proxy itself speaks plain HTTP and only reads Host.

This is deliberately SELF-CONTAINED to the example: it is its own aiohttp server
and touches nothing in `temporal_agent_harness.web`. It is a teaching skeleton,
not production code — start from the official samples for anything real:
https://github.com/daytonaio/daytona-proxy-samples

Run it with `python -m examples.sandbox_tools.coding_agent.preview`.


THE MAP — modules bottom-up, each importing only the ones above it:

    config.py      Every environment knob, in one place. No logic.
    allowlist.py   WHO may view previews: env-fixed admins + a file-backed,
                   live-editable guest list. The privilege split lives here.
    registry.py    WHAT exists: every preview site the proxy has ever served,
                   registered implicitly as it forwards traffic.
    session.py     The gate itself: signed session cookies, and the is_authed /
                   is_admin predicates every request is checked against.
    screenshots.py Headless-browser capture, taken only while a sandbox is
                   already awake — the reason the gallery is images, not iframes.
    pages.py       The three HTML pages (sign-in, admin panel, gallery). Markup only.
    auth.py        HTTP handlers for signing in and for editing the guest list.
    home.py        The gallery and its routes.
    proxy.py       The actual proxying: wake the sandbox, wait for its server,
                   forward the request. Plus the Caddy cert-issuance check.
    app.py         Route table + lifecycle. The order routes are added in is
                   load-bearing — read the comments there.

Start at `allowlist.py` if you care about the security model, `proxy.py` if you
care about how a request reaches a sandbox, `screenshots.py` if you care about
what the gallery costs to run.
"""
