# ABOUTME: The sandboxed coding agent's six tools + the SandboxConfig picking their backend. Each is
# a @agent.activity_tool_defn(sandboxed=True) tool whose body runs INSIDE a Daytona sandbox, against
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
from remote import Daytona
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
# empty — the agent scaffolds a project from scratch. The live-preview supervisor runs whatever the
# agent writes to start.sh IN THIS DIR (supervise.sh's START_FILE = <PROJECT_ROOT>/start.sh — it must
# be under the project root so the confined `write` tool can create it), so the agent can build a web
# app here and serve it for the preview proxy (see preview/ and supervise.sh).
PROJECT_ROOT = Path("/home/daytona/project")

# The snapshot's IDENTITY: the fields the image is built from. Everything that must agree between
# build time and run time lives here, in one object, so the two can't drift — the offline build
# (`build_sandbox(SANDBOX, backend=SANDBOX_BACKEND)`) and the runtime provider (tailscale.py, which
# `model_copy`s this and adds env_vars) both take it from this single definition. Drift would fail
# activation's `require_prebuilt` check.
SANDBOX_BACKEND = Daytona(
    snapshot_name="sandboxed-coding-agent",
    dockerfile_path="Dockerfile.sandbox-coding-agent",
)

# The ONE place this agent's sandbox backend is chosen (never the tools themselves). Runs on a real
# Daytona cloud sandbox. local_project_root is the `examples/` dir (Path parents up from this file):
# the snapshot is built from BOTH this example's tools.py and the shared coding_agent_common, whose
# lowest common ancestor is examples/ — see examples/Dockerfile.sandbox-coding-agent for why the
# Dockerfile lives there. Needs DAYTONA_API_KEY and a prebuilt snapshot (`just build-sandbox`).
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
    backend="daytona-tailscale",  # == tailscale.PROVIDER_NAME (a literal: see the import note above)
    local_project_root=Path(__file__).parent.parent.parent,  # -> examples/
)

# A generous timeout for the shell tool — installs/builds/tests can run long.
_BASH_ACTIVITY = ActivityConfig(start_to_close_timeout=timedelta(minutes=3))


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


@agent.activity_tool_defn(sandboxed=True, inherently_safe=True)
async def read(arg: ReadInput) -> ReadResult:
    """Read a UTF-8 text file from the project and return its full contents. `file_path` is relative
    to the project root, e.g. "src/main.py". Always read a file before editing it, so your `edit`
    matches the exact current text."""
    return ReadResult(content=tool_impls.read_file(PROJECT_ROOT, arg.file_path))


@agent.activity_tool_defn(sandboxed=True)
async def write(arg: WriteInput) -> WriteResult:
    """Create a new file, or OVERWRITE an existing one, with `content` (UTF-8), creating parent
    directories as needed. Replaces the WHOLE file — use `edit` for a surgical change to a large
    file. `file_path` is relative to the project root. Returns a short confirmation."""
    return WriteResult(message=tool_impls.write_file(PROJECT_ROOT, arg.file_path, arg.content))


@agent.activity_tool_defn(sandboxed=True)
async def edit(arg: EditInput) -> EditResult:
    """Replace an exact substring in a file. `old_string` must occur EXACTLY ONCE (include enough
    surrounding context to make it unique) and is replaced with `new_string`. `read` the file first
    so the match is exact. Returns a confirmation plus a unified diff. `file_path` is relative to the
    project root."""
    message, diff = tool_impls.edit_file(PROJECT_ROOT, arg.file_path, arg.old_string, arg.new_string)
    return EditResult(message=message, diff=diff)


@agent.activity_tool_defn(sandboxed=True, inherently_safe=True)
async def grep(arg: GrepInput) -> GrepResult:
    """Search every text file in the project for a Python regular expression, returning matching
    lines as "path:lineno: line". Use it to locate a symbol, string, or definition before reading or
    editing. Results are capped."""
    matches, count = tool_impls.grep_files(PROJECT_ROOT, arg.pattern)
    return GrepResult(matches=matches, count=count)


@agent.activity_tool_defn(sandboxed=True, inherently_safe=True)
async def glob(arg: GlobInput) -> GlobResult:
    """List project files whose path matches a glob `pattern` (e.g. "**/*.py", "src/**/*.ts"), one
    per line, relative to the project root. Use it to discover files by name/extension before reading
    them. Results are capped."""
    paths, count = tool_impls.glob_files(PROJECT_ROOT, arg.pattern)
    return GlobResult(paths=paths, count=count)


# The sandboxed tools, in a stable order. The workflow adds the inline todowrite/todoread on top.
SANDBOXED_CODING_TOOLS = [bash, read, write, edit, grep, glob]
