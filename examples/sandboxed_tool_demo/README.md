# Sandboxed tool demo

A real, Gemini-backed conversational agent (`workflow.py`) with exactly one tool: `run_bash`
(`tools.py`) — an arbitrary bash command that runs inside an isolated **Daytona** cloud sandbox,
never on the worker's own machine. `run_bash` is a
`@agent.activity_tool_defn(sandboxed=True)` tool, and **every single call requires your explicit
approval before it runs** (`ToolApprovalPolicy.always_require_approvals()` — never
auto-approved, by design, since a bash command can do anything a shell can).

A real interactive worker + UI wiring (`worker.py`, `agents.toml`, `justfile`) is provided.

## Requirements

- `GEMINI_API_KEY` — the agent calls the Gemini Interactions API to converse and decide when to
  call `run_bash`.
- `DAYTONA_API_KEY` — `tools.py`'s `SANDBOX` runs `run_bash` on a real Daytona cloud sandbox.
  Also used by the live-preview proxy (below).
- `DAYTONA_TARGET` *(optional)* — Daytona region for the preview proxy (e.g. `us`); omit to use
  your org's default region.

All go in the repo-root `.env.local` (see `.env.example`).

## Backend: Daytona

The sandbox image is built from `Dockerfile.sandboxed-tool-demo` **in this directory**, with a
build context scoped to this directory — nothing from the rest of the harness repo leaks into the
snapshot. That's possible because the image installs the harness as a **GitHub dependency** (see
this example's own `pyproject.toml`) rather than by `COPY`ing the repo's source tree. The scoped
`pyproject.toml` pulls exactly one thing — `temporal-agent-harness[sandbox]` from the
`sandbox-tools` branch — which is all `tools.py` needs at import time (the
`@agent.activity_tool_defn` decorator + `SandboxConfig`/`Daytona`, plus `remote-box` and the
Daytona SDK the `sandbox` extra brings). No `uv.lock` is committed — this project is only ever built
inside the snapshot, never locally, so the Dockerfile's `uv sync` resolves fresh at build time. That
keeps the source tree clean at the cost of pinning: a rebuild follows the branch's current HEAD.

