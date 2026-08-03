# ABOUTME: A conversational CODING agent whose tools run inside an E2B cloud sandbox (not on the
# user's machine, and not via callback). Same six tools as the callback coding agent, but declared as
# @agent.activity_tool_defn(sandboxed=True) — the work happens in a disposable box, on a project that
# lives there. Pairs with the live-preview proxy: the agent builds a web app in the box and you
# preview it. The model is driven by the OPENAI AGENTS SDK: `Runner.run_streamed` owns the
# tool-calling loop, so there is no hand-rolled loop here (the Gemini-era
# examples.coding_agent_common.chat_loop is gone).

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

# EVERY temporal_agent_harness / remote-box import must live in this ONE block (take "every"
# literally — todo_tools, tools, agent_protocol, the plugin glue). remote-box (pulled in
# transitively by tools.py) needs pass-through treatment, and splitting even one harness import out
# was enough to load two copies of agent_workflow.py — each with its own _CURRENT_RUNNER contextvar —
# so a sandboxed tool call fails with "tool ... has no active runner". The `agents` SDK and its
# `openai` dependency belong here too: both do module-level I/O-ish work the workflow sandbox rejects.
with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import ModelSettings, Runner, TResponseInputItem
    from openai.types.shared import Reasoning

    from temporal_agent_harness.ai_sdks.openai_agents_harness import as_openai_agent_tool
    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )

    from examples.coding_agent_common.todo_tools import todoread, todowrite

    from .tools import (
        AI_GATEWAY_MODELS,
        AI_GATEWAY_URL,
        PREVIEW_BASE_DOMAIN,
        PROJECT_ROOT,
        SANDBOX,
        SANDBOX_ID_ENV_VAR,
        SANDBOXED_CODING_TOOLS,
    )


TASK_QUEUE = "sandboxed-coding-agent"
DEFAULT_MODEL = "gpt-5.1"

# `summary="auto"` is what makes the reasoning stream visible: the harness's OpenAI observer turns
# `response.reasoning_summary_text.delta` events into `thought_summary` turn events, and without a
# requested summary the API emits none (the Gemini-era equivalent was `thinking_summaries: "auto"`).
# Low effort matches the old `thinking_level: "low"` — this agent's work is mostly tool calls, and
# reasoning tokens on every hop is the expensive way to do them.
MODEL_SETTINGS = ModelSettings(reasoning=Reasoning(effort="low", summary="auto"))

# The SDK's `Runner` counts one "turn" per model call and gives up at 10 by default — a ceiling a
# coding agent hits in the middle of a routine task (scaffold, install, build, fix, re-run) and then
# dies with MaxTurnsExceeded. The Gemini chat_loop had no such bound; keep the bound (a runaway loop
# should still end) but set it where only a genuinely stuck agent reaches it.
MAX_MODEL_TURNS = 100


_BASE_INSTRUCTION = f"""\
You are a capable, careful coding assistant. The user talks to you in plain language — asking you \
to build an app, add a feature, run a command, or explain code — and YOU do the work by calling \
tools. Your tools run inside an isolated cloud sandbox (never on the user's own machine), on a \
project that lives in that sandbox at {PROJECT_ROOT}. All file paths are relative to that \
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
# the port it chose; the domain, project dir and sandbox-id env var are all interpolated from config
# and tools.py, never spelled out here — hardcoding them is how the Daytona-era `$DAYTONA_SANDBOX_ID`
# and /home/daytona paths survived the move to E2B and quietly broke every preview URL.
_PREVIEW_INSTRUCTION = f"""

## Making a web app previewable

When the user asks for a website or web app they can open in a browser, this sandbox can serve it \
live at its own subdomain. After building the app in the project directory:
1. Write the single server-launch command to `start.sh` in the project root (i.e. `write` with \
file_path="start.sh") — it MUST run in the FOREGROUND (no `&`) and bind 0.0.0.0 on a plain port you \
pick (e.g. 3000), and `cd` into the project dir first. A supervisor picks this up within ~1s and \
keeps it running.
   Example: `write` file_path="start.sh" content="cd {PROJECT_ROOT}\\npython3 -m http.server 3000 --bind 0.0.0.0\\n"
2. Read the sandbox id and give the user the preview URL. Run `bash` with `echo "${SANDBOX_ID_ENV_VAR}"`, \
then tell them to open https://<that-id>-<port>.{PREVIEW_BASE_DOMAIN}/ (that-id is the sandbox id, \
port is the one you chose). The site is served at the ROOT of that subdomain, so build it like any \
normal website — absolute asset paths (/style.css, /assets/app.js), client-side routing, and calls \
to /api/… all work as-is. No <base href> or relative-path tricks needed.

## Every site is a mobile-first, installable PWA

