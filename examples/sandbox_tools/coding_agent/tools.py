# ABOUTME: The sandboxed coding agent's six tools + the SandboxConfig picking their backend. Each is
# a @agent.activity_tool_defn(sandboxed=True) tool whose body runs INSIDE an E2B sandbox, against
# a project living in the box (PROJECT_ROOT). The actual work is the SHARED
# examples.coding_agent_common.tool_impls — the same code the callback coding agent runs on the
# user's laptop; here it just runs in the cloud sandbox instead. Kept a plain (non-workflow) module:
# remote-box re-imports it fresh inside the sandbox for every call.
#
# Sandboxed tools take exactly one pydantic.BaseModel in and return one out (remote-box's contract,
# enforced by the harness's _validate_sandboxable) — so unlike the callback agent's flat str tools,
# each tool here has an Input/Result model. That's fine: the two agents deliberately declare their
# tools differently and share only the impls.

import os
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel
from remote import E2B
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.sandbox import SandboxConfig

from examples.coding_agent_common import tool_impls

# Base domain the live-preview proxy serves sandbox subdomains under (e.g. "preview.example.com" —
# a running site is reached at https://<sandboxId>-<port>.<this>/). Read from the worker's env HERE,
# in this plain (non-workflow) module: workflow.py imports it via `imports_passed_through()`, so the
# value is resolved once in the real worker process and never read from inside the Temporal workflow
# sandbox (where env access is non-deterministic). Empty if previews aren't configured — the agent's
# system instruction then omits the preview steps. See preview/proxy.py for the routing.
PREVIEW_BASE_DOMAIN = os.environ.get("PREVIEW_BASE_DOMAIN", "").strip().lower()

# The project the agent works on lives HERE, inside the sandbox (created by the Dockerfile). Starts
# empty — the agent scaffolds a project from scratch. Under /home/user because that is E2B's sandbox
# user's home (the Daytona backend used /home/daytona). The live-preview supervisor (supervise.sh, run
# by boot.sh) launches whatever the agent writes to <PROJECT_ROOT>/start.sh — it must be under the
# project root so the confined `write` tool can create it — so the agent can serve a web app here for
# the preview proxy (see preview/).
PROJECT_ROOT = Path("/home/user/project")

# The env var the sandbox exposes its own id under. The agent reads it to build the preview URL
# (workflow.py interpolates this name into the system instruction rather than hardcoding one, which is
# how a stale Daytona-era `$DAYTONA_SANDBOX_ID` survived the move to E2B and silently produced
# unroutable preview URLs — the id came back empty).
SANDBOX_ID_ENV_VAR = "E2B_SANDBOX_ID"

# A pre-authenticated AI gateway reachable from inside the sandbox — in this deployment a Tailscale
# Aperture node on the tailnet the sandbox joins (see tailscale.py), so calls are authorized by the
# sandbox's tailnet identity and carry NO credential. Empty disables the whole feature: the agent's
# system instruction then never mentions it, which is right for anyone without such a gateway.
# Read here rather than hardcoded in workflow.py's prompt, so the URL and models can't drift out of
# sync with the deployment the way the Daytona-era paths did.
AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "").strip().rstrip("/")

# Models the gateway serves, best-first. The agent picks per task: the first for real work, the last
# when it wants cheap and fast.
AI_GATEWAY_MODELS = os.environ.get(
    "AI_GATEWAY_MODELS", "gemini-3.6-flash, gemini-3.5-flash-lite"
).strip()

