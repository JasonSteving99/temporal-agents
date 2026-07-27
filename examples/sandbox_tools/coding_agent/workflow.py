# ABOUTME: A conversational CODING agent whose tools run inside a Daytona cloud sandbox (not on the
# user's machine, and not via callback). Same six tools as the callback coding agent, but declared as
# @agent.activity_tool_defn(sandboxed=True) — the work happens in a disposable box, on a project that
# lives there. Pairs with the live-preview proxy: the agent builds a web app in the box and you
# preview it. The conversational loop is the SHARED examples.coding_agent_common.chat_loop.

from datetime import timedelta

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

# EVERY temporal_agent_harness / remote-box import must live in this ONE block (take "every"
# literally — chat_loop, todo_tools, tools, agent_protocol, the plugin glue). remote-box (pulled in
# transitively by tools.py) needs pass-through treatment, and splitting even one harness import out
# was enough to load two copies of agent_workflow.py — each with its own _CURRENT_RUNNER contextvar —
# so a sandboxed tool call fails with "tool ... has no active runner".
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
    from examples.coding_agent_common.todo_tools import todoread, todowrite

    from .tools import PREVIEW_BASE_DOMAIN, SANDBOX, SANDBOXED_CODING_TOOLS


TASK_QUEUE = "sandboxed-coding-agent"
DEFAULT_MODEL = "gemini-3.6-flash"

GENERATION_CONFIG = {"thinking_level": "low", "thinking_summaries": "auto"}


_BASE_INSTRUCTION = """\
You are a capable, careful coding assistant. The user talks to you in plain language — asking you \
to build an app, add a feature, run a command, or explain code — and YOU do the work by calling \
tools. Your tools run inside an isolated cloud sandbox (never on the user's own machine), on a \
project that lives in that sandbox at /home/daytona/project. All file paths are relative to that \
project root, which starts EMPTY — you scaffold whatever the task needs.

Your tools: `bash`, `read`, `write`, `edit`, `grep`, `glob`, plus `todowrite`/`todoread` for a task \
list. Their descriptions give the exact signatures — follow them.

How to work:
- PLAN multi-step work with `todowrite`; mark one task `in_progress` and `completed` as you go. \
Skip planning for trivial one-step requests.
- ORIENT with `glob`/`grep`/`read` before editing existing files, so your changes fit in.
- READ before you EDIT. `edit` needs an exact, unique `old_string`. Use `write` for new files or \
full rewrites, `edit` for surgical changes.
- VERIFY when it's cheap: run the build or tests via `bash` after a change."""

# Only offered when a preview domain is configured (PREVIEW_BASE_DOMAIN). Routing is by SUBDOMAIN
# (see preview/proxy.py), so the site is served at the ROOT of its own subdomain and behaves like a
# normal website — no <base href> / relative-path constraints. The agent fills in the sandbox id and
# the port it chose; the domain is baked in from config.
_PREVIEW_INSTRUCTION = f"""

## Making a web app previewable

When the user asks for a website or web app they can open in a browser, this sandbox can serve it \
live at its own subdomain. After building the app in the project directory:
1. Write the single server-launch command to `start.sh` in the project root (i.e. `write` with \
file_path="start.sh") — it MUST run in the FOREGROUND (no `&`) and bind 0.0.0.0 on a plain port you \
pick (e.g. 3000), and `cd` into the project dir first. A supervisor picks this up within ~1s and \
keeps it running.
   Example: `write` file_path="start.sh" content="cd /home/daytona/project\\npython3 -m http.server 3000 --bind 0.0.0.0\\n"
2. Read the sandbox id and give the user the preview URL. Run `bash` with `echo "$DAYTONA_SANDBOX_ID"`, \
then tell them to open https://<that-id>-<port>.{PREVIEW_BASE_DOMAIN}/ (that-id is the sandbox id, \
port is the one you chose). The site is served at the ROOT of that subdomain, so build it like any \
normal website — absolute asset paths (/style.css, /assets/app.js), client-side routing, and calls \
to /api/… all work as-is. No <base href> or relative-path tricks needed."""

_CLOSING_INSTRUCTION = """

Keep going across tool calls until the task is done, then reply in brief, friendly prose: what you \
built or changed, which files, and (if relevant) the preview URL. Never invent file contents or \
command output you didn't actually read."""

SYSTEM_INSTRUCTION = _BASE_INSTRUCTION + (
    _PREVIEW_INSTRUCTION if PREVIEW_BASE_DOMAIN else ""
) + _CLOSING_INSTRUCTION


@workflow.defn(name="SandboxedCodingAgent")
@agent.defn
class SandboxedCodingAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # The tools run in a disposable sandbox, so the blast radius is contained — but we still
            # gate the mutating tools (bash/write/edit) and auto-approve the read-only ones
            # (read/grep/glob) + the plan tools, mirroring the callback agent's UX in the Svelte UI.
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
            sandbox=SANDBOX,
        )
        self._model: str = DEFAULT_MODEL
        self._previous_interaction_id: str | None = None
        self._tools = [*SANDBOXED_CODING_TOOLS, todowrite, todoread]
        self._tools_by_name = {tool.__name__: tool for tool in self._tools}
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
        """Chat with the sandboxed coding agent. Ask it to build an app, add a feature, or run
        something; it works on a project inside a cloud sandbox and can serve a web app for live
        preview. Mutating tools pause for your approval."""
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
