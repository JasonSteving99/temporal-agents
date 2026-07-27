"""
WHO GETS IN — two tiers, deliberately stored in two different places.

  ADMINS  come from the PREVIEW_ADMIN_EMAILS env var (config.ADMIN_EMAILS).
          Immutable at runtime: nothing served over HTTP can change them, so
          changing who is an admin requires shell access to the host plus a
          restart.
  GUESTS  live in a JSON file on a Docker volume, editable live through the
          admin panel (auth.py). No restart, no redeploy.

Keeping those in ONE place would be the whole vulnerability: if admins were read
from the same file the panel writes, then the panel — the thing guests can never
see, but any future bug might expose — would double as a privilege-escalation
primitive, and one bad write would hand out root. Splitting them means the worst
a compromised panel can do is grant *preview* access, never admin. That's why
this module exposes no way to add an admin, and why adding one would be a
mistake.

The effective allowlist is ADMINS | GUESTS, so an admin can never lock themselves
out by clearing the guest list.
"""

import asyncio
import json
import os

from .config import ADMIN_EMAILS, ALLOWLIST_PATH, MAX_GUESTS

_EMAIL_MAX_LEN = 254      # RFC 5321


def clean_email(raw: object) -> "str | None":
    """Normalise one address, or None if it isn't plausibly an email.

    Deliberately loose — the real validation is that Google had to authenticate
    the address before it means anything. This only keeps junk out of the file.
    """
    if not isinstance(raw, str):
        return None
    email = raw.strip().lower()
    if not email or len(email) > _EMAIL_MAX_LEN or any(c.isspace() for c in email):
        return None
    local, sep, domain = email.partition("@")
    if not sep or not local or "." not in domain or domain.startswith("."):
        return None
    return email


class Allowlist:
    """The guest list: a JSON array of emails on disk, cached in memory.

    Reads are hot (every proxied request calls allows()), writes are rare, so the
    cache is refreshed only when the file's mtime changes. That also means editing
    the JSON by hand on the host takes effect without a restart.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._guests: set[str] = set()
        self._mtime: int | None = None
        self._lock = asyncio.Lock()   # serialises read-modify-write
        self._load()

    def _load(self) -> None:
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            self._guests, self._mtime = set(), None   # no file yet == no guests
            return
        if mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
            # Fail CLOSED on a malformed file: an unreadable list must not be read
            # as "allow everyone". Admins come from env, so you can always sign in
            # and repair it through the panel.
            self._guests = (
                {e for e in map(clean_email, raw) if e} if isinstance(raw, list) else set()
            )
        except Exception:
            self._guests = set()
        self._mtime = mtime

    def guests(self) -> "list[str]":
        self._load()
        return sorted(self._guests)

    def allows(self, email: str) -> bool:
        self._load()
        return email in ADMIN_EMAILS or email in self._guests

    def _persist(self, emails: "set[str]") -> None:
        # Write-temp-then-rename: os.replace is atomic, so a crash mid-write can
        # never leave a truncated file that locks every guest out.
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(sorted(emails), fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self._guests = emails
        self._mtime = os.stat(self.path).st_mtime_ns

    async def add(self, email: str) -> "tuple[bool, str]":
        async with self._lock:
            self._load()
            if email in ADMIN_EMAILS:
                # Not an error worth failing on, but say so: admins are allowed by
                # env already, and storing them here would imply the file grants it.
                return False, f"{email} is an admin — already has access."
            if email in self._guests:
                return False, f"{email} is already on the list."
            if len(self._guests) >= MAX_GUESTS:
                return False, f"List is full ({MAX_GUESTS} max)."
            self._persist(self._guests | {email})
            return True, f"Added {email}."

    async def remove(self, email: str) -> "tuple[bool, str]":
        async with self._lock:
            self._load()
            if email not in self._guests:
                return False, f"{email} is not on the list."
            self._persist(self._guests - {email})
            return True, f"Removed {email}."


allowlist = Allowlist(ALLOWLIST_PATH)
