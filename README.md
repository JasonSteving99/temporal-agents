# Sandboxed coding agent — self-host bundle

A trimmed fork of [`temporal-community/temporal-agent-harness`](https://github.com/temporal-community/temporal-agent-harness)
that keeps **only** the [sandboxed coding agent example](examples/sandbox_tools/coding_agent) and the
pieces needed to **self-host it**: an OpenAI coding agent, running durably on **Temporal**, whose
`bash`/`read`/`write`/`edit`/`grep`/`glob` tools execute inside an isolated **E2B cloud sandbox**.

The harness itself is **not** vendored here — it's pulled as a git dependency on the upstream repo's
`sandbox-tools` branch (see [`pyproject.toml`](pyproject.toml)). This repo contributes the example
code plus the deployment glue.

## What's here

```
examples/
  app.py                     # FastAPI + UI entrypoint (thin wrapper over the harness web app)
  session_manager_worker.py  # shared session-manager worker
  coding_agent_common/       # shared tool impls, todo tools
  sandbox_tools/coding_agent/# the agent: tools, workflow, worker, preview proxy, snapshot image
  Dockerfile.sandbox-coding-agent-e2b  # the E2B template the tools run in (built by `build-sandbox`)
  Dockerfile.sandbox-coding-agent      # the old Daytona snapshot, kept for reference only
Dockerfile                   # the runtime image for all four processes -> GHCR
.github/workflows/build-image.yml  # builds + pushes that image
deploy/                      # docker-compose + env templates + runbook for self-hosting
```

## Deploy it

See **[`deploy/README.md`](deploy/README.md)** — a `docker compose` stack (server, worker,
session-manager, preview-proxy) that runs the whole agent against Temporal Cloud from one prebuilt
public GHCR image.

## Learn how it works

The [example's own README](examples/sandbox_tools/coding_agent/README.md) explains the architecture:
sandboxed tools as durable activities, the approval gating, and the live-preview proxy.

Upstream project: https://github.com/temporal-community/temporal-agent-harness
