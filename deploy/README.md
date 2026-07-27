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
`main`. That same workflow also builds the **Daytona snapshot** the tools run in (from the
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
2. Push to `main` (or run the workflow manually via *Actions → build-image → Run workflow*). The run
   pushes the image **and** builds the Daytona snapshot.
3. Make the image package public so the host can pull without logging in:
   > GitHub → your profile → **Packages** → the `temporal-agents` container → **Package
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
`ghcr.io/jasonsteving99/temporal-agents:latest`, edit the `x-image` line in
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

1. **DNS — two records, not one:**
   - `*.<PREVIEW_BASE_DOMAIN>` → this host. Serves the sandbox subdomains.
   - `<PREVIEW_BASE_DOMAIN>` → this host. Serves the gallery, sign-in and admin panel.

   The second one is easy to miss: **a wildcard does not match the name it hangs off**, so
   `*.preview.example.com` leaves `preview.example.com` itself unresolvable. If your base domain is a
   zone apex your DNS host won't point at a bare IP, set `PREVIEW_AUTH_HOST` (e.g.
   `login.<PREVIEW_BASE_DOMAIN>`) to move those pages onto a name the wildcard already covers, and
   skip the extra record and the extra Caddy block below.
2. **Per-subdomain TLS + Host passthrough:** run a reverse proxy in front of the preview proxy (host
   port `3011`) that terminates TLS and forwards the Host header unchanged. A single-label domain
   pattern (`*.preview.example.com`) is why sandboxId+port share one label (`<sandboxId>-<port>`)
   rather than separate dotted labels.

