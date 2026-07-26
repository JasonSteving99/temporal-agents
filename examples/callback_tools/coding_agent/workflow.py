"""A conversational CODING agent built on harness CALLBACK TOOLS.

The user chats in plain text ("add a test for X", "why does this crash?", "refactor this module")
and a *model in the loop* works on their project by calling shell + filesystem tools. Those tools
are **callback tools**: the agent has no disk of its own (picture it running in a cloud worker), so
each tool call pauses in-workflow and a client on the user's machine executes it against the local
project directory and returns the result. Here that client is the **OpenCode shim** — the same
process that fronts the stock OpenCode TUI.

Every tool that touches the user's machine is GATED on their approval (``allow_inherently_safe``):
the mutating tools (``bash``/``write``/``edit``) become an OpenCode permission prompt; the read-only
tools (``read``/``grep``/``glob``) and the plan tools (``todowrite``/``todoread``) auto-approve.

The conversational loop itself — streaming the Gemini Interactions API, dispatching tool calls,
feeding results back — is the SHARED ``examples.coding_agent_common.chat_loop``, used unchanged by
the sandboxed coding agent too; this module only supplies the toolset, dispatch, and policy.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.ai_sdks.google_genai_plugin import (
        function_param,
        google_genai_client,
    )
    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )

    from examples.coding_agent_common.chat_loop import dispatch_via_runner, run_chat_turn

    from .tools import CODING_TOOLS


TASK_QUEUE = "coding-agent"
DEFAULT_MODEL = "gemini-3.6-flash"

# Ask the model to think and STREAM its thought summaries — the shim renders these as OpenCode's
# collapsible "thinking" block. `thinking_summaries` is a string enum ("auto" | "none"), not a bool.
GENERATION_CONFIG = {"thinking_level": "low", "thinking_summaries": "auto"}


SYSTEM_INSTRUCTION = """\
You are a capable, careful coding assistant working inside the user's project. The user talks to \
you in plain language — asking you to explain code, fix a bug, add a feature, write tests, or run \
a command — and YOU do the work by calling tools.

You do not have a filesystem or shell of your own. You act on the project only through these \
tools, which run on the user's machine: `bash`, `read`, `write`, `edit`, `grep`, `glob`. You also \
have `todowrite`/`todoread` to keep a task list. Their descriptions give the exact signatures — \
follow them. All paths are relative to the project root.

How to work:
- PLAN multi-step work with `todowrite`: lay out the tasks up front, mark one `in_progress`, and \
mark it `completed` as you finish — so the user can follow along. `todoread` recalls the current \
list (it persists across messages). Skip planning for trivial one-step requests.
- ORIENT before you change anything. Use `glob`/`grep`/`read` (or `bash` with `ls`/`cat`) to \
understand the code and conventions before editing, so your changes fit in.
- READ before you EDIT. `edit` needs an exact, unique `old_string`; read the file first. Use \
`edit` for surgical changes and `write` only for new files or full rewrites.
- Every tool call requires the user's approval before it runs — so keep calls purposeful, and \
when a `bash` command is destructive or slow, say so in your reply so the user can decide.
- VERIFY your work when it's cheap to: run the relevant test or build via `bash` after a change.
- Keep going across multiple tool calls until the task is done, then reply in brief, friendly \
prose: what you changed, which files, and how you checked it. Never invent file contents or \
command output you didn't actually read."""


@workflow.defn(name="CodingAgent")
@agent.defn
class CodingAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Every tool that touches the user's machine is gated — the shim turns each gated call
            # into an OpenCode permission prompt. Only `inherently_safe` tools auto-approve.
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._model: str = DEFAULT_MODEL
        self._previous_interaction_id: str | None = None
        self._tools = list(CODING_TOOLS)
        self._tools_by_name = {tool.__name__: tool for tool in self._tools}
        # Durable workflow state: the agent's task list, replaced in place by the inline `todowrite`
        # tool (injected as its `sink`). Survives across turns like any workflow field.
        self._todos: list = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=3)),
            runner=self._runner,
        )
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the coding agent. Ask it to explain, fix, refactor, test, or run something; it
        reads and edits your local project through callback tools (each gated on your approval) and
        replies with what it did."""
        reply_text, self._previous_interaction_id = await run_chat_turn(
            self._gemini,
            model=self._model,
            system_instruction=SYSTEM_INSTRUCTION,
            user_text=message.text,
            tools=[function_param(tool) for tool in self._tools],
            previous_interaction_id=self._previous_interaction_id,
            dispatch_tool=self._dispatch_tool,
            generation_config=GENERATION_CONFIG,
        )
        return TextReply(text=reply_text)

    def _dispatch_tool(self, call):
        """Bind this workflow's runner/toolset/todo state into the shared dispatcher."""
        return dispatch_via_runner(
            call,
            runner=self._runner,
            tools_by_name=self._tools_by_name,
            todo_sink=self._todos,
        )
