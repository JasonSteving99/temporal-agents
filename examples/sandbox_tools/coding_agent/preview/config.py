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
# Sign-in happens only at https://login.<PREVIEW_BASE_DOMAIN>/, which the existing
# wildcard DNS + wildcard Caddy block already cover, so there's no new infra — and
# it's the ONLY host you must add to Firebase Console -> Authentication ->
# Settings -> Authorized domains. The session cookie is scoped to
# Domain=<PREVIEW_BASE_DOMAIN>, so one sign-in covers every sandbox subdomain.
#
# The gate is OFF when FIREBASE_API_KEY is unset, so the example still runs
# unconfigured (open). It is ON as soon as you set the key.
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "").strip()
FIREBASE_AUTH_DOMAIN = os.environ.get("FIREBASE_AUTH_DOMAIN", "").strip()
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "").strip()

AUTH_ENABLED = bool(FIREBASE_API_KEY and PREVIEW_BASE_DOMAIN)
AUTH_HOST = f"login.{PREVIEW_BASE_DOMAIN}" if PREVIEW_BASE_DOMAIN else ""

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