[Caddy](https://caddyserver.com)'s `on_demand_tls` fits this better than a wildcard cert: sandbox
subdomains are unbounded and short-lived, so a DNS-01 wildcard cert would cover ids that no longer
exist, and a fresh per-id cert issued only when actually requested is more targeted anyway.
`on_demand_tls` issues a normal (non-wildcard) cert the first time each unique `<sandboxId>-<port>`
host is requested, gated by an `ask` callback — **required**, not optional: Caddy applies no rate
limiting of its own, so without it, scanners hitting random subdomains will burn your ~50/week
Let's Encrypt quota for the domain within the hour and get it rate-limited for a week.

The preview proxy already knows which sandboxes exist, so it exposes the `ask` check itself at
`GET /check?domain=<hostname>` — see `check()` in `preview/proxy.py`. It parses the hostname the
same way the main handler does and returns `200` only if that sandbox id actually exists (`403`
otherwise), so Caddy only issues certs for real, live sandboxes:

```caddy
{
    on_demand_tls {
        ask http://localhost:3011/check
    }
}

# The gallery / sign-in / admin host. A single known name, so it gets an ordinary
# automatic cert — no on-demand, no `ask` round trip. Caddy prefers this exact-match
# block over the wildcard below, so ordering doesn't matter.
preview.example.com {
    reverse_proxy localhost:3011
}

*.preview.example.com {
    tls {
        on_demand
    }
    reverse_proxy localhost:3011                 # the preview-proxy service; Host is preserved
}
```

(If you set `PREVIEW_AUTH_HOST` to something under the wildcard instead, drop the first block —
the wildcard already covers it, and `/check` returns `200` for that host so on-demand issues its
cert.)

The FastAPI server (`3010`) is a normal single-host site — front it however you like (e.g.
`agent.example.com → localhost:3010`).

## Preview auth (Firebase)

Previews are gated by a Google sign-in, so agent-built sites aren't readable by anyone who guesses a
sandbox subdomain. Set `FIREBASE_API_KEY` in `.env` to turn the gate on (blank = previews open to
all). Setup, once:

1. **Firebase Console → Authentication → Sign-in method:** enable the **Google** provider.
2. **→ Settings → Authorized domains:** add `<PREVIEW_BASE_DOMAIN>` (or whatever you set
   `PREVIEW_AUTH_HOST` to). That single host is the only origin sign-in ever runs on.
3. **→ Project settings → Your apps → SDK setup and configuration:** copy `apiKey`, `authDomain` and
   `projectId` into `FIREBASE_API_KEY` / `FIREBASE_AUTH_DOMAIN` / `FIREBASE_PROJECT_ID`.
4. Set **`PREVIEW_ADMIN_EMAILS`** to your own email. This is not optional — Google sign-in accepts
   *any* Google account, so the allowlist is what actually keeps strangers out, and with no admins
   and no guests nobody gets in at all.
5. Set `PREVIEW_SESSION_SECRET` (`openssl rand -hex 32`), or sessions die on each proxy restart.

## The preview gallery

**`https://<PREVIEW_BASE_DOMAIN>/`** lists every preview site the proxy has ever served — the
home page after you sign in, and the way to find sites from old agent sessions whose sandbox ids only
ever existed in a chat transcript. Admins get a **Manage access** link there too, so the admin path
isn't something to memorise.

Registration is automatic and needs no configuration: whenever the proxy successfully forwards a
request to a `<sandboxId>-<port>`, it remembers it.

**Tiles are screenshots, never iframes.** An iframe per app would wake every sandbox in the gallery
just to render the page, and each wake bills for compute. So screenshots are captured only at the
moments a sandbox is *already* awake and the capture is therefore free:

- the first time the proxy ever serves that app, and
- immediately after the proxy wakes a stopped sandbox for a real visitor — done in the background so
  the visitor isn't kept waiting, and it picks up whatever `start.sh` relaunched, so a rebuilt site
  gets a fresh shot.

**Refresh** on a tile forces a new screenshot and *will* wake the sandbox if it's stopped — the only
control here that can cost you anything, and the button says so. **Forget** (admins only) drops an
app from the gallery; it never touches the sandbox, and the app re-registers if anyone visits it
again. Dead sandboxes aren't pruned automatically — a transient Daytona error is indistinguishable
enough from "deleted" that auto-removal would eventually eat live entries — so stale tiles are left
showing their age for you to Forget.

Capture needs Playwright + Chromium, installed in the image by the `playwright install` layer in the
`Dockerfile` (~150MB, using the stripped `chromium-headless-shell` build). If that layer is removed
or the browser fails to launch, the gallery degrades to placeholder tiles and everything else keeps
working; set `PREVIEW_SCREENSHOTS=0` to skip the attempt entirely.

### Adding people

Sign in and open **`https://<PREVIEW_BASE_DOMAIN>/__auth/admin`** — or just click **Manage access**
on the gallery. Add or remove guests there
and it takes effect immediately — no restart, no redeploy, no editing `.env`. The list is stored as
JSON on the `preview-auth` Docker volume.

Access comes in two tiers, and they are stored in two different places on purpose:

| | Where it lives | Who can change it | What it grants |
|---|---|---|---|
| **Admins** | `PREVIEW_ADMIN_EMAILS` in `.env` | shell access to the host, then a restart | previews **+** the admin panel |
| **Guests** | JSON on the `preview-auth` volume | any admin, live from the panel | previews only |

That split is the security property: **nothing reachable over HTTP can create an admin.** The panel
writes only the guest file, so even a bug that exposed it could hand out preview access, never admin
— and your friends can't promote themselves or each other. There is deliberately no "make admin"
button; adding one would collapse the two tiers back into one.

Other things worth knowing:

- The allowlist is re-checked on **every request**, so removing a guest cuts off their existing
  session on their next click — you don't wait out their 7-day cookie.
- To non-admins the panel returns **404, not 403**, so a signed-in friend poking at URLs gets no
  hint that it exists.
- Admins are always allowed in, so you can't lock yourself out by clearing the guest list.
- A missing or corrupt guest file means *no guests*, never *everyone* — and since admins come from
  the environment, you can always still sign in and repair it.
- You can also edit the JSON on the host directly; the proxy notices the change without a restart.

How sign-in works: an unauthenticated request to any `<sandboxId>-<port>` subdomain is redirected to
`https://<PREVIEW_BASE_DOMAIN>/__auth/login`, which signs in with Firebase and posts the ID
token back. The proxy verifies that token against Google's Identity Toolkit (no service-account key
needed), checks the allowlist, and sets an HMAC-signed `HttpOnly` cookie scoped to
`<PREVIEW_BASE_DOMAIN>` — so one sign-in covers every sandbox subdomain and steady-state requests
cost no extra network calls. `/__auth/logout` clears it. See `preview/session.py`.

Update to a newer image:

```bash
docker compose pull && docker compose up -d
```

## Security notes (read before exposing this)

- **The preview proxy is only gated if you configure it** — see [Preview auth](#preview-auth-firebase).
  With `FIREBASE_API_KEY` blank, every preview is world-readable to anyone who guesses a subdomain.
- **The FastAPI server (`3010`) still has no auth of its own**, and both `3010`/`3011` bind publicly.
  Put them behind a firewall and a reverse proxy with TLS + your own auth before real use.
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
