"""
Every environment variable the preview proxy reads, gathered in one place.

Deliberately logic-free: it's the single file to open when you want to know what
is configurable and what the defaults are. Everything else imports from here
rather than reaching for os.environ, so no knob can hide in the middle of a
handler.
"""

import os
import secrets

# --- Daytona --------------------------------------------------------------
DAYTONA_API_KEY = os.environ["DAYTONA_API_KEY"]          # required
DAYTONA_TARGET = os.environ.get("DAYTONA_TARGET")        # e.g. "us"; None = org default

# --- Where we serve ------------------------------------------------------
# The base domain preview subdomains hang off of, e.g. "preview.example.com". A request to
# "<sandboxId>-<port>.<this>" is routed to that sandbox's port. Required — with it unset every
# request just gets the help page (there's nothing to parse a sandbox out of).
PREVIEW_BASE_DOMAIN = os.environ.get("PREVIEW_BASE_DOMAIN", "").strip().lower()

PROXY_PORT = int(os.environ.get("PREVIEW_PROXY_PORT", "8080"))

# Idle cost cap. Without this, a woken sandbox keeps billing for compute until
# something else stops it — and preview traffic alone never will. So the proxy
# tells Daytona to auto-stop the sandbox after this many idle minutes. This is a
# PROXY-scoped concern on purpose: the harness that CREATES the sandbox is left
# untouched. Set 0 to leave the sandbox's auto-stop as-is (don't manage it).
AUTO_STOP_MINUTES = int(os.environ.get("PREVIEW_AUTO_STOP_MINUTES", "3"))

# --- Firebase auth gate ---------------------------------------------------
# The gate is OFF when FIREBASE_API_KEY is unset, so the example still runs
# unconfigured (open). It is ON as soon as you set the key.
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "").strip()
FIREBASE_AUTH_DOMAIN = os.environ.get("FIREBASE_AUTH_DOMAIN", "").strip()
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "").strip()

# THE ONE HOST WE SERVE OURSELVES — the gallery, sign-in, and the admin panel all
# live here; everything else is a sandbox subdomain we forward. It defaults to the
# base domain, so the gallery is simply https://<PREVIEW_BASE_DOMAIN>/.
#
# That default asks slightly more of your DNS than the wildcard alone: `*.<base>`
# does NOT match `<base>` itself, so the base domain needs its own A/AAAA record
# and its own Caddy site block (see deploy/README.md). If you can't add those —
# e.g. <base> is a zone apex your DNS host won't point at an IP — set
# PREVIEW_AUTH_HOST to a subdomain the wildcard already covers, such as
# "login.<base>", and no DNS or Caddy change is needed at all.
#
# Whichever you pick, this is the ONLY host to add to Firebase Console ->
# Authentication -> Settings -> Authorized domains, because it's the only origin
# sign-in ever runs on. The session cookie is scoped to Domain=<PREVIEW_BASE_DOMAIN>,
# which covers the base domain AND every sandbox subdomain either way.
AUTH_HOST = os.environ.get("PREVIEW_AUTH_HOST", "").strip().lower() or PREVIEW_BASE_DOMAIN

AUTH_ENABLED = bool(FIREBASE_API_KEY and PREVIEW_BASE_DOMAIN)

# Key that signs session cookies. Unset -> random per process, i.e. everyone is
# logged out whenever the proxy restarts. Set it in .env to avoid that.
SESSION_SECRET = (
    os.environ.get("PREVIEW_SESSION_SECRET", "").strip() or secrets.token_hex(32)
).encode()
SESSION_TTL = int(os.environ.get("PREVIEW_SESSION_HOURS", "168")) * 3600  # default 7d
COOKIE_NAME = "preview_session"

# --- Who gets in (see allowlist.py for why these two live apart) ----------
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("PREVIEW_ADMIN_EMAILS", "").split(",")
    if e.strip()
}

# Where the guest list is persisted. Mount a volume here (see docker-compose.yml)
# or the list resets on every container replacement.
ALLOWLIST_PATH = os.environ.get(
    "PREVIEW_ALLOWLIST_PATH", "/data/preview-auth/allowed_emails.json"
)

MAX_GUESTS = 200          # bounds the file; also bounds the panel's DOM

# --- The agent chat app, reverse-proxied ADMINS ONLY ----------------------
# The agent UI spends real money (model tokens, Daytona compute) on every message,
# so unlike previews it is never opened to guests — the gate here is is_admin, not
# is_authed. That's the whole reason this lives behind the same proxy instead of
# being published directly: the admin check already exists and is already tested.
#
# AGENT_HOST needs no new DNS record or Caddy block — it's an ordinary
# "<label>.<PREVIEW_BASE_DOMAIN>" name, so the wildcard record and the wildcard
# Caddy site block already cover it, exactly like a sandbox subdomain. It can't
# collide with one either: sandbox hosts must end in "-<port>".
AGENT_HOST = os.environ.get("PREVIEW_AGENT_HOST", "").strip().lower() or (
    f"agent.{PREVIEW_BASE_DOMAIN}" if PREVIEW_BASE_DOMAIN else ""
)

# Where the FastAPI server actually listens, e.g. "http://server:8000" (the compose
# service name). EMPTY BY DEFAULT — opting in is deliberate, because it's the switch
# that takes the agent from "reachable only over Tailscale" to "reachable from the
# public internet, behind the admin gate".
AGENT_UPSTREAM = os.environ.get("PREVIEW_AGENT_UPSTREAM", "").strip().rstrip("/")
AGENT_ENABLED = bool(AGENT_HOST and AGENT_UPSTREAM)


# --- The app gallery (registry.py, screenshots.py, home.py) ---------------
# Where the "what preview sites exist" registry and their screenshots live. Mount
# a volume here or the gallery empties on every container replacement.
APPS_PATH = os.environ.get("PREVIEW_APPS_PATH", "/data/preview-apps/apps.json")
SHOTS_DIR = os.environ.get("PREVIEW_SHOTS_DIR", "/data/preview-apps/shots")
MAX_APPS = int(os.environ.get("PREVIEW_MAX_APPS", "300"))

# Screenshots need Playwright + Chromium in the image (see Dockerfile). Set to 0
# to skip capture entirely and run the gallery on placeholder tiles.
SCREENSHOTS_ENABLED = os.environ.get("PREVIEW_SCREENSHOTS", "1").strip() not in ("0", "false", "")
SHOT_WIDTH = int(os.environ.get("PREVIEW_SHOT_WIDTH", "1280"))
SHOT_HEIGHT = int(os.environ.get("PREVIEW_SHOT_HEIGHT", "800"))
SHOT_QUALITY = int(os.environ.get("PREVIEW_SHOT_QUALITY", "72"))   # jpeg
SHOT_TIMEOUT_MS = int(os.environ.get("PREVIEW_SHOT_TIMEOUT_MS", "20000"))
