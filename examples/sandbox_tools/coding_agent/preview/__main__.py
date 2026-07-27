"""Entrypoint: `python -m examples.sandbox_tools.coding_agent.preview`."""

from aiohttp import web

from .app import build_app
from .config import PROXY_PORT

if __name__ == "__main__":
    web.run_app(build_app(), port=PROXY_PORT)
