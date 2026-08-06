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

ONE RECORD IS NOT LIKE THE OTHERS. A `kept` app (see keep.py) is a fork whose
sandbox no chat session owns, so this file is the ONLY place its id is written
down — a chat transcript won't have it, because no agent ever saw it. Two rules
follow, and both are enforced below rather than left to callers:

  * kept records are never evicted to make room (`touch`), and
  * `forget`ting one is the only way to lose it, which is why the UI makes that
    the same gesture as destroying the sandbox.
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
            self._evict_one()
            self._apps[key] = self._record(sandbox_id, port, now)
            self._dirty = True
            self.flush(force=True)      # a new app is worth an immediate write
            return True
        app["last_seen"] = now
        # We just served it, so it is demonstrably awake — whatever a pause we
        # recorded earlier says (see mark_sandbox_paused).
        app["asleep_since"] = 0
        self._dirty = True
        self.flush()                    # debounced
        return False

    @staticmethod
    def _record(sandbox_id: str, port: int, now: int) -> dict:
        return {
            "key": app_key(sandbox_id, port),
            "sandbox_id": sandbox_id,
            "port": port,
            "title": "",          # read off the page itself when we screenshot it
            "label": "",          # what a human called it; wins over title in the UI
            "pinned": False,
            "first_seen": now,
            "last_seen": now,
            "shot_at": 0,
            # When WE paused this sandbox, so the gallery can state its sleep
            # rather than infer it from the clock. 0 means "we never did", which
            # is every record written before this field existed.
            "asleep_since": 0,
            # Set only by keep.py. `kept` makes the record unevictable; the other
            # two are provenance for the card ("kept from <key>, on <date>").
            "kept": False,
            "kept_at": 0,
            "forked_from": "",
        }

    def _evict_one(self) -> None:
        """Drop the least recently seen app once we're at MAX_APPS.

        KEPT apps are excluded from the candidates, not merely sorted last. They
        are the only records whose loss is unrecoverable — a kept sandbox's id
        exists nowhere else — so an unrelated burst of new previews must never be
        able to age one out. If every record is kept there is nothing to evict and
        the map grows past the cap; that is the right failure, and it is bounded
        by how many sandboxes an admin chose to keep by hand.
        """
        if len(self._apps) < MAX_APPS:
            return
        candidates = [k for k, v in self._apps.items() if not v.get("kept")]
        if not candidates:
            return
        self._apps.pop(min(candidates, key=lambda k: self._apps[k].get("last_seen", 0)), None)

    async def register_kept(self, sandbox_id: str, port: int, source: dict) -> dict:
        """Record a forked sandbox as a kept app, and return the new record.

        The name carries over from the source, because "Keep" means keep THIS
        site: arriving as an untitled copy would make a gallery of forks
        unreadable, and the screenshot that follows would only re-title it with
        the same `<title>` the original already has.
        """
        async with self._lock:
            now = int(time.time())
            app = self._record(sandbox_id, port, now)
            app.update(
                title=source.get("title", ""),
                label=source.get("label", ""),
                kept=True,
                kept_at=now,
                forked_from=source.get("key", ""),
            )
            self._apps[app["key"]] = app
            self._dirty = True
            self.flush(force=True)
            return app

    async def mark_sandbox_paused(self, sandbox_id: str) -> None:
        """Record that we paused this sandbox — for every port it serves.

        The gallery otherwise infers sleep from "no traffic for AUTO_STOP_MINUTES",
        which is right eventually but wrong for the minutes right after a pause we
        performed ourselves. Keeping the pause here makes those cards honest
        immediately, and it matters most for a just-kept app: keep.py parks the
        fork the moment it is made, so without this a brand-new kept card would
        claim to be awake for a full window.
        """
        async with self._lock:
            now = int(time.time())
            for app in self._apps.values():
                if app.get("sandbox_id") == sandbox_id:
                    app["asleep_since"] = now
            self._dirty = True
            self.flush(force=True)

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