Assume a phone. Build every site this way unless the user says otherwise:
- Mobile first: `<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">`, \
fluid layout, tap targets >=44px, `env(safe-area-inset-*)` padding, no horizontal scroll.
- `/manifest.webmanifest`, linked from `<head>` with a `theme-color` meta: name, short_name, \
`start_url` and `scope` "/", `display` "standalone", background/theme colors, and 192+512 PNG icons \
(generate them — Pillow is a `pip install` away) including one with `"purpose":"maskable"` \
(full-bleed background, art inside the middle 80%).
- `/sw.js`, registered on load with `registration.update()`: network-first for navigations and data \
so every load shows the latest deploy, cache-first ONLY for immutable/versioned assets. \
`skipWaiting()` + `clients.claim()`, and delete other caches on activate. Never cache redirects.
- An explicit **Install** button, hidden by default: on `beforeinstallprompt` call \
`preventDefault()`, keep the event, show the button, and `prompt()` it on click. Hide it after \
`appinstalled` or when already `(display-mode: standalone)`. iOS fires no such event — there, show \
a one-line "Share -> Add to Home Screen" hint instead."""

# Only offered when AI_GATEWAY_URL is configured. The gateway is reachable from inside the sandbox
# only (it lives on the tailnet the sandbox joins), which is why the instruction is emphatic about
# calling it SERVER-side: a previewed site's client-side fetch runs in the user's browser, which is
# not on the tailnet, so it would fail for everyone but the sandbox itself.
_AI_INSTRUCTION = f"""

## Building AI features

This sandbox can reach a **pre-authenticated Gemini gateway** at {AI_GATEWAY_URL} — no API key \
exists or is needed; access is granted by the sandbox itself. Use it whenever the user asks for AI \
features. Models available, best first: {AI_GATEWAY_MODELS}.

Use the `google-genai` library (`pip install google-genai`) pointed at the gateway. This is the \
whole pattern — it runs as-is, so don't go looking for credentials or a different endpoint:
```python
from google import genai

client = genai.Client(api_key="unused", http_options={{"base_url": "{AI_GATEWAY_URL}"}})
resp = client.models.generate_content(
    model="{AI_GATEWAY_MODELS.split(",")[0].strip()}",
    contents="Summarise this in one sentence: ...",
)
print(resp.text)          # the reply, as a plain string
```
`api_key="unused"` is required: the library refuses to construct a client without one, and the \
gateway ignores it. Never ask the user for a key and never put one in the code — there isn't one.

In an async server: `await client.aio.models.generate_content(...)`, same arguments. For a system \
prompt: `config=types.GenerateContentConfig(system_instruction="...")` with \
`from google.genai import types`.

Call it from your **server-side code only** (the process `start.sh` starts). The gateway is reachable \
from inside this sandbox, NOT from the user's browser, so a client-side `fetch()` to it from a \
previewed page will fail for the user. Add an endpoint on your own server (e.g. `POST /api/ask`) that \
calls the gateway and returns the result to the page."""

_CLOSING_INSTRUCTION = """

Keep going across tool calls until the task is done, then reply in brief, friendly prose: what you \
built or changed, which files, and (if relevant) the preview URL. Never invent file contents or \
command output you didn't actually read."""

SYSTEM_INSTRUCTION = (
    _BASE_INSTRUCTION
    + (_PREVIEW_INSTRUCTION if PREVIEW_BASE_DOMAIN else "")
    + (_AI_INSTRUCTION if AI_GATEWAY_URL else "")
    + _CLOSING_INSTRUCTION
)


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
        self._todos: list = []
        # Conversation state, threaded across turns as the SDK's own input-item list. This replaces
        # the Gemini `previous_interaction_id` handle: OpenAI's Responses conversation state is not
        # server-side here, so the workflow carries the transcript itself — which suits the harness
        # (it is durable workflow state, restored on replay, with no dependency on the provider
        # still holding the prior interaction).
        self._conversation: list[TResponseInputItem] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the sandboxed coding agent. Ask it to build an app, add a feature, or run
        something; it works on a project inside a cloud sandbox and can serve a web app for live
        preview. Mutating tools pause for your approval."""
        sdk_agent = OpenAIAgent(
            name="SandboxedCodingAgent",
            instructions=SYSTEM_INSTRUCTION,
            model=self._model,
            model_settings=MODEL_SETTINGS,
            tools=self._openai_tools(),
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": message.text},
        ]

        # run_streamed returns immediately; draining its events is what drives the turn to
        # completion. `context=self._runner` is the harness seam — worker.py wires the plugin's
        # `stream_to_provider` to read this turn's stream context off the runner, so live model
        # events reach the attached UI.
        result = Runner.run_streamed(
            sdk_agent,
            input=input_items,
            context=self._runner,
            max_turns=MAX_MODEL_TURNS,
        )
        async for _event in result.stream_events():
            pass

        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))

    def _openai_tools(self):
        """Adapt this agent's harness tools onto the SDK, rebuilt per turn.

        `as_openai_agent_tool` keeps every harness guarantee — approval policy, tool_start/end/error
        events, and for the six sandboxed tools the durable in-sandbox activity dispatch. The two
        todo tools declare `sink: agent.Injected[list]`, which is hidden from the model's schema and
        supplied HERE, per call, as this workflow's durable todo state (the same binding the Gemini
        dispatcher did by hand).
        """
        injected = {todowrite: {"sink": self._todos}, todoread: {"sink": self._todos}}
        return [
            as_openai_agent_tool(self._runner, tool, injections=injected.get(tool))
            for tool in (*SANDBOXED_CODING_TOOLS, todowrite, todoread)
        ]
