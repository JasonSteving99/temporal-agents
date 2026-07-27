"""
WHO GETS IN — three tiers, deliberately stored in two different places.

  ADMIN    from the PREVIEW_ADMIN_EMAILS env var (config.ADMIN_EMAILS).
           Previews + the agent + the admin panel. Immutable at runtime:
           nothing served over HTTP can change it, so becoming an admin
           requires shell access to the host plus a restart.
  AGENT    a member whose role is "agent". Previews + the agent, no panel.
  PREVIEW  a member whose role is "preview". Previews only. The default.

The last two live in a JSON file on a Docker volume, editable live from the
admin panel (auth.py). No restart, no redeploy.

Admins are kept OUT of that file on purpose. If they were read from the same
place the panel writes, then the panel — the thing members can never see, but
any future bug might expose — would double as a privilege-escalation primitive,
and one bad write would hand out root. Splitting them puts a ceiling on what a
compromised panel can do, which is why this module exposes no way to create an
admin and why adding one would be a mistake.

Note the AGENT tier raises that ceiling: the panel can now grant something that
spends money (model tokens, Daytona compute), where before the worst it could
hand out was reading previews. It still cannot grant the ability to grant, so a
mistake here costs tokens, not control — but "promote to agent" deserves more
care than "add a guest", and only an admin can do it.

The effective allowlist is ADMINS | members, so an admin can never lock
themselves out by clearing the list.
"""

import asyncio
import json
import os

from .config import ADMIN_EMAILS, ALLOWLIST_PATH, MAX_GUESTS

_EMAIL_MAX_LEN = 254      # RFC 5321

ROLE_PREVIEW = "preview"
ROLE_AGENT = "agent"
ROLES = (ROLE_PREVIEW, ROLE_AGENT)


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
    """Members and their roles: a JSON object on disk, cached in memory.

    Reads are hot (every proxied request calls allows()), writes are rare, so the
    cache is refreshed only when the file's mtime changes. That also means editing
    the JSON by hand on the host takes effect without a restart.

    On-disk shape is {"members": {"<email>": "<role>"}}. A bare JSON array is also
    accepted and read as all-"preview" — that's the format this file had before
    roles existed, and a deployment that upgrades must not lock out everyone who
    was already on the list. The array is rewritten into the new shape on the next
    write.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._members: dict[str, str] = {}
        self._mtime: int | None = None
        self._lock = asyncio.Lock()   # serialises read-modify-write
        self._load()

    @staticmethod
    def _parse(raw: object) -> "dict[str, str]":
        # Legacy: a plain array of emails, from before roles existed.
        if isinstance(raw, list):
            return {e: ROLE_PREVIEW for e in map(clean_email, raw) if e}
        if not isinstance(raw, dict):
            return {}
        members = raw.get("members")
        if not isinstance(members, dict):
            return {}
        out: dict[str, str] = {}
        for addr, role in members.items():
            email = clean_email(addr)
            if email:
                # Unknown role -> the least privileged one, never the most.
                out[email] = role if role in ROLES else ROLE_PREVIEW
        return out

    def _load(self) -> None:
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            self._members, self._mtime = {}, None   # no file yet == no members
            return
        if mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                # Fail CLOSED on a malformed file: an unreadable list must not be
                # read as "allow everyone". Admins come from env, so you can always
                # sign in and repair it through the panel.
                self._members = self._parse(json.load(fh))
        except Exception:
            self._members = {}
        self._mtime = mtime

    def members(self) -> "list[dict]":
        self._load()
        return [{"email": e, "role": self._members[e]} for e in sorted(self._members)]

    def role_of(self, email: str) -> "str | None":
        self._load()
        return self._members.get(email)

    def allows(self, email: str) -> bool:
        """May they view previews? Every tier may."""
        self._load()
        return email in ADMIN_EMAILS or email in self._members

    def allows_agent(self, email: str) -> bool:
        """May they run the agent? Admins and the "agent" role only."""
        self._load()
        return email in ADMIN_EMAILS or self._members.get(email) == ROLE_AGENT

    def _persist(self, members: "dict[str, str]") -> None:
        # Write-temp-then-rename: os.replace is atomic, so a crash mid-write can
        # never leave a truncated file that locks every member out.
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"members": dict(sorted(members.items()))}, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self._members = members
        self._mtime = os.stat(self.path).st_mtime_ns

    async def add(self, email: str, role: str = ROLE_PREVIEW) -> "tuple[bool, str]":
        if role not in ROLES:
            return False, "Unknown role."
        async with self._lock:
            self._load()
            if email in ADMIN_EMAILS:
                # Not an error worth failing on, but say so: admins are allowed by
                # env already, and storing them here would imply the file grants it.
                return False, f"{email} is an admin — already has access."
            if email in self._members:
                return False, f"{email} is already on the list."
            if len(self._members) >= MAX_GUESTS:
                return False, f"List is full ({MAX_GUESTS} max)."
            self._persist({**self._members, email: role})
            return True, f"Added {email}."

    async def set_role(self, email: str, role: str) -> "tuple[bool, str]":
        if role not in ROLES:
            return False, "Unknown role."
        async with self._lock:
            self._load()
            if email in ADMIN_EMAILS:
                # Admin is env-only; letting the panel "demote" one here would be a
                # lie, since ADMIN_EMAILS would still grant everything on the next
                # request. Refuse rather than pretend.
                return False, f"{email} is an admin — change PREVIEW_ADMIN_EMAILS instead."
            if email not in self._members:
                return False, f"{email} is not on the list."
            if self._members[email] == role:
                return False, f"{email} already has that access."
            self._persist({**self._members, email: role})
            label = "can now run the agent" if role == ROLE_AGENT else "is back to previews only"
            return True, f"{email} {label}."

    async def remove(self, email: str) -> "tuple[bool, str]":
        async with self._lock:
            self._load()
            if email not in self._members:
                return False, f"{email} is not on the list."
            rest = dict(self._members)
            rest.pop(email)
            self._persist(rest)
            return True, f"Removed {email}."


allowlist = Allowlist(ALLOWLIST_PATH)
