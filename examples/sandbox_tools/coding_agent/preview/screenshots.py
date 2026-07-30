"""
Screenshot capture — why the gallery shows images instead of iframes.

An iframe per app would WAKE every sandbox in the gallery the moment the page
loads, and a woken sandbox bills for compute until it auto-stops. A gallery of
twenty apps would cost twenty wakes to look at. So the gallery is static images,
and this module fills them in at the only moments when a sandbox is ALREADY
awake and the capture is therefore free:

  * right after the proxy woke it to serve a real visitor (proxy.py), and
  * the first time we ever serve it, and
  * when someone presses Refresh — which DOES wake a stopped sandbox, so the
    landing page says so on the button.

Capture goes straight to the sandbox's e2b.app URL with its traffic token, not
through our own proxy, so it never trips the auth gate and never re-enters the
handler that spawned it.

Playwright is optional. If it isn't installed, or the browser was never
downloaded, every capture reports failure and the gallery falls back to a
placeholder tile — the proxy itself keeps working. That keeps the example
runnable without a ~150MB browser and lets a deployment adopt screenshots
separately from the rest.
"""

import asyncio
import os

from .config import (
    SCREENSHOTS_ENABLED,
    SHOT_HEIGHT,
    SHOT_QUALITY,
    SHOT_TIMEOUT_MS,
    SHOT_WIDTH,
    SHOTS_DIR,
)
from .registry import registry


def shot_path(key: str) -> str:
    return os.path.join(SHOTS_DIR, f"{key}.jpg")


def _write_shot(key: str, data: bytes) -> None:
    """Write-temp-then-rename, so the gallery never serves a half-written jpeg."""
    os.makedirs(SHOTS_DIR, exist_ok=True)
    tmp = shot_path(key) + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, shot_path(key))


class Screenshotter:
    """One shared headless browser, lazily started, reused across captures."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._start_lock = asyncio.Lock()
        self._inflight: set[str] = set()
        self.last_error = ""

    async def _browser_or_none(self):
        if self._browser is not None:
            return self._browser
        async with self._start_lock:
            if self._browser is not None:      # another caller won the race
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                self.last_error = "playwright is not installed"
                return None
            try:
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            except Exception as exc:
                # Most commonly "browser not downloaded" — see the Dockerfile.
                self.last_error = f"could not launch chromium: {exc}"
                self._pw = None
                self._browser = None
                return None
        return self._browser

    async def capture(self, key: str, url: str, token: str) -> bool:
        """Screenshot one app. Returns True on success. Never raises."""
        if not SCREENSHOTS_ENABLED:
            return False
        if key in self._inflight:
            # A wake can produce a burst of concurrent requests, each wanting a
            # capture. One is enough.
            return False
        self._inflight.add(key)
        try:
            browser = await self._browser_or_none()
            if browser is None:
                return False
            from .proxy import preview_headers  # local: proxy imports this module
            context = await browser.new_context(
                viewport={"width": SHOT_WIDTH, "height": SHOT_HEIGHT},
                # The sandbox's traffic token (empty when the sandbox allows public
                # traffic); see proxy.preview_headers.
                extra_http_headers=preview_headers(token),
            )
            try:
                page = await context.new_page()
                await page.goto(url, wait_until="load", timeout=SHOT_TIMEOUT_MS)
                # Give client-rendered apps a beat to paint past the empty root
                # div — without this an SPA screenshots as a blank page.
                try:
                    await page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
                title = (await page.title() or "").strip()
                png = await page.screenshot(type="jpeg", quality=SHOT_QUALITY)
            finally:
                await context.close()

            # to_thread: the write is small but this runs on the same event loop
            # that is serving proxied traffic, and disk stalls are not rare.
            await asyncio.to_thread(_write_shot, key, png)
            await registry.record_shot(key, title)
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        finally:
            self._inflight.discard(key)

    async def close(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass


screenshotter = Screenshotter()

# Strong refs to in-flight background captures. asyncio only holds a WEAK
# reference to a running task, so without this the garbage collector is free to
# cancel a capture mid-flight.
_tasks: set[asyncio.Task] = set()


def schedule_capture(key: str, url: str, token: str) -> None:
    """Fire-and-forget a capture. Returns immediately; never blocks a response."""
    if not SCREENSHOTS_ENABLED:
        return
    task = asyncio.create_task(screenshotter.capture(key, url, token))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain(timeout: float = 10.0) -> None:
    """Let in-flight captures finish on shutdown, briefly."""
    if _tasks:
        await asyncio.wait(set(_tasks), timeout=timeout)