# The template's IDENTITY: the fields the image is built from. Everything that must agree between
# build time and run time lives here, in one object, so the two can't drift — the offline build
# (`build_sandbox(SANDBOX, backend=SANDBOX_BACKEND)`) and the runtime provider (tailscale.py, which
# `model_copy`s this and adds env_vars) both take it from this single definition. Drift would fail
# activation's `require_prebuilt` check.
#
# E2B, not Daytona: Daytona restricts sandbox egress to a fixed "essential services" allowlist on
# tiers 1-2 and refuses to let it be overridden at either the org or the sandbox level, so
# `tailscaled` can never reach controlplane.tailscale.com and the sandbox can never join the tailnet.
# E2B has no such filter (and has /dev/net/tun, so the tailnet gets a real interface). The Daytona
# config + its Dockerfile are kept in the tree for reference; see Dockerfile.sandbox-coding-agent-e2b.
#
# post_create_cmd runs boot.sh: join the tailnet, then supervise the agent's start.sh (what makes a
# site previewable). It's fired once after creation, as root, backgrounded, and inherits `env_vars` —
# so it sees the TAILSCALE_AUTHKEY the provider minted.
# A template START command could not: E2B runs that while the TEMPLATE builds and snapshots the
# running process into every sandbox, so it starts before any sandbox env exists (and remote-box
# translates a Dockerfile CMD/ENTRYPOINT into exactly that, then overrides it — hence no CMD in the
# Dockerfile). The script is baked into the image so the template hash covers it.
SANDBOX_BACKEND = E2B(
    template_prefix="sandboxed-coding-agent",
    dockerfile_path="Dockerfile.sandbox-coding-agent-e2b",
    post_create_cmd="/usr/local/bin/boot.sh",
    # Previews must not be shareable past the proxy's sign-in. With this False, E2B gates the
    # sandbox's own https://<port>-<id>.e2b.app URL behind a traffic token that ONLY the preview
    # proxy holds (it recovers it via connect(); see preview/proxy.py) — anyone else gets 403. Left
    # True (E2B's default) that URL is world-readable to anyone who knows the sandbox id, which the
    # gallery shows to every signed-in viewer, so the Firebase gate would be trivially bypassable.
    # Settable only at creation: E2B's update_network cannot change it afterwards.
    allow_public_traffic=False,
    # Installs/builds/tests need more than the 1 CPU / 1 GiB default.
    cpu_count=2,
    memory_mb=2048,
    # Refreshed before every call, so this bounds only how long a session may sit IDLE before its
    # sandbox is reclaimed — not the session's total length. Generous because a user thinking between
    # turns must not cost them the box.
    sandbox_ttl_seconds=1800,
)

# The ONE place this agent's sandbox backend is chosen (never the tools themselves). Runs on a real
# E2B cloud sandbox. local_project_root is the `examples/` dir (Path parents up from this file): the
# template is built from BOTH this example's tools.py and the shared coding_agent_common, whose lowest
# common ancestor is examples/ — see examples/Dockerfile.sandbox-coding-agent-e2b for why the
# Dockerfile lives there. Needs E2B_API_KEY and a prebuilt template (`just build-sandbox`).
#
# `backend` is a provider NAME, not the config above: each sandbox needs its own freshly minted
# Tailscale auth key in its environment, which takes an HTTP call and so cannot be stated as a
# literal here. The worker registers the async callable under this name
# (`sandbox_activities({...})` in worker.py) and it runs once per run inside `sandbox_activate`.
# tailscale.py holds the callable; this module deliberately does not import it, since remote-box
# re-imports THIS module inside the sandbox on every tool call.
#
# Offline builds can't run a provider (there's no worker), so they pass the config explicitly:
# `build_sandbox(SANDBOX, backend=SANDBOX_BACKEND)`.
SANDBOX = SandboxConfig(
    backend="e2b-tailscale",  # == tailscale.PROVIDER_NAME (a literal: see the import note above)
    local_project_root=Path(__file__).parent.parent.parent,  # -> examples/
)

# Every tool activity gives up after 3 attempts instead of retrying forever (the Temporal default).
#
# A sandboxed tool call is only as available as the sandbox behind it, and the sandbox's failure modes
# are mostly NOT transient: it was killed by its TTL, paused out from under the run, or the E2B API is
# refusing us. Retrying those forever turns a broken box into a turn that never ends and never says
# why — the agent just stops, mid-thought, with the UI waiting. Three attempts still absorb the honest
# blips (a dropped connection, a slow envd), and anything past that surfaces as a tool error the model
# sees and can tell the user about.
_RETRIES = RetryPolicy(maximum_attempts=3)

# A generous timeout for the shell tool — installs/builds/tests can run long. Must stay comfortably
# BELOW the preview proxy's idle-pause window (PREVIEW_AUTO_STOP_MINUTES), or a single long command
# can outlast it: the proxy's idle timer is driven by the sandbox's TTL refresh, which happens once per
# tool call, so a command that runs longer than the whole window looks exactly like an idle sandbox.
_BASH_ACTIVITY = ActivityConfig(
    start_to_close_timeout=timedelta(minutes=3),
    retry_policy=_RETRIES,
)

# The other five tools are pure filesystem work inside the box — fast, so the harness's own 30s default
# is right; it's restated here only because supplying an ActivityConfig replaces that default wholesale
# (a config with a retry policy and no timeout is rejected by Temporal).
_FAST_ACTIVITY = ActivityConfig(
    start_to_close_timeout=timedelta(seconds=30),
    retry_policy=_RETRIES,
)


