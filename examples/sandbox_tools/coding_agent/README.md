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
- `E2B_API_KEY` — the tools run in a real E2B cloud sandbox, and `just build-sandbox` builds its template.
- `DAYTONA_API_KEY` / `DAYTONA_TARGET` *(optional)* — only for the Daytona backend and its preview proxy.
- `TAILSCALE_OAUTH_CLIENT_SECRET` *(optional)* — puts each sandbox on your tailnet (see
  [Tailnet](#tailnet)).

All go in the repo-root `.env.local` (see `.env.example`).

## Backend: E2B (was Daytona)

The tools run in an **E2B** sandbox, built from `examples/Dockerfile.sandbox-coding-agent-e2b`.

**Why the switch.** Daytona restricts sandbox egress to a fixed "essential services" allowlist on
tiers 1–2, and it cannot be overridden at either the organization or the sandbox level — the API
answers `400 "Network access is restricted and cannot be overridden at the sandbox level"`, and the
docs are explicit that org policy wins even if you specify custom allow lists. Tailscale's control
plane isn't on that list, so `tailscaled` could never log in (it fails with
`fetch control key: ... read: connection reset by peer`). E2B has no such filter and provides
`/dev/net/tun`, so the tailnet works with a real interface.

Two consequences worth knowing:

- **The live-preview proxy was ported to E2B** and is better off for it — see
  [Live preview](#live-preview). E2B's memory-preserving pause removes the need to relaunch a
  server on wake, and `allow_public_traffic=False` makes preview URLs unshareable past the sign-in.
- **The Daytona Dockerfile is kept in the tree** (`Dockerfile.sandbox-coding-agent`) for reference.
  Nothing points at it; `supervise.sh` is now shared by both backends and does only server
  supervision, with the tailnet in `tailscale_up.sh`.

The Daytona snapshot is built from `examples/Dockerfile.sandbox-coding-agent`. Unusually, that Dockerfile
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

Each sandbox joins your Tailscale tailnet, as a node tagged `tag:agent-artifact`. This is the
example's use of `SandboxConfig`'s **backend provider**: the tailnet credential comes from the
worker's environment and has to ride into the sandbox as an `env_var`, so no literal `E2B(...)` in
`tools.py` could carry one. Instead `tools.py` names a provider and [`tailscale.py`](tailscale.py)
supplies the callable:

```
tools.py      SANDBOX = SandboxConfig(backend="e2b-tailscale", ...)    # a NAME
worker.py     sandbox_activities({PROVIDER_NAME: e2b_with_tailscale})  # the CALLABLE
```

The credential is consumed by `tailscale_up.sh`, baked into the template and run as the E2B backend's
**`post_create_cmd`** — fired once per sandbox after creation, as root, in the background, inheriting
the sandbox's `env_vars`. That hook is the *only* place it can be consumed: a template **start
command** runs while the template BUILDS and is snapshotted into every sandbox, so it starts before
any sandbox (and any per-sandbox env var) exists. remote-box translates a Dockerfile
`CMD`/`ENTRYPOINT` into exactly that start command, which is why the E2B Dockerfile has neither.

The harness resolves the name inside `sandbox_activate`, once per workflow run, on the turn that
creates the sandbox — off the workflow thread, with the resolved config recorded in history and
reused for the rest of the run. `tools.py` keeps the fields the snapshot's identity comes from in
`SANDBOX_BACKEND`, and the provider `model_copy`s that and adds only `env_vars`, so what CI built and
what runs can't drift apart. Offline builds pass it explicitly:
`build_sandbox(SANDBOX, backend=SANDBOX_BACKEND)`.

**The sandbox mints its own keys.** What `TAILSCALE_AUTHKEY` carries is not an auth key but an OAuth
client secret (`tskey-client-…`) with `?preauthorized=true&ephemeral=true` appended. The `tailscale`
CLI recognizes that prefix and, on every `up`, does an OAuth2 exchange with `api.tailscale.com` and
mints itself a fresh **single-use, ephemeral, pre-authorized** key stamped with `TAILSCALE_TAG` —
which is why that tag is mandatory here (`--advertise-tags`; the CLI refuses to mint without one).
This is the same mechanism [`tailscale/github-action`](https://github.com/tailscale/github-action)
uses to get a CI runner onto a tailnet, and it exists in the CLI itself, not the action
(`feature/oauthkey/oauthkey.go`).

Carrying the minting capability rather than a key is what makes the rejoin below work at all hours
and any number of times; a pre-minted key had to be *reusable* and *unexpired* to manage it even
once. The cost is real and worth stating: an OAuth client secret does **not** expire, and this one
lives in the sandbox's env where the agent's own `bash` tool can read it, plus in Temporal history.
It grants no extra reach — the CLI can only mint for tags the client owns, and every node it joins
carries the same tag — so scope the client to `auth_keys` on that one tag and rotate it on a
schedule. The tag remains the security boundary: agent-written code runs as that node, so grant it
the least your ACLs can. Two policy-file edits are required before any of this works (`tagOwners`
for the tag, plus an `acls` rule) — see `deploy/.env.example`. With
`TAILSCALE_OAUTH_CLIENT_SECRET` unset the provider returns the plain config and sandboxes join
nothing, so the example still runs without Tailscale.

Getting `tailscaled` to work inside an E2B sandbox took three fixes, each documented at length where
it lives, because every one of them presents as "the auth key was rejected" when it is nothing of the
kind:

1. **Link-local-only networking** (`tailscale_up.sh`, `ensure_routable_addr`). An E2B sandbox's `eth0`
   has a single `169.254.0.x/30` address and a link-local default gateway. Tailscale's network monitor
   ignores 169.254/16, concludes the network is down, logs `control: setPaused(true)` and parks the
   auth routine *before contacting the control plane* — while `curl https://controlplane.tailscale.com`
   returns 200 from the same shell. Adding a dummy interface with a routable `/32` fixes it. Userspace
   networking does **not** dodge this.
2. **The packaged systemd unit** (E2B Dockerfile). E2B runs systemd as PID 1 and the Debian package
   enables `tailscaled.service`, whose `ExecStartPre` runs `tailscaled --cleanup` — which *deletes the
   socket* our daemon just bound. The unit is masked so one daemon, ours, owns the socket.
3. **Build-time daemons get snapshotted** (E2B Dockerfile). E2B snapshots the build VM with its
   processes running, so a `tailscaled` started by the package's postinst reappears in every sandbox
   holding the socket with a network that died with the build. `policy-rc.d` suppresses the start, and
   the baked `tailscaled.state` is deleted so sandboxes don't all share one node identity.

Further caveats:

- **MagicDNS must be enabled on your tailnet** for tailnet *names* to resolve. Verified against this
  tailnet: the control plane sent an empty DNS config (`dns: Set: {DefaultResolvers:[]
  SearchDomains:[]}`), `MagicDNSSuffix` was empty and `/etc/resolv.conf` kept E2B's `8.8.8.8`, so only
  `100.x` addresses work. Enable it under **Admin → DNS**.

- **(Daytona backend) Egress to the control plane is blocked.** Observed July 2026: connections to
  `controlplane.tailscale.com` are reset (`fetch control key: ... read: connection reset by peer`),
  so `tailscaled` never reaches the coordination server and the auth key is never presented. The
  node sits in `NeedsLogin` and `tailscale status` says `Logged out.` — which looks like a key or ACL
  problem but is neither. Check `/home/daytona/server.log` first: the supervisor distinguishes this
  case explicitly. Sandbox hosts often block VPN control planes as an abuse vector; Daytona's create
  API has `domain_allow_list`/`network_allow_list`, but remote-box passes neither (it sends only
  `snapshot` and `env_vars`), so allowing them per-sandbox from the provider isn't possible today.
- **TUN vs userspace networking.** Both scripts detect `/dev/net/tun` and use a real interface when
  it's there (transparent routing; MagicDNS too, if enabled on the tailnet) — E2B and Daytona's
  container class both provide one. Without it, they fall back to `--tun=userspace-networking`, where the tailnet is reachable
  only through the SOCKS5/HTTP proxies on `localhost:1055`. Those get published to
  `/home/daytona/tailscale.env` rather than exported, so the agent's ordinary internet traffic (pip,
  npm, git) isn't silently pulled through the tailnet stack; point one command at the tailnet with
  `set -a; . /home/daytona/tailscale.env; set +a; curl http://host`.
- **Long pauses used to end the tailnet permanently, and this is the fix.** The harness pauses the
  sandbox between chat turns. E2B's pause is a SUSPEND, so `tailscaled` itself survives and a short
  pause is invisible (measured: node still on the tailnet after 10 minutes paused, and `Running` the
  instant it resumes). A *long* one is not: Tailscale reaps an ephemeral node that has been silent
  long enough, and the sandbox then resumes holding a node key for a machine the control plane has
  forgotten. Nothing in the old boot sequence ever looked again, so the tailnet — and with it the AI
  gateway — simply stopped answering mid-session, with no error anywhere near the cause.

  Two changes make it self-healing: the sandbox holds a credential it can mint **new keys** with
  (`tailscale.py`), and `boot.sh` runs a **watchdog** that re-runs `tailscale_up.sh` every
  `TAILSCALE_WATCH_SECONDS` (default 30), which re-authenticates with `--force-reauth` whenever this
  node is off the tailnet — retrying with backoff (`TS_UP_ATTEMPTS`), and, if a re-auth still can't
  make the saved identity work, deleting `tailscaled.state` and registering as a brand-new node. That
  last step is only affordable because keys are no longer scarce; it's the in-sandbox equivalent of
  the GitHub Action's `tailscaled --state=mem:`, which starts every CI run from a clean identity.

  What the watchdog checks is not the obvious thing, and this is the part worth remembering:
  **`BackendState` stays `Running` after the node is deleted.** `tailscale status` keeps printing the
  old 100.x address and every peer it last knew about; only the daemon log says
  `PollNetMap: initial fetch failed 404: node not found`, forever. The field that tells the truth is
  `Self.Online`, which flips to `false` within ~20s of the deletion. A watchdog keyed on
  `BackendState` alone sees a healthy node and does nothing — verified by deleting a paused sandbox's
  device through the API, which is the same end state the reaper leaves. With the `Self.Online` check
  (plus a 20s grace period, so a normal post-resume reconnect isn't mistaken for it) the same test
  recovers in under a minute: new address, back on the tailnet, gateway answering. Nothing now bounds
  how long a chat can idle and still get its tailnet back — the earlier version of this fix passed a
  reusable key with an expiry, so a chat resumed after that expiry was still stranded.

### Building AI features with no API key

Set `AI_GATEWAY_URL` (e.g. `http://llm`, a Tailscale Aperture node on your tailnet) and the agent is
told it can build AI features against it. Authorization is the sandbox's **tailnet identity**, so no
key exists or is needed — `google-genai` is pointed at the gateway with a placeholder `api_key` the
library insists on and the gateway ignores:

```python
from google import genai

client = genai.Client(api_key="unused", http_options={"base_url": "http://llm"})
resp = client.models.generate_content(model="gemini-3.6-flash", contents="...")
print(resp.text)
```

`api_key="unused"` is not decorative: the library refuses to construct a client without one, and the
gateway ignores it. The instruction also gives the async form (`client.aio.models.generate_content`)
and how to pass a system prompt, because those are the two things a model reaches for next and would
otherwise debug against a gateway that gives no hints.

Two things the instruction is emphatic about, because both are easy to get wrong:

- **Server-side only.** The gateway lives on the tailnet, which the *sandbox* joined — the user's
  browser did not. A client-side `fetch()` from a previewed page fails for everyone but the sandbox,
  so the app must expose its own endpoint that calls the gateway.
- **Never ask the user for a key.** There isn't one, and a model asked to "add AI" will otherwise
  reach for `GEMINI_API_KEY` out of habit.

Leave `AI_GATEWAY_URL` blank and the agent is never told the gateway exists. `AI_GATEWAY_MODELS`
(default `gemini-3.6-flash, gemini-3.5-flash-lite`) is the list it picks from, best first.

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

1. **The boot script (`boot.sh`, the template's `post_create_cmd`)** joins the tailnet, then hands
   off to **`supervise.sh`**, a keepalive that watches `/home/user/project/start.sh` (inside the
   project dir, so the agent's `write` tool can create it) and relaunches it if it dies.
2. **The agent** writes the foreground, `0.0.0.0`-bound launch command to `start.sh`, then reads
   `$E2B_SANDBOX_ID` and hands you `https://<sandboxId>-<port>.<PREVIEW_BASE_DOMAIN>/`.
3. **The proxy** routes by **subdomain**: it parses `<sandboxId>-<port>` from the Host header, resumes
   a paused sandbox on request, waits for the server to bind, then forwards HTTP + WebSocket/HMR
   traffic with the request path untouched, adding the sandbox's traffic token upstream.

E2B's `pause()` preserves **memory as well as disk**, so the agent's server is still running when a
later request resumes it — there is no relaunch to wait for. (On Daytona, stopping killed every
process, which is the only reason `supervise.sh` had to re-run `start.sh` on each boot. It is kept
here for restart-on-crash and because it gives the agent the `start.sh` contract its instructions are
written against.)

**Previews can't be shared past the sign-in.** The sandbox is created with
`allow_public_traffic=False`, so its own `https://<port>-<id>.e2b.app` URL returns **403** to
everyone; only the proxy holds the `e2b-traffic-access-token` (recovered via `connect()`). Daytona's
equivalent token travelled inside preview links, so it could leak past the gate — this cannot.

Routing by subdomain (not a `/s/<id>/<port>/` path prefix) is what lets a previewed site behave like
a normal site — it's served at the root of its own subdomain, so absolute asset paths work. Deploying
it needs a wildcard DNS record + a reverse proxy for wildcard TLS; see
[`deploy/README.md`](../../../deploy/README.md).

### The two front doors

`https://<PREVIEW_BASE_DOMAIN>/` serves one of two pages depending on who is asking, and
[`theme.py`](preview/theme.py) is the design system both are built from.

- **Signed out** you get the landing page ([`landing.py`](preview/landing.py)) — what a preview is,
  the three states a sandbox can be in, what signing in actually gets you, and the role table. It
  used to be an immediate bounce to a sign-in card that explained nothing, which stranded two kinds
  of visitor: someone handed a preview link who had no idea what they were being given, and someone
  not on the allowlist who had no way to know that being *added* was the missing step.
- **Signed in** you get the gallery ([`pages.py`](preview/pages.py)), with client-side search, sort,
  grouping by sandbox and a compact density — none of which costs a request. The three controls that
  change shared state are admin-only, because one registry is shared by everyone who signs in:
  **rename** (`POST /__apps/label`, since sites arrive named whatever `<title>` the agent wrote),
  **pin** (`POST /__apps/pin`, the only ordering a human controls), and **forget**.

Card state is **inferred, not observed**: a site visited within `PREVIEW_AUTO_STOP_MINUTES` is
labelled `awake`, anything older `asleep`. That is right almost always and wrong in one case worth
knowing — a sandbox the agent is actively working in gets no preview traffic, so it reads as asleep
while it is very much running. Asking E2B per tile would be an API call per card on every load.

Colour carries that state everywhere: **amber means running** (and therefore billing), **cold blue
means suspended with its memory intact**, grey means the session ended. It is the inversion of the
usual convention and it is deliberate — the thing that costs money is the thing that glows.

#### Landing-page demo clips

The landing page's three visuals are hand-built CSS by default. Each is also a **media slot** — drop
a file into `preview/static/media/` and it is used instead, from the next page load, no restart:

| Slot | Replaces | Capture as | Box |
| --- | --- | --- | --- |
| `hero.*` | the dashboard inside the browser frame | 6–12s clip | 2:1 |
| `gallery.*` | the mock grid in the "every site" card | 4–8s clip | 16:10 |
| `install.*` | the phone's screen in the "installs like an app" card | screenshot | 9:17 portrait |

`.webm`, `.mp4`, `.png`, `.jpg` and `.gif` all work; webm is offered to the browser first and mp4 is
the fallback, so ship both when you ship video.

**Two of the slots supply their own frame.** The hero draws the browser chrome and URL bar around
its slot; the install card draws the phone bezel and notch around its own. So in both cases capture
a **bare viewport** — a recording that includes browser or OS chrome ends up as chrome inside chrome.

Everything is `object-fit: cover`, so the box crops rather than letterboxes. Two consequences worth
planning for: anything taller than the box is cut from the bottom, and the `gallery` slot renders
only ~500px wide, so a 1440px-wide capture is unreadable there — record it at roughly 1120px, or
zoom to 133%, and check legibility at final size before you commit to a take.

Videos autoplay muted, looping and `playsinline`, with no controls. Record accordingly: no audio
track (`-an`), and **a loop whose last frame matches its first** — reset every control before you
stop recording, or there is a visible jump every few seconds, forever.

The CSS mocks are the default on purpose: they can never go out of date, they weigh nothing, and
they hold still under `prefers-reduced-motion`. Adding media is an upgrade, not a fix — and deleting
a file puts the mock straight back.

### Cost: idle sandboxes stop themselves

Resuming a sandbox on a preview hit would otherwise leave it billing compute forever. E2B has no
server-side idle stop to delegate this to, and its `timeout` is **not** an equivalent — when an E2B
timeout elapses the sandbox is *killed* and cannot be resumed, which would destroy the chat session,
not just the preview. So the proxy runs its own idle timer (`PREVIEW_AUTO_STOP_MINUTES`, default `10`)
and calls `pause()`, which preserves everything; the next request resumes it. `PREVIEW_SANDBOX_TIMEOUT_SECONDS`
(default ≥4× the idle window) is only a backstop against a leaked sandbox if the proxy dies before its
timer fires.

**Idle means nobody is using it — including the agent.** The agent's tool calls go worker → E2B and
never touch this proxy, so preview traffic alone is a false idle signal. The timer therefore reads the
sandbox's E2B lifetime (`end_at`) as a heartbeat: every tool call refreshes it, so a deadline that
hasn't changed across a whole window means genuinely nobody used the box. Without that check the proxy
would pause sandboxes mid-turn, and a pause from out here is **not** recoverable: remote-box only
auto-resumes a sandbox its own session paused, so the next tool call — and every retry — fails against
the paused box.

The heartbeat ticks once per tool call, which is why the window must stay well above the longest tool
activity (`bash` allows 3 minutes).

### Caveats (this is a demo, not production)

- **Lifetime is tied to the chat session.** The harness pauses the sandbox between turns and deletes
  it when the workflow ends, so once you close the session the preview 404s. On E2B that pause is a
  SUSPEND — memory and processes are frozen and come back on resume, so the agent's server does not
  need relaunching. It is also invisible to remote-box unless remote-box did it, which is why the
  preview proxy's own idle pause has to check first that nobody is using the box (above).
- **Subdomain routing needs infra.** Previews require a wildcard DNS record and a reverse proxy that
  terminates wildcard TLS and preserves the Host header (see `deploy/README.md`). Set
  `PREVIEW_BASE_DOMAIN` to enable them; leave it unset and the agent simply won't offer previews.
- **No auth** on the proxy — add your own gate before exposing it anywhere.
