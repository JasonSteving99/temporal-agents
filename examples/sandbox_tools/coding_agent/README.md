# Sandboxed coding agent

A real Gemini **coding agent** whose tools run **inside an isolated Daytona cloud sandbox** — never
on the worker's or the user's machine. Ask it to build an app, add a feature, run tests, or explain
code, and an LLM reasons and calls `bash` / `read` / `write` / `edit` / `grep` / `glob` to do it,
all against a project that lives in the box.

It's the sandboxed sibling of [`examples/callback_tools/coding_agent`](../../callback_tools/coding_agent):
**same six tools, same underlying implementations** — the difference is *where* they run. The
callback agent runs its tools on your laptop (via the OpenCode shim, gated to protect your real
machine); this one runs them as durable activities inside a disposable sandbox, and pairs with a
**live-preview proxy** so you can build a web app in the box and open it in your browser.

## What's shared

The two coding agents duplicate almost nothing — the common pieces live in
[`examples/coding_agent_common`](../../coding_agent_common):

| Module | Role |
| --- | --- |
| `tool_impls.py` | The pure `(root, args) → result` implementations (`bash_exec`/`read_file`/`edit_file`/…). Both agents call these; this agent bakes it into its snapshot image. |
| `todo_tools.py` | `todowrite`/`todoread` — inline workflow tools for the agent's task list. |
| `chat_loop.py` | The Gemini Interactions streaming tool-calling loop. |

This example only supplies what genuinely differs: the tools declared as
`@agent.activity_tool_defn(sandboxed=True)` **`BaseModel`-in/out** tools (`tools.py`), the workflow
config (`sandbox=SANDBOX`), the worker, and the snapshot image.

## Requirements