class BashInput(BaseModel):
    command: str


class BashResult(BaseModel):
    output: str
    exit_code: int


class ReadInput(BaseModel):
    file_path: str


class ReadResult(BaseModel):
    content: str


class WriteInput(BaseModel):
    file_path: str
    content: str


class WriteResult(BaseModel):
    message: str


class EditInput(BaseModel):
    file_path: str
    old_string: str
    new_string: str


class EditResult(BaseModel):
    message: str
    diff: str


class GrepInput(BaseModel):
    pattern: str


class GrepResult(BaseModel):
    matches: str
    count: int


class GlobInput(BaseModel):
    pattern: str


class GlobResult(BaseModel):
    paths: str
    count: int


@agent.activity_tool_defn(sandboxed=True, activity_config=_BASH_ACTIVITY)
async def bash(arg: BashInput) -> BashResult:
    """Run a shell command in the project directory and return its combined stdout+stderr and exit
    code. Use it to scaffold the project, install deps, build, run tests, use `git`, or start the
    server. Runs inside the sandbox, in the project root. To make a site previewable, write the
    launch command to `start.sh` in the project root (foreground, bound to 0.0.0.0) — a supervisor
    runs it.
    Prefer `write`/`edit` for file changes so the result shows a clean diff. Gated on approval."""
    output, exit_code = await tool_impls.bash_exec(PROJECT_ROOT, arg.command)
    return BashResult(output=output, exit_code=exit_code)


@agent.activity_tool_defn(sandboxed=True, inherently_safe=True, activity_config=_FAST_ACTIVITY)
async def read(arg: ReadInput) -> ReadResult:
    """Read a UTF-8 text file from the project and return its full contents. `file_path` is relative
    to the project root, e.g. "src/main.py". Always read a file before editing it, so your `edit`
    matches the exact current text."""
    return ReadResult(content=tool_impls.read_file(PROJECT_ROOT, arg.file_path))


@agent.activity_tool_defn(sandboxed=True, activity_config=_FAST_ACTIVITY)
async def write(arg: WriteInput) -> WriteResult:
    """Create a new file, or OVERWRITE an existing one, with `content` (UTF-8), creating parent
    directories as needed. Replaces the WHOLE file — use `edit` for a surgical change to a large
    file. `file_path` is relative to the project root. Returns a short confirmation."""
    return WriteResult(message=tool_impls.write_file(PROJECT_ROOT, arg.file_path, arg.content))


@agent.activity_tool_defn(sandboxed=True, activity_config=_FAST_ACTIVITY)
async def edit(arg: EditInput) -> EditResult:
    """Replace an exact substring in a file. `old_string` must occur EXACTLY ONCE (include enough
    surrounding context to make it unique) and is replaced with `new_string`. `read` the file first
    so the match is exact. Returns a confirmation plus a unified diff. `file_path` is relative to the
    project root."""
    message, diff = tool_impls.edit_file(PROJECT_ROOT, arg.file_path, arg.old_string, arg.new_string)
    return EditResult(message=message, diff=diff)


@agent.activity_tool_defn(sandboxed=True, inherently_safe=True, activity_config=_FAST_ACTIVITY)
async def grep(arg: GrepInput) -> GrepResult:
    """Search every text file in the project for a Python regular expression, returning matching
    lines as "path:lineno: line". Use it to locate a symbol, string, or definition before reading or
    editing. Results are capped."""
    matches, count = tool_impls.grep_files(PROJECT_ROOT, arg.pattern)
    return GrepResult(matches=matches, count=count)


@agent.activity_tool_defn(sandboxed=True, inherently_safe=True, activity_config=_FAST_ACTIVITY)
async def glob(arg: GlobInput) -> GlobResult:
    """List project files whose path matches a glob `pattern` (e.g. "**/*.py", "src/**/*.ts"), one
    per line, relative to the project root. Use it to discover files by name/extension before reading
    them. Results are capped."""
    paths, count = tool_impls.glob_files(PROJECT_ROOT, arg.pattern)
    return GlobResult(paths=paths, count=count)


# The sandboxed tools, in a stable order. The workflow adds the inline todowrite/todoread on top.
SANDBOXED_CODING_TOOLS = [bash, read, write, edit, grep, glob]
