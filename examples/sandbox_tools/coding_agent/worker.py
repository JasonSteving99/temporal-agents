"""Worker for the sandboxed coding agent.

Run from the repo root with:
    uv run --extra sandbox --group examples python -m examples.sandbox_tools.coding_agent.worker

Hosts SandboxedCodingAgentWorkflow plus its sandbox lifecycle activities (and the Tailscale backend
provider they resolve this agent's `SandboxConfig.backend` name against) and
its six sandboxed tools' activities (bash/read/write/edit/grep/glob). The OpenAI Agents plugin is
registered because the agent drives the OpenAI Agents SDK; the plugin auto-registers its model
activities (including the streaming one). Run `just build-sandbox` once before starting this worker —
runtime never builds the sandbox image implicitly (SandboxConfig.require_prebuilt).

The plugin is wired for the HARNESS STREAMING PATH:
  * `model_params.stream_to_provider=stream_to_provider` — resolves each streamed model call's
    per-turn stream context off the runner the workflow passes as `Runner.run_streamed(context=...)`,
    and
  * `observer_factory=harness_observer_factory` — turns that context into the observer that
    translates raw OpenAI events into the harness turn-stream vocabulary live.
Drop either one and the UI stops seeing token-by-token replies, thought summaries and tool requests.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required — the agent calls the OpenAI API
    E2B_API_KEY                               required — the tools run on an E2B sandbox
    SANDBOXED_CODING_AGENT_TASK_QUEUE         task queue to poll (default: sandboxed-coding-agent)
    TAILSCALE_OAUTH_CLIENT_SECRET             optional — lets each sandbox mint its own tailnet key;
                                              unset means sandboxes join no tailnet (see tailscale.py)
    TAILSCALE_TAG                             the ACL tag those sandboxes advertise
                                              (default: tag:agent-artifact)
"""

import asyncio
import logging
import os
import sys
from datetime import timedelta

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    stream_to_provider,
)
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.sandbox.activities import sandbox_activities

from .tailscale import PROVIDER_NAME, e2b_with_tailscale
from .tools import SANDBOXED_CODING_TOOLS
from .workflow import TASK_QUEUE, SandboxedCodingAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("SANDBOXED_CODING_AGENT_TASK_QUEUE", TASK_QUEUE)

    # The SDK reads OPENAI_API_KEY itself, inside the model activity; check it here so a missing key
    # fails at startup rather than mid-turn.
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            # A coding agent's model calls can be long (big transcripts, long tool schemas), so this
            # matches the 3 minutes the Gemini interactions activity was given. Streaming leans on
            # activity heartbeats to notice a stuck call, so keep the heartbeat well under it.
            start_to_close_timeout=timedelta(minutes=3),
            heartbeat_timeout=timedelta(seconds=30),
            # The harness streaming seam: route streamed events to the in-flight turn.
            stream_to_provider=stream_to_provider,
        ),
        observer_factory=harness_observer_factory,
    )

    # No `data_converter=` here: the plugin installs its own, which is OpenAI-aware AND
    # pydantic-compatible — it supersedes the `pydantic_data_converter` the Gemini worker passed
    # (the sandboxed tools' BaseModel inputs/results still round-trip).
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[plugin])

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[SandboxedCodingAgentWorkflow],
        # sandbox_activities(...): sandbox activate/pause/terminate, plus the backend PROVIDER this
        # agent names as its SandboxConfig.backend — the callable that mints the sandbox's Tailscale
        # auth key. The factory form is used instead of the ready-made SANDBOX_ACTIVITIES precisely
        # because there's a provider to inject; register one or the other, never both (the three
        # activity names can be claimed only once per worker).
        # One tool_activity per sandboxed tool: each tool's durable body. The OpenAI model
        # activities (incl. invoke_model_activity_streaming) are registered by the plugin.
        activities=[
            *sandbox_activities({PROVIDER_NAME: e2b_with_tailscale}),
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