The one thing the git dependency can't provide is `examples/` itself — the repo's wheel ships only
`temporal_agent_harness*`, so `examples/` is never part of the installed package. And remote-box
re-imports the tool inside the sandbox by its exact dotted path `examples.sandboxed_tool_demo.tools`
(it uses the function's real `__module__`). So the Dockerfile `COPY`s `tools.py` + its package
`__init__.py` to `/app/examples/sandboxed_tool_demo/`, and since `/app` is the interpreter's working
directory it's on `sys.path` — that's how `from examples.sandboxed_tool_demo.tools import run_bash`
resolves at runtime. (Daytona's builder git-clones the harness from GitHub during the build, so the
`sandbox-tools` branch must be pushed — it is — and the repo reachable — `temporal-community` is
public. Once the sandboxed-tool support merges to `main`, repoint `pyproject.toml` at a tag/`main`
for stability.)

Built ahead of time, never at runtime (`SandboxConfig.require_prebuilt` defaults to `True`):

```python
from temporal_agent_harness.harness.sandbox import build_sandbox
from examples.sandboxed_tool_demo.tools import SANDBOX
build_sandbox(SANDBOX)  # first run takes a while (real image build); cached after that
```

Swap `Daytona(...)` for `remote.Subprocess()` (no API key, no image build — reuses your local
venv directly) or `remote.E2B(...)` in `tools.py`'s `SANDBOX` to run the exact same tool under a
different backend — nothing else in this demo changes, since the tool never chooses its own
backend (`harness/sandbox/`'s whole point).

## The approval gate

`run_bash` is never eligible for auto-approval under any policy this agent uses. Every call:
1. Pauses in-workflow, publishing a `tool_approval_requested` event (visible on the turn stream
   and via `GET /api/status/{session_id}`'s `pending_approvals`).
2. Waits — indefinitely, with no activity timeout consumed — for a human decision.
3. Resolves via `POST /api/approve` (or `AgentClient.approve_tool(tool_id, approved=...)`
   programmatically), publishing `tool_approval_resolved`, then either dispatches the sandboxed
   activity (approved) or reports the denial back to the model (denied) so it can react instead
   of retrying blindly.

See `docs/internal/human-in-the-loop-tool-approvals.md` for the full design.

## Interactive (chat through the real UI)

```bash
just build-sandbox      # once — builds the Daytona snapshot (needs DAYTONA_API_KEY)
just temporal            # 1. local Temporal dev server
just session-manager     # 2. shared session-manager worker
just server               # 3. FastAPI API + UI on :8000
just worker                # 4. this example's agent worker (needs GEMINI_API_KEY + DAYTONA_API_KEY)
```

Then open `http://localhost:8000`, pick "Sandboxed Tool Demo", and chat. Ask it to run something
(e.g. "what's in the current directory?" or "what's the Python version?") — it'll explain what
it's about to run, then wait for you to approve before the command actually executes in the
sandbox.

## Live preview: build a web app in the sandbox and open it in your browser

`preview_proxy.py` is a small, **self-contained** aiohttp server (it touches nothing in
`temporal_agent_harness/web/`) that lets you build a site inside the sandbox via chat and preview
it live. Three pieces make it work:

1. **The snapshot entrypoint (`supervise.sh`).** This example's `Dockerfile.sandboxed-tool-demo`
   installs `supervise.sh` as the container `ENTRYPOINT` and creates a persistent
   `/home/daytona`. On every boot — create, start, and start-from-archive — the supervisor watches
   `/home/daytona/start.sh` and launches whatever command it holds, restarting it if it exits. So
   when the proxy wakes a stopped sandbox, the server comes back automatically with no agent
   action. Daytona injects its own toolbox daemon independently of this entrypoint, so `run_bash`
   (`process.exec`) and preview links keep working — the entrypoint is purely a keepalive + server
   supervisor.

2. **The agent contract.** When you ask the agent to build a site/app (see `workflow.py`'s system
   instruction), it writes the project files, then writes the single foreground, `0.0.0.0`-bound
   launch command into `/home/daytona/start.sh`, then reads `$DAYTONA_SANDBOX_ID` from inside the
   sandbox and hands you the preview URL.

3. **The proxy (`preview_proxy.py`).** Path-routed as
   `http://localhost:8080/s/<sandboxId>/<port>/…` — no wildcard DNS or real domain needed for this demo (would be significantly better under a subdomain to avoid needing to workaround by prompting the agent to build sites with assets under this path). On each request it `daytona.get(sandboxId)`, calls `sandbox.start()` if the container is stopped, fetches a **fresh** preview link + token, waits for the server to actually bind the port, then forwards the request (HTTP and WebSocket/HMR).

### Run it

```bash
# First run above steps...then:
just preview-proxy      # the standalone preview proxy on :8080
```

Then, in the chat, ask e.g. *"build a hello-world site and serve it on port 3000"*. Approve the
`run_bash` calls; the agent will reply with a URL like
`http://localhost:8080/s/<sandboxId>/3000/`. Open it — the first hit wakes the sandbox and waits
for the server, so it may take a moment ("Warming up…"), then loads.

### Cost: idle sandboxes stop themselves

Waking a sandbox on a preview hit would otherwise leave it **running (billing compute) forever** —
preview HTTP traffic on its own never stops it. So the proxy sets a Daytona **auto-stop interval**
(`PREVIEW_AUTO_STOP_MINUTES`, default `3`, `0` to leave it unmanaged) on each sandbox it serves.
Daytona then auto-stops the sandbox after that many minutes with no *SDK* activity.

The nuance that makes this safe: Daytona counts SDK interactions (state changes, `process.exec`,
etc.) as activity but **not** preview HTTP traffic. So the agent's own `run_bash` keeps an active
chat turn alive (it never stops mid-turn), while a sandbox that's merely idle — e.g. a preview tab
left open but quiet — stops itself. The next request just wakes it again (a brief "Warming up…").
This is a **proxy-scoped** cost control: it sets the interval on the already-created sandbox and
changes nothing in the harness. (A stopped container still costs a little disk; the harness deletes
the sandbox entirely when the chat session ends, so that's bounded too.)

### Caveats (this is a demo, not production)

- **Lifetime is tied to the chat session.** The harness *stops* the sandbox between turns (a
  container sandbox's pause is stop — disk persists, processes are killed; that's why the
  supervisor relaunches the server on wake) and *deletes* it when the workflow ends. Once you close
  the session the preview 404s. A turn that runs right after you open a preview can also stop the
  sandbox out from under it.
- **Path-based routing.** Absolute asset URLs (`/style.css`) miss the `/s/<id>/<port>/` prefix —
  use relative paths or a `<base href>`. The agent is prompted to do this; a production proxy would
  rewrite them (or use host-based subdomain routing).
- **No auth.** The proxy is intentionally open — add your own gate before exposing it anywhere.
- Start from the official samples for anything real:
  <https://github.com/daytonaio/daytona-proxy-samples>.
