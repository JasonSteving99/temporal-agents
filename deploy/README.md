# Self-hosting the sandboxed coding agent

Runs the four processes of the [sandboxed coding agent](../examples/sandbox_tools/coding_agent) on a
single host (e.g. a Hetzner box), pointed at **Temporal Cloud**. All four share one prebuilt public
image from GHCR — nothing is built on the host.

| Service | What it does | Host port |
| --- | --- | --- |
| `server` | FastAPI API + browser UI (the site you open) | `3010` |
| `worker` | The agent worker; its tools run in Daytona's cloud | — |
| `session-manager` | Launches the agent as a child workflow | — |
| `preview-proxy` | Serves web apps the agent builds inside a sandbox | `3011` |

The image is built and pushed to `ghcr.io/<owner>/<repo>` by
[`.github/workflows/build-image.yml`](../.github/workflows/build-image.yml) on every push to
`sandbox-tools`. That same workflow also builds the **Daytona snapshot** the tools run in (from the
image it just pushed, so the snapshot's content hash matches what the deployed worker looks up) — so
you normally don't run the snapshot build by hand.

## Prerequisites

- Docker + the Compose plugin on the host.
- A **Temporal Cloud** namespace + an API key (or client cert for mTLS).
- `GEMINI_API_KEY` and `DAYTONA_API_KEY`.
- The GHCR image published (below).
- For live preview: a **domain you control**, a **wildcard DNS record** `*.<preview-domain>` pointing
  at this host, and a reverse proxy that terminates wildcard TLS (see *Preview subdomains* below).

## One-time: CI, image, and snapshot

1. Add a **`DAYTONA_API_KEY`** repo secret (Settings → Secrets and variables → Actions) — the
   workflow's snapshot-build step needs it.
2. Push to the `sandbox-tools` branch (or run the workflow manually via *Actions → build-image → Run
   workflow*). The run pushes the image **and** builds the Daytona snapshot.
3. Make the image package public so the host can pull without logging in:
   > GitHub → your profile → **Packages** → the `temporal-agent-harness` container → **Package
   > settings** → **Change visibility** → **Public**.
   (Or keep it private and `docker login ghcr.io` on the host with a PAT that has `read:packages`.)

## Configure

From this `deploy/` directory:

```bash
cp .env.example .env                          # fill GEMINI_API_KEY, DAYTONA_API_KEY, PREVIEW_BASE_DOMAIN
cp temporal.cloud.toml.example temporal.toml  # fill Temporal Cloud address / namespace / api_key
```

Set `PREVIEW_BASE_DOMAIN` (e.g. `preview.example.com`) to the domain you'll serve previews under — it
feeds both the worker (the URL it hands the user) and the proxy (Host parsing). Leave it blank to turn
previews off entirely.

Both `.env` and `temporal.toml` are gitignored. If the image is under a different owner/tag than
`ghcr.io/jasonsteving99/temporal-agent-harness:sandbox-tools`, edit the `x-image` line in
`docker-compose.yml`.

## Run

```bash
docker compose up -d
docker compose logs -f            # watch them connect to Temporal Cloud
```

The Daytona snapshot is already built by CI. If you need to (re)build it from the host instead —
e.g. you're iterating without CI — run the one-shot (needs only `DAYTONA_API_KEY`):

```bash
docker compose --profile setup run --rm build-sandbox
```

Open `http://<host>:3010`, pick **"Sandboxed Coding Agent"**, and chat — e.g. *"build a hello-world
site and serve it on port 3000"*. Approve the `bash`/`write`/`edit` calls; for a web app the agent
serves it and hands you a preview URL like `https://<sandboxId>-3000.<preview-domain>/` — a real
subdomain that behaves like a normal site.

## Preview subdomains (wildcard DNS + TLS)

The preview proxy routes by **subdomain**: `https://<sandboxId>-<port>.<PREVIEW_BASE_DOMAIN>/` is
forwarded to that sandbox's port, path untouched — so previewed sites work like any normal site (no
subpath prefix, no `<base href>`). To make that reachable:

1. **Wildcard DNS:** add an `A`/`AAAA` record for `*.<PREVIEW_BASE_DOMAIN>` pointing at this host.
2. **Wildcard TLS + Host passthrough:** run a reverse proxy in front of the preview proxy (host port
   `3011`) that terminates TLS for `*.<PREVIEW_BASE_DOMAIN>` and forwards the Host header unchanged.
   A single-label wildcard cert (`*.preview.example.com`) is why sandboxId+port share one label
   (`<sandboxId>-<port>`) rather than separate dotted labels.

[Caddy](https://caddyserver.com) makes the wildcard cert painless (needs a DNS-provider plugin for
the ACME DNS-01 challenge). Minimal `Caddyfile`:

```caddy
*.preview.example.com {
    tls {
        dns <your-dns-provider> <credentials>   # e.g. cloudflare {env.CF_API_TOKEN}
    }
    reverse_proxy localhost:3011                 # the preview-proxy service; Host is preserved
}
```

The FastAPI server (`3010`) is a normal single-host site — front it however you like (e.g.
`agent.example.com → localhost:3010`).

Update to a newer image:

```bash
docker compose pull && docker compose up -d
```

## Security notes (read before exposing this)

- **The preview proxy has no auth** and both `3010`/`3011` bind publicly. Put them behind a firewall
  and a reverse proxy with TLS + your own auth before real use.
- Secrets live in `.env` / `temporal.toml` on the host — keep them `chmod 600` and off version control
  (already gitignored).

## Notes & limitations

- **Large payloads:** offloaded payloads use a Docker-managed named volume
  (`LARGE_PAYLOAD_DRIVER=local`), correct for one host — Docker owns the storage, there's no host
  path to manage. The offload driver never deletes what it writes, so a `payload-gc` sidecar prunes
  files older than `PAYLOAD_GC_MAX_AGE_DAYS` (default 7) to cap growth. The agent worker's data
  converter does not offload, so a single workflow input/result over ~1.5 MB would fail there —
  coding-agent payloads are small, so this doesn't bite in practice. Split across hosts? Switch to
  `LARGE_PAYLOAD_DRIVER=s3` (see `.env.example`).
- **Sandbox lifetime** is tied to the chat session; a preview 404s once you close the session. This
  is a demo proxy, not production — see [the example README](../examples/sandbox_tools/coding_agent/README.md).