- `GEMINI_API_KEY` — the agent calls the Gemini Interactions API.
- `DAYTONA_API_KEY` — the tools run on a real Daytona cloud sandbox (also used by the preview proxy).
- `DAYTONA_TARGET` *(optional)* — Daytona region for the preview proxy (e.g. `us`).
- `TAILSCALE_API_KEY` *(optional)* — puts each sandbox on your tailnet (see [Tailnet](#tailnet)).

All go in the repo-root `.env.local` (see `.env.example`).

## Backend: Daytona

The snapshot is built from `examples/Dockerfile.sandbox-coding-agent`. Unusually, that Dockerfile
lives at **`examples/`**, not in this example dir — because the image bakes in BOTH this example's
`tools.py` *and* the shared `coding_agent_common/tool_impls.py`, and Daytona resolves every `COPY`
source relative to the Dockerfile's own directory, so it must sit at the lowest common ancestor of
those two trees. A consequence: `local_project_root` is `examples/`, so the snapshot's content hash
covers everything under `examples/` — editing an unrelated example changes this snapshot's name, so
re-run `just build-sandbox` before the next run if that happens.

The image installs the harness (with its `sandbox` extra) **from GitHub** (a commit on the
`sandbox-tools` branch) rather than COPYing the repo source — the tools only need the harness +
`remote-box`/Daytona SDK, demonstrating that a sandboxed tool's deps are its own, independent of the
worker's. Built ahead of time, never at runtime (`SandboxConfig.require_prebuilt`):
`just build-sandbox`.

The dependency in [`pyproject.toml`](pyproject.toml) is pinned to an exact **commit SHA**, not the
branch name. That is deliberate: the snapshot's name is a hash of `examples/` + the Dockerfile, so an
upstream push doesn't invalidate it and `build_sandbox` would keep reporting `ready` while running a
stale harness. Bumping that SHA is what forces the rebuild — and it must match the commit
`../../../uv.lock` resolved for the worker image. The step-by-step is in the "Updating the harness"
header of [`examples/Dockerfile.sandbox-coding-agent`](../../Dockerfile.sandbox-coding-agent).

## Tailnet

Each sandbox joins your Tailscale tailnet on boot, as a node tagged `tag:agent-artifact`. This is
the example's use of `SandboxConfig`'s **backend provider**: an auth key has to be minted per
sandbox by an HTTP call, so no literal `Daytona(...)` in `tools.py` could carry one. Instead
`tools.py` names a provider and [`tailscale.py`](tailscale.py) supplies the callable:

```
tools.py      SANDBOX = SandboxConfig(backend="daytona-tailscale", ...)   # a NAME
worker.py     sandbox_activities({PROVIDER_NAME: daytona_with_tailscale}) # the CALLABLE
```

The harness resolves the name inside `sandbox_activate`, once per workflow run, on the turn that
creates the sandbox — so the key is minted at sandbox-creation time, off the workflow thread, and
the resolved config is recorded in history and reused for the rest of the run (never re-minted on
replay). `tools.py` keeps the fields the snapshot's identity comes from in `SANDBOX_BACKEND`, and
the provider `model_copy`s that and adds only `env_vars`, so what CI built and what runs can't drift
apart. Offline builds pass it explicitly: `build_sandbox(SANDBOX, backend=SANDBOX_BACKEND)`.

Keys are **single-use, ephemeral, pre-authorized and tagged**. The tag is the security boundary —
agent-written code runs as that node, so grant it the least your ACLs can. Two policy-file edits are
required before this works at all (`tagOwners` for the tag, plus an `acls` rule) — see
`deploy/.env.example`. With `TAILSCALE_API_KEY` unset the provider returns the plain config and
sandboxes join nothing, so the example still runs without Tailscale.

Inside the sandbox, `supervise.sh` runs `tailscale up` before launching the agent's server. Two
caveats worth knowing:

- **Userspace networking.** Daytona container sandboxes have no `/dev/net/tun`, so `tailscaled`
  runs with `--tun=userspace-networking` and the tailnet is reachable only through the SOCKS5/HTTP
  proxies it listens on (`localhost:1055`). Those are published to `/home/daytona/tailscale.env`
  rather than exported globally, so the agent's ordinary internet traffic (pip, npm, git) isn't
  silently pulled through the tailnet stack. To point one command at the tailnet:
  `set -a; . /home/daytona/tailscale.env; set +a; curl http://host`. If the sandbox class ever does
  get a TUN device, the script detects it and uses a real interface instead, with no proxy needed.
- **Restarts.** The key is single-use and the node is ephemeral, so a sandbox that is stopped and
  started again (which the preview proxy does routinely) rejoins using the `tailscaled.state` it
  persisted — but only while the tailnet still recognises that node. Once an ephemeral node has been
  offline long enough to be reaped, there is no second key to redeem and the sandbox comes back
  without the tailnet (logged, never fatal). If sandboxes need to survive long stops, mint a
  reusable key instead — flip `"reusable"` in `tailscale.py` and accept that a key read out of a
  sandbox can then join more nodes.

## Approvals

The tools run in a disposable box, so the blast radius is contained — but the mutating tools
(`bash` / `write` / `edit`) are still **gated** (`ToolApprovalPolicy.allow_inherently_safe()`) and
the read-only tools (`read` / `grep` / `glob`) + the plan tools auto-approve, mirroring the callback
agent's UX. Approvals surface in the Svelte UI (`GET /api/status/{session_id}`'s `pending_approvals`,
resolved via `POST /api/approve`).

## Run

```bash
just build-sandbox      # once — builds the Daytona snapshot (needs DAYTONA_API_KEY)
just temporal           # 1. local Temporal dev server
just session-manager    # 2. shared session-manager worker
just server             # 3. FastAPI API + UI on :8000
just worker             # 4. this example's agent worker (needs GEMINI_API_KEY + DAYTONA_API_KEY)
just preview-proxy      # 5. (optional) live-preview proxy on :8080
```

Open `http://localhost:8000`, pick "Sandboxed Coding Agent", and chat — e.g. *"build a hello-world
site and serve it on port 3000"*. Approve the `bash`/`write` calls; the agent scaffolds the project
in `/home/daytona/project`, and (for a web app) writes the launch command to `start.sh` in the
project dir and gives you a preview URL.

## Live preview

The `preview/` package is a small, self-contained aiohttp server (it touches nothing in the harness
web app) that lets you open a server the agent started inside the sandbox. Its `__init__.py` has a
map of the modules; `preview/proxy.py` is the request path and `preview/allowlist.py` is the access
model. It works like this:

1. **The snapshot entrypoint (`supervise.sh`)** is a keepalive that watches
   `/home/daytona/project/start.sh` (inside the project dir, so the agent's `write` tool can create
   it) and (re)launches it on every boot — so a woken sandbox re-serves automatically.
2. **The agent** writes the foreground, `0.0.0.0`-bound launch command to `start.sh`, then reads
   `$DAYTONA_SANDBOX_ID` and hands you `https://<sandboxId>-<port>.<PREVIEW_BASE_DOMAIN>/`.
3. **The proxy** routes by **subdomain**: it parses `<sandboxId>-<port>` from the Host header, wakes a
   stopped sandbox on request, fetches a fresh preview token, waits for the server to bind, then
   forwards HTTP + WebSocket/HMR traffic with the request path untouched.

Routing by subdomain (not a `/s/<id>/<port>/` path prefix) is what lets a previewed site behave like
a normal site — it's served at the root of its own subdomain, so absolute asset paths work. Deploying
it needs a wildcard DNS record + a reverse proxy for wildcard TLS; see
[`deploy/README.md`](../../../deploy/README.md).

### Cost: idle sandboxes stop themselves

Waking a sandbox on a preview hit would otherwise leave it billing compute forever (preview HTTP
traffic doesn't count as activity). So the proxy sets a Daytona **auto-stop interval**
(`PREVIEW_AUTO_STOP_MINUTES`, default `3`) on each sandbox it serves. SDK interactions — including the
agent's own tool calls — count as activity, so an active session never stops mid-turn; an idle
preview stops itself and re-wakes on the next request.

### Caveats (this is a demo, not production)

- **Lifetime is tied to the chat session.** The harness stops the sandbox between turns (a container
  sandbox's pause is stop — disk persists, processes are killed; the supervisor relaunches the server
  on wake) and deletes it when the workflow ends. Once you close the session, the preview 404s.
- **Subdomain routing needs infra.** Previews require a wildcard DNS record and a reverse proxy that
  terminates wildcard TLS and preserves the Host header (see `deploy/README.md`). Set
  `PREVIEW_BASE_DOMAIN` to enable them; leave it unset and the agent simply won't offer previews.
- **No auth** on the proxy — add your own gate before exposing it anywhere.
