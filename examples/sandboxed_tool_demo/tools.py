# ABOUTME: A single sandboxed=True tool + the SandboxConfig that decides its backend. Kept in
# its own plain (non-workflow, non-worker) module: remote-box re-imports this module fresh inside
# the sandbox subprocess for every call, so it stays minimal on purpose (see
# `harness/sandbox/config.py`'s docstring for why SandboxConfig itself needs remote-box, and
# `harness/agent_workflow.py`'s `_validate_sandboxable` for why a sandboxed tool takes exactly
# one pydantic.BaseModel param and returns one).

import subprocess
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel
from remote import Daytona
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.sandbox import SandboxConfig

# The ONE place this demo's sandbox backend is chosen — never the tool itself. Runs under a real
# Daytona cloud sandbox. Swap in remote.E2B(...) or remote.Subprocess() to run the exact same
# tool under a different backend, with zero changes to `run_bash` below.
#
# local_project_root is THIS directory (the example dir), not the repo root. It does NOT determine
# how the tool is imported inside the sandbox: remote-box re-imports `run_bash` by its real
# __module__ (`examples.sandboxed_tool_demo.tools`; see remote/decorator.py's __get_import_path),
# which the Dockerfile satisfies by COPYing tools.py to /app/examples/sandboxed_tool_demo/ with
# /app on sys.path. local_project_root only (a) names/hashes the snapshot — scoping it to this dir
# means edits elsewhere in the repo don't invalidate the prebuilt image — and (b) is passed to
# remote-box, which for Daytona never uploads it (the code is baked into the snapshot by the
# Dockerfile, not shipped at runtime).
#
# dockerfile_path is relative to local_project_root, so it points at THIS dir's Dockerfile. Keeping
# the Dockerfile here (not the repo root) is deliberate: this example's code must not leak into the
# rest of the repo. It works because the image installs the harness from GitHub (see pyproject.toml)
# instead of COPYing the repo's source — so the build context is scoped to this dir and every COPY
# source resolves within it. Daytona's SDK resolves each COPY source relative to the Dockerfile's
# OWN directory and builds its own upload list by parsing the COPY lines client-side (no real
# `docker build`, so `.dockerignore` is never consulted for the upload) — the Dockerfile here lists
# only the bare filenames that live beside it.
#
# Needs DAYTONA_API_KEY set (in .env.local) and the snapshot built ahead of time — never at
# runtime (SandboxConfig.require_prebuilt defaults to True) — via:
#   from temporal_agent_harness.harness.sandbox import build_sandbox
#   build_sandbox(SANDBOX)
SANDBOX = SandboxConfig(
    backend=Daytona(
        snapshot_name="sandboxed-tool-demo",
        dockerfile_path="Dockerfile.sandboxed-tool-demo",
    ),
    local_project_root=Path(__file__).parent,
)


class RunBashInput(BaseModel):
    command: str


class RunBashResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


@agent.activity_tool_defn(
    sandboxed=True,
    # Arbitrary bash commands can run long (installs, builds, ...) — a generous timeout, well
    # past the harness's 30s tool-call default.
    activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=2)),
)
async def run_bash(arg: RunBashInput) -> RunBashResult:
    """Run an arbitrary bash command inside an isolated sandbox and return its stdout, stderr,
    and exit code.

    Runs INSIDE the sandbox (Daytona/E2B/Subprocess — whichever this agent is configured with),
    never on the worker's own machine. Every call requires human approval before it runs (see
    workflow.py's approval_policy_default) — deliberately NOT `inherently_safe`, since a bash
    command can do anything a shell can, and no approval policy should ever auto-approve it.
    """
    proc = subprocess.run(
        ["bash", "-c", arg.command],
        capture_output=True,
        text=True,
        timeout=100,  # comfortably under the activity's own 2-minute start_to_close_timeout
    )
    return RunBashResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)
