"""
WHAT EXISTS — the set of preview sites the proxy has ever successfully served.

Registration is dynamic and implicit: nothing declares an app, the proxy simply
remembers every `<sandboxId>-<port>` it has forwarded a real response for. That's
enough to rebuild a gallery of every site the agent has built across sessions,
which is otherwise unrecoverable — sandbox ids only ever appear in a chat
transcript.

Storage differs from allowlist.py on purpose, because the access pattern is the
opposite way round:

  * The allowlist is read constantly and written by hand, so it is FILE
    authoritative (mtime-checked on read) and written through immediately.
  * This registry is written constantly (every proxied request touches last_seen)
    and read rarely, so it is MEMORY authoritative and flushed lazily. Writing
    through on every request would mean a JSON rewrite per image, per stylesheet,
    per XHR.

Lazy flushing means a hard kill can lose up to FLUSH_INTERVAL of `last_seen`
drift. That is the only field at risk, and it's cosmetic — registrations,
removals and screenshot updates are all flushed immediately.
"""

import asyncio
import json
import os
import time

from .config import APPS_PATH, MAX_APPS

FLUSH_INTERVAL = 60.0     # seconds; bounds how often last_seen churn hits disk


def app_key(sandbox_id: str, port: int) -> str:
    return f"{sandbox_id}-{port}"


class Registry:
    """Every preview site we've served, keyed "<sandboxId>-<port>"."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._apps: dict[str, dict] = {}
        self._dirty = False
        self._flushed_at = 0.0
        self._lock = asyncio.Lock()
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except Exception:
            # A corrupt registry is cosmetic — worst case the gallery starts
            # empty and refills as sites are visited. Never fail startup for it.
            return
        if isinstance(raw, dict):
            self._apps = {
                k: v for k, v in raw.items()
                if isinstance(v, dict) and isinstance(v.get("port"), int)
            }

    def _write(self) -> None:
        # Same write-temp-then-rename as the allowlist: a crash mid-write must not
        # leave a truncated file that loses every registration.
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._apps, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self._dirty = False
        self._flushed_at = time.time()

    def flush(self, force: bool = False) -> None:
        """Persist if dirty. Debounced unless forced (call forced on shutdown)."""
        if not self._dirty:
            return
        if force or time.time() - self._flushed_at >= FLUSH_INTERVAL:
            try:
                self._write()
            except Exception:
                pass   # a full/RO disk must never break proxying

    # -- reads -------------------------------------------------------------
    def get(self, key: str) -> "dict | None":
        return self._apps.get(key)

    def list(self) -> "list[dict]":
        """Pinned first, then newest-first — the order the gallery shows them in.

        Pinning is the one piece of ordering a human controls. Everything else is
        recency, which is right for a gallery that fills itself but wrong for the
        two or three sites you keep coming back to.
        """
        return sorted(
            self._apps.values(),
            key=lambda a: (bool(a.get("pinned")), a.get("last_seen", 0)),
            reverse=True,
        )

    def __contains__(self, key: str) -> bool:
        return key in self._apps

    # -- writes ------------------------------------------------------------
    def touch(self, sandbox_id: str, port: int) -> bool:
        """Record a successful proxy hit. Returns True if this app is NEW.

        Cheap by design — called on every proxied request, so it only mutates
        memory and lets flush() decide when that reaches disk.
        """
        key = app_key(sandbox_id, port)
        now = int(time.time())
        app = self._apps.get(key)
        if app is None:
            if len(self._apps) >= MAX_APPS:
                # Make room by dropping the least recently seen. Unbounded growth
                # would eventually make the gallery useless anyway.
                oldest = min(self._apps, key=lambda k: self._apps[k].get("last_seen", 0))
                self._apps.pop(oldest, None)
            self._apps[key] = {
                "key": key,
                "sandbox_id": sandbox_id,
                "port": port,
                "title": "",      # read off the page itself when we screenshot it
                "label": "",      # what a human called it; wins over title in the UI
                "pinned": False,
                "first_seen": now,
                "last_seen": now,
                "shot_at": 0,
            }
            self._dirty = True
            self.flush(force=True)      # a new app is worth an immediate write
            return True
        app["last_seen"] = now
        self._dirty = True
        self.flush()                    # debounced
        return False

    async def record_shot(self, key: str, title: str) -> None:
        async with self._lock:
            app = self._apps.get(key)
            if app is None:
                return
            app["shot_at"] = int(time.time())
            if title:
                app["title"] = title[:120]
            self._dirty = True
            self.flush(force=True)

    async def set_label(self, key: str, label: str) -> bool:
        """Name one app by hand. An empty label falls back to the page's own title."""
        async with self._lock:
            app = self._apps.get(key)
            if app is None:
                return False
            app["label"] = label.strip()[:80]
            self._dirty = True
            self.flush(force=True)
            return True

    async def set_pinned(self, key: str, pinned: bool) -> bool:
        async with self._lock:
            app = self._apps.get(key)
            if app is None:
                return False
            app["pinned"] = pinned
            self._dirty = True
            self.flush(force=True)
            return True

    async def forget(self, key: str) -> bool:
        async with self._lock:
            if self._apps.pop(key, None) is None:
                return False
            self._dirty = True
            self.flush(force=True)
            return True


registry = Registry(APPS_PATH)
