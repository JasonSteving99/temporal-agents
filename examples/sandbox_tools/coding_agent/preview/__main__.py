"""Entrypoint: `python -m examples.sandbox_tools.coding_agent.preview`."""

import logging
import os

from aiohttp import web

from .app import build_app
from .config import PROXY_PORT

if __name__ == "__main__":
    # Without this, nothing the proxy logs is emitted at all, and a failed preview looks identical to
    # a DNS/TLS/auth problem from the outside. PREVIEW_LOG_LEVEL=DEBUG for more.
    logging.basicConfig(
        level=os.environ.get("PREVIEW_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    web.run_app(build_app(), port=PROXY_PORT)
