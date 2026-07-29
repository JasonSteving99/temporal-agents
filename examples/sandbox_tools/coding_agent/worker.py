"""Worker for the sandboxed coding agent.

Run from the repo root with:
    uv run --extra sandbox --group examples python -m examples.sandbox_tools.coding_agent.worker

Hosts SandboxedCodingAgentWorkflow plus its sandbox lifecycle activities (and the Tailscale backend
provider they resolve this agent's `SandboxConfig.backend` name against) and
its six sandboxed tools' activities (bash/read/write/edit/grep/glob). The Gemini plugin is
registered because the agent drives the Gemini Interactions API; the plugin auto-registers its
interactions activity. Run `just build-sandbox` once before starting this worker — runtime never
builds the sandbox image implicitly (SandboxConfig.require_prebuilt).

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    GEMINI_API_KEY                            required — the agent calls the Gemini API
    DAYTONA_API_KEY                           required — the tools run on Daytona
    SANDBOXED_CODING_AGENT_TASK_QUEUE         task queue to poll (default: sandboxed-coding-agent)
    TAILSCALE_API_KEY                         optional — mints each sandbox's tailnet auth key;
                                              unset means sandboxes join no tailnet (see tailscale.py)
"""

import asyncio
import logging
import os
import sys

from google.genai import Client as GeminiClient
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.sandbox.activities import sandbox_activities

from .tailscale import PROVIDER_NAME, daytona_with_tailscale
from .tools import SANDBOXED_CODING_TOOLS
from .workflow import TASK_QUEUE, SandboxedCodingAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("SANDBOXED_CODING_AGENT_TASK_QUEUE", TASK_QUEUE)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("error: GEMINI_API_KEY env var not set")
    plugin = GoogleGenAIPlugin(GeminiClient(api_key=api_key))

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[plugin],
        data_converter=pydantic_data_converter,
    )

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[SandboxedCodingAgentWorkflow],
        # sandbox_activities(...): sandbox activate/pause/terminate, plus the backend PROVIDER this
        # agent names as its SandboxConfig.backend — the callable that mints the sandbox's Tailscale
        # auth key. The factory form is used instead of the ready-made SANDBOX_ACTIVITIES precisely
        # because there's a provider to inject; register one or the other, never both (the three
        # activity names can be claimed only once per worker).
        # One tool_activity per sandboxed tool: each tool's durable body. The Gemini interactions
        # activity is registered by the plugin.
        activities=[
            *sandbox_activities({PROVIDER_NAME: daytona_with_tailscale}),
            *(agent.tool_activity(tool) for tool in SANDBOXED_CODING_TOOLS),
        ],
    )
    print(
        f"sandboxed-coding-agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
