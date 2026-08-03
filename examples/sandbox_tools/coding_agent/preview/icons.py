"""
Renders the PWA's icon set from the mark in theme.py.

    python -m examples.sandbox_tools.coding_agent.preview.icons

These are checked-in PNGs, not generated at request time — an installed app asks
for its icon outside any browser session, sometimes while the server is down, so
they have to be plain files on disk. This script is how they stay HONEST: the
artwork lives in `theme.mark_svg` and nowhere else, so changing the header mark
and re-running this is the whole update. The set it replaced was left over from
an earlier identity and looked nothing like the app it installed.

Chromium does the rasterising, via the Playwright the proxy already depends on
for screenshots (`screenshots.py`) — so there is no new dependency and no
Pillow/cairo/rsvg toolchain to keep working.

What gets written, and why each one differs, is in `theme.icon_svg`.
"""

import asyncio
import os

from .theme import icon_svg

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# (filename, size, shape). 180px for apple-touch-icon is the size iOS asks for.
TARGETS = [
    ("icon-192.png", 192, "rounded"),
    ("icon-512.png", 512, "rounded"),
    ("icon-maskable-512.png", 512, "maskable"),
    ("apple-touch-icon.png", 180, "square"),
]


async def render() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            for name, size, shape in TARGETS:
                # device_scale_factor=1 with a viewport of exactly `size`: the SVG
                # is vector, so the page IS the icon and no resampling happens.
                page = await (
                    await browser.new_context(
                        viewport={"width": size, "height": size}, device_scale_factor=1
                    )
                ).new_page()
                await page.set_content(
                    "<style>html,body{margin:0;background:transparent}"
                    f"svg{{display:block;width:{size}px;height:{size}px}}</style>"
                    + icon_svg(shape)
                )
                # omit_background keeps the rounded corners transparent rather
                # than white — the one thing that makes an icon look pasted on.
                await page.screenshot(
                    path=os.path.join(STATIC_DIR, name), omit_background=True
                )
                print(f"wrote {name} ({size}px, {shape})")
        finally:
            await browser.close()


def write_favicon() -> None:
    """The favicon is served as SVG, so it needs no rasterising at all."""
    with open(os.path.join(STATIC_DIR, "favicon.svg"), "w", encoding="utf-8") as fh:
        fh.write(icon_svg("rounded"))
    print("wrote favicon.svg")


if __name__ == "__main__":
    asyncio.run(render())
    write_favicon()
