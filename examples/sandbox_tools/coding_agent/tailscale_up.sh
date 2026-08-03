#!/usr/bin/env bash
#
# /usr/local/bin/tailscale-up.sh — get this E2B sandbox onto the tailnet, and KEEP it there.
#
# Run twice over: once at boot, then repeatedly by boot.sh's watchdog for the life of the sandbox.
# The repeat is not belt-and-braces — the sandbox is paused between chat turns, and Tailscale removes
# an ephemeral node that has been silent long enough, so a session resumed after a long idle comes
# back holding a node key for a machine the control plane has forgotten. Every pass therefore has to
# be cheap when there is nothing to do (it returns at the first check while the node is genuinely on
# the tailnet) and has to be able to re-authenticate from scratch when there is. "Genuinely" is
# load-bearing: see self_online.
#
# Run as this example's E2B `post_create_cmd` (see tools.py's SANDBOX_BACKEND): fired once
# immediately after sandbox creation, as root, in the background, inheriting the sandbox's env_vars —
# which is where TAILSCALE_AUTHKEY and TAILSCALE_TAG arrive from the worker (tailscale.py).
#
# TAILSCALE_AUTHKEY is normally NOT an auth key: it is an OAuth client secret (`tskey-client-...`)
# with `?preauthorized=true&ephemeral=true` appended, and the `tailscale` CLI mints itself a fresh
# single-use key from it on every `up`. That is what makes re-authentication above possible at all
# hours and any number of times — a pre-minted key had to be reusable and unexpired to survive one
# pause, and eventually ran out of road. It is also why TAILSCALE_TAG is mandatory here: the CLI
# refuses an OAuth secret with no `--advertise-tags` to stamp the key it mints. A literal auth key
# still works and skips both (ts_login detects which it has).
#
# It CANNOT be the template's start command: that runs while the template builds and is snapshotted
# into every sandbox, so it starts before any sandbox env exists. post_create_cmd is the only hook
# that sees the credential. (boot.sh is what post_create_cmd actually points at; it runs this script
# first, then the watchdog, then hands off to supervise.sh. This file stays purely "the tailnet" so
# it is usable on its own.)
#
# Never fatal: every failure is logged and the script exits 0. Losing the tailnet must not cost the
# sandbox — remote-box backgrounds this and logs a launch failure as a warning, and the agent's tools
# work regardless.
#
# Idempotent, as the hook contract requires and the watchdog depends on: safe to run any number of
# times (a second run finds the daemon already up and returns immediately).

set -u -o pipefail

LOG="${TAILSCALE_BOOT_LOG:-/var/log/tailscale-boot.log}"
TS_STATE="${TS_STATE:-/var/lib/tailscale/tailscaled.state}"
TS_SOCK="${TS_SOCK:-/var/run/tailscale/tailscaled.sock}"
TS_PROXY_ADDR="${TS_PROXY_ADDR:-localhost:1055}"
TS_ENV_FILE="${TS_ENV_FILE:-/home/user/tailscale.env}"
# The node's name in the tailnet. Every E2B sandbox reports the same `hostname` ("e2b.local"), which
# would make every node collide and get suffixed by Tailscale (agent-e2b, agent-e2b-1, ...), so prefer
# the sandbox id when E2B exposes one. Dots are stripped either way: a name with a dot in it reads as
# a subdomain in MagicDNS names.
TS_HOSTNAME="${TAILSCALE_HOSTNAME:-agent-$(echo "${E2B_SANDBOX_ID:-$(hostname)}" | tr '.' '-')}"
# See ensure_routable_addr: a routable address tailscale's netmon can see. /32 so it contributes no
# subnet route; override if 10.99.99.1 collides with something real on your tailnet.
TS_DUMMY_IF="${TS_DUMMY_IF:-ts-dummy0}"
TS_DUMMY_ADDR="${TS_DUMMY_ADDR:-10.99.99.1/32}"
# How hard to try each `tailscale up`. The OAuth path makes two API round-trips (token exchange, then
# key creation) BEFORE the login itself, so a single short attempt is a good deal more fragile than
# it was when the key arrived pre-minted; a transient 5xx from api.tailscale.com used to be
# impossible and now costs the tailnet until the next watchdog pass. Retries are linear-backoff, the
# same shape tailscale/github-action settled on for the identical call.
TS_UP_ATTEMPTS="${TS_UP_ATTEMPTS:-3}"
TS_UP_TIMEOUT="${TS_UP_TIMEOUT:-45s}"

log() {
  printf '[tailscale-up %s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$LOG" >&2
}

# Tailscale's network monitor decides whether a network exists at all, and it IGNORES link-local
# (169.254/16) addresses. An E2B sandbox's eth0 has nothing else — its only address is a
# 169.254.0.x/30 with a link-local default gateway — so tailscaled concludes the network is down,
# logs `control: setPaused(true)`, and parks the auth routine BEFORE ever contacting the control
# plane. Login then times out with the node still `NeedsLogin` and health reporting "Tailscale cannot
# connect because the network is down" — while `curl https://controlplane.tailscale.com/key` returns
# 200 from the same shell. Nothing about the key, the tag or the ACLs is involved.
#
# Giving any interface a routable address is enough to satisfy the monitor. A dummy interface is used
# rather than touching eth0, and a /32 so it adds no subnet route that could shadow a real tailnet
# route. Verified: with this, `tailscale up` succeeds and the node gets its 100.x address.
#
# Userspace networking does NOT avoid this — the monitor gates that path too, so the fallback below
# is no help on E2B. No-op wherever the sandbox already has a routable address (e.g. Daytona).
ensure_routable_addr() {
  if ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 \
      | grep -qvE '^169\.254\.'; then
    return 0
  fi
  log "only link-local addresses found; adding $TS_DUMMY_ADDR on $TS_DUMMY_IF for tailscale's netmon"
  modprobe dummy 2>/dev/null || true
  ip link add "$TS_DUMMY_IF" type dummy 2>/dev/null || true
  ip addr add "$TS_DUMMY_ADDR" dev "$TS_DUMMY_IF" 2>/dev/null || true
  ip link set "$TS_DUMMY_IF" up 2>/dev/null || true
  if ip -4 -o addr show dev "$TS_DUMMY_IF" 2>/dev/null | grep -q inet; then
    log "$TS_DUMMY_IF up with $TS_DUMMY_ADDR"
  else
    log "could not add $TS_DUMMY_IF; tailscale will likely report the network as down"
  fi
}

# Write the tailnet's peers into /etc/hosts, so `curl http://llm.your-tailnet.ts.net/` and the short
# `http://llm/` both work inside the sandbox.
#
# This should be MagicDNS's job and isn't, in an E2B sandbox. Observed with MagicDNS enabled
# tailnet-wide and `tailscale dns status` reporting "Tailscale DNS: enabled": tailscaled never
# rewrites /etc/resolv.conf (it stays on E2B's `nameserver 8.8.8.8`), and pointing resolv.conf at
# 100.100.100.100 by hand does not help either — the resolver is routable (`ip route get` resolves it
# via tailscale0, table 52) and answers, but with header-only replies containing no records. That
# matches tailscaled's own log of what control sent it: `dns: Set: {DefaultResolvers:[] Routes:{}
# SearchDomains:[] Hosts:0}` — zero host records, so its resolver has nothing to serve. Root cause
# not established; this sidesteps it entirely.
#
# The trade-off, deliberately accepted: /etc/hosts is a SNAPSHOT taken at boot. A peer that joins the
# tailnet later, or changes address, won't resolve until this runs again. Names are a convenience here
# — `tailscale ping` and raw 100.x addressing work regardless.
publish_peer_hosts() {
  local py=""
  for c in /app/.venv/bin/python python3 python; do
    command -v "$c" >/dev/null 2>&1 && { py=$c; break; }
  done
  [ -n "$py" ] || { log "no python; skipping /etc/hosts peer entries"; return 0; }

  # Code goes via -c, NOT a heredoc: a heredoc would replace stdin and the piped JSON would never
  # reach python (shellcheck SC2259). Single-quoted, so the code below uses only double quotes.
  # Idempotent — the marked block is rewritten wholesale, never appended to.
  local n
  n=$(tailscale --socket="$TS_SOCK" status --json 2>/dev/null | "$py" -c '
import json, sys
MARK = "# --- tailnet peers (managed by tailscale-up.sh) ---"
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
nodes = list((d.get("Peer") or {}).values())
if d.get("Self"):
    nodes.append(d["Self"])
lines = []
for node in nodes:
    ips = node.get("TailscaleIPs") or []
    dns = (node.get("DNSName") or "").rstrip(".")
    if not ips or not dns:
        continue
    v4 = next((i for i in ips if ":" not in i), None)
    if v4:
        lines.append(v4 + "\t" + dns + " " + dns.split(".")[0])
with open("/etc/hosts") as f:
    kept = f.read().split(MARK)[0].rstrip("\n")
with open("/etc/hosts", "w") as f:
    f.write(kept + "\n" + MARK + "\n" + "\n".join(sorted(lines)) + "\n")
print(len(lines))
' 2>/dev/null) || true
  log "published ${n:-0} /etc/hosts entries for tailnet peers"
}

# This node's Tailscale node ID, or "unknown". Only meaningful once logged in. Parsed with python
# because "ID" appears on every peer too, so position-based grepping would be fragile.
node_id() {
  local py=""
  for c in /app/.venv/bin/python python3 python; do
    command -v "$c" >/dev/null 2>&1 && { py=$c; break; }
  done
  [ -n "$py" ] || { echo "unknown"; return 0; }
  tailscale --socket="$TS_SOCK" status --json 2>/dev/null \
    | "$py" -c 'import json,sys
try:
    print(json.load(sys.stdin).get("Self", {}).get("ID") or "unknown")
except Exception:
    print("unknown")' 2>/dev/null || echo "unknown"
}

# The daemon's own view of whether it has an identity: NeedsLogin / Stopped / Running / Starting, or
# empty if it isn't answering yet. `tailscale status` (non-JSON) can't be used for this — it exits 1
# both for "no daemon at all" and for the healthy "Logged out." a fresh sandbox reports, which are
# opposite situations. grep/sed rather than a JSON tool: the field is a flat string and this image
# isn't guaranteed to have one.
backend_state() {
  tailscale --socket="$TS_SOCK" status --json 2>/dev/null \
    | grep -o '"BackendState"[[:space:]]*:[[:space:]]*"[^"]*"' \
    | head -1 \
    | sed 's/.*"\([^"]*\)"$/\1/'
}

# Whether the CONTROL PLANE still knows this node: "true", "false", or "" if it can't be read.
#
# The field that actually matters, and the one the obvious check misses. When Tailscale deletes this
# node — which is what happens to an ephemeral node whose sandbox stayed paused too long — tailscaled
# does NOT go back to NeedsLogin. It stays `BackendState: Running`, keeps serving its old `tailscale
# status` output, complete with a 100.x address and every peer it last knew about, and only its log
# betrays the truth: `PollNetMap: initial fetch failed 404: node not found`, forever. Measured: this
# flips to false within ~20s of the node being deleted while BackendState never budges. So a watchdog
# keyed on BackendState alone sees a perfectly healthy node and never lifts a finger, which is
# precisely how a session could sit "on the tailnet" for hours while nothing on it answered.
#
# Parsed with python (like node_id) because "Online" appears on every peer too.
self_online() {
  local py=""
  for c in /app/.venv/bin/python python3 python; do
    command -v "$c" >/dev/null 2>&1 && { py=$c; break; }
  done
  [ -n "$py" ] || return 0
  tailscale --socket="$TS_SOCK" status --json 2>/dev/null \
    | "$py" -c 'import json,sys
try:
    v = json.load(sys.stdin).get("Self", {}).get("Online")
except Exception:
    v = None
print("" if v is None else str(v).lower())' 2>/dev/null
}

main() {
  if ! command -v tailscaled >/dev/null 2>&1; then
    log "tailscaled not installed; nothing to do"
    return 0
  fi
  if [ -z "${TAILSCALE_AUTHKEY:-}" ] && [ ! -s "$TS_STATE" ]; then
    log "no TAILSCALE_AUTHKEY and no saved state; not joining a tailnet"
    return 0
  fi
  if [ "$(id -u)" != "0" ]; then
    log "not root (uid=$(id -u)); tailscaled needs root — set post_create_cmd to run as root"
    return 0
  fi

  mkdir -p "$(dirname "$TS_STATE")" "$(dirname "$TS_SOCK")" 2>/dev/null || true

  # Already up from an earlier run of this script? Then there is nothing to do. This is also the
  # fast path of the watchdog boot.sh runs, which calls this script every TAILSCALE_WATCH_SECONDS
  # for the life of the sandbox — so everything below has to be cheap to reach and safe to repeat.
  #
  # "Running" alone is not enough, though: see self_online. A node that has been deleted from the
  # tailnet keeps reporting Running forever, so this also asks whether the control plane still
  # agrees, and falls through to re-authentication when it doesn't.
  if [ "$(backend_state)" = "Running" ]; then
    [ "$(self_online)" = "false" ] || return 0
    # Being briefly offline is normal — a resumed sandbox reconnects a moment after it wakes, and
    # re-registering during that window would abandon a perfectly good node and make a new one. Only
    # a node that is STILL offline after the grace period is genuinely gone.
    sleep "${TS_OFFLINE_GRACE:-20}"
    [ "$(self_online)" = "false" ] || return 0
    log "node reports Running but the control plane does not know it (deleted during a long pause?);" \
        "re-authenticating"
  fi

  local userspace=""
  if [ -n "$(backend_state)" ]; then
    # A daemon is answering; this node just isn't on the tailnet (logged out, or holding an identity
    # the control plane has forgotten). Nothing to restart: leave the daemon alone and go straight to
    # `up` below, which re-redeems the key. Restarting it here instead would throw away a perfectly
    # good daemon on every pass of the watchdog.
    log "daemon is answering (state $(backend_state)) but this node is not on the tailnet;" \
        "re-authenticating"
    [ -c /dev/net/tun ] || userspace=1
    ensure_routable_addr
  else
    ts_start_daemon || return 0
    [ -c /dev/net/tun ] || userspace=1
  fi

  ts_login "$userspace" || return 0

  if [ -n "$userspace" ]; then
    {
      echo "# Point ONE command at the tailnet:  set -a; . $TS_ENV_FILE; set +a; curl http://host"
      echo "export ALL_PROXY=socks5h://$TS_PROXY_ADDR"
      echo "export HTTP_PROXY=http://$TS_PROXY_ADDR"
      echo "export HTTPS_PROXY=http://$TS_PROXY_ADDR"
    } >"$TS_ENV_FILE" 2>/dev/null || true
  fi

  publish_peer_hosts
  tailscale --socket="$TS_SOCK" status >>"$LOG" 2>&1 || true
  # The node ID is logged because it is the unit other systems account against: Tailscale Aperture's
  # `<node>` quota template expands to exactly this, giving a distinct quota per sandbox even though
  # every sandbox shares one tag (its `<user>` template expands to the tag, which collapses them).
  # Node ID -> sandbox is then an exact join: the Tailscale devices API maps this ID to the hostname
  # above, which is agent-<E2B_SANDBOX_ID>.
  log "tailnet ready as $TS_HOSTNAME (node id $(node_id))"
}

# Start our own tailscaled and wait for it to answer. Only called when nothing is answering.
ts_start_daemon() {
  # Any OTHER tailscaled must go before we start ours. A daemon can be inherited from the template
  # snapshot (E2B snapshots the build VM with its processes running, so anything that started
  # tailscaled during the build — notably the Debian package's postinst — reappears in every
  # sandbox). Such a daemon is unusable: its network died with the build VM, it reports "the network
  # is down" no matter what, and it holds the socket so ours exits with "address already in use".
  # The Dockerfile suppresses that start, but a stale socket alone is enough to break the bind, so
  # clear both here rather than trusting the image. Not reachable for a daemon that ANSWERS — those
  # two cases returned above.
  if pkill -x tailscaled 2>/dev/null; then
    log "killed a pre-existing tailscaled (inherited from the template snapshot?)"
    sleep 1
  fi
  rm -f "$TS_SOCK" /run/tailscale/tailscaled.sock 2>/dev/null || true

  ensure_routable_addr

  if [ -c /dev/net/tun ]; then
    log "/dev/net/tun present; starting with a real TUN interface"
    tailscaled --state="$TS_STATE" --socket="$TS_SOCK" >>"$LOG" 2>&1 &
  else
    # Fallback for a sandbox class without a TUN device: tailscaled joins the tailnet as a normal
    # node, but with no kernel route, so traffic reaches the tailnet only through the proxies it
    # listens on. Published to $TS_ENV_FILE rather than exported, so the agent's ordinary internet
    # traffic (pip, npm, git) isn't silently pulled through the tailnet stack.
    log "no /dev/net/tun; starting in userspace mode (proxies on $TS_PROXY_ADDR)"
    tailscaled --state="$TS_STATE" --socket="$TS_SOCK" \
      --tun=userspace-networking \
      --socks5-server="$TS_PROXY_ADDR" \
      --outbound-http-proxy-listen="$TS_PROXY_ADDR" >>"$LOG" 2>&1 &
  fi

  # Wait until the daemon ANSWERS, not merely until its socket file exists (it appears before the
  # daemon serves on it). `tailscale up` against a not-yet-listening socket fails immediately.
  local ready=""
  for _ in $(seq 1 30); do
    if [ -n "$(backend_state)" ]; then ready=1; break; fi
    sleep 0.5
  done
  [ -n "$ready" ] || log "daemon did not answer within 15s; trying anyway"
}

# Run `tailscale up` with the given flags, retrying with linear backoff. All output to the log.
ts_up_retry() {
  local attempt=1
  while :; do
    if tailscale --socket="$TS_SOCK" up --timeout="$TS_UP_TIMEOUT" "$@" >>"$LOG" 2>&1; then
      return 0
    fi
    [ "$attempt" -ge "$TS_UP_ATTEMPTS" ] && return 1
    log "up attempt $attempt/$TS_UP_ATTEMPTS failed; retrying in $((attempt * 2))s"
    sleep "$((attempt * 2))"
    attempt=$((attempt + 1))
  done
}

# Throw this node's identity away and start over with a brand-new machine key.
#
# The last resort, and the one that only became affordable with an OAuth secret in hand. When a node
# has been deleted from the tailnet, `up --force-reauth` is supposed to re-register it — but if the
# daemon's saved state is what tailscaled is choking on, no amount of re-auth fixes it, and the old
# code had nothing left to try. Wiping the state file is the equivalent of the GitHub Action's
# `tailscaled --state=mem:`, which gets a fresh identity on every single run and never has this class
# of problem at all. Costs a key (fine — each `up` mints its own now) and the node's 100.x address,
# which nothing here holds onto.
ts_reset_identity() {
  log "discarding saved node state and re-registering from scratch"
  # The daemon has to go BEFORE the state file: a live tailscaled rewrites it on the way out, so
  # deleting it first accomplishes nothing. ts_start_daemon clears the socket and re-pkills (a no-op
  # by then) on its own.
  pkill -x tailscaled 2>/dev/null && sleep 1
  rm -f "$TS_STATE" 2>/dev/null || true
  ts_start_daemon
}

# Log this node in, from whatever state the daemon is in. $1 non-empty means userspace mode.
# Returns non-zero when the node did NOT come up, so callers can bail quietly.
ts_login() {
  local userspace="$1"
  local up_args="--hostname=$TS_HOSTNAME"
  # MagicDNS points /etc/resolv.conf at 100.100.100.100, which nothing can route without a TUN
  # device — that would break ALL name resolution, not just tailnet names.
  [ -n "$userspace" ] && up_args="$up_args --accept-dns=false"

  # The OAuth path needs a tag to stamp the key it is about to mint, and refuses to mint without one
  # ("oauth authkeys require --advertise-tags"). A literal auth key carries its tags already, and
  # passing a mismatched --advertise-tags alongside one is an error — so this flag goes on ONLY for a
  # client secret, which is exactly what the credential's prefix tells us.
  case "${TAILSCALE_AUTHKEY:-}" in
    tskey-client-*)
      if [ -z "${TAILSCALE_TAG:-}" ]; then
        log "TAILSCALE_AUTHKEY is an OAuth client secret but TAILSCALE_TAG is unset;" \
            "the CLI cannot mint a key without a tag — continuing without the tailnet"
        return 1
      fi
      up_args="$up_args --advertise-tags=$TAILSCALE_TAG"
      ;;
  esac

  # Which `up` to run comes from the DAEMON's state, never from whether the state file exists:
  # tailscaled writes that file (its machine key) as soon as it starts, before any login. A bare
  # `tailscale up` on a logged-out node starts an INTERACTIVE login, printing an auth URL and
  # blocking for a browser that is never coming. --timeout bounds every path regardless.
  local state
  state=$(backend_state)
  log "backend state is ${state:-unknown}"

  # shellcheck disable=SC2086  # $up_args is a flag list; word splitting is the point
  if [ "$state" = "Stopped" ]; then
    # One attempt, not $TS_UP_ATTEMPTS: this is the cheap "maybe the saved identity still works"
    # probe, and the authenticated path below is the real one. Retrying a dead identity three times
    # just delays the fix.
    if tailscale --socket="$TS_SOCK" up --timeout="$TS_UP_TIMEOUT" $up_args >>"$LOG" 2>&1; then
      log "up with saved node state"
      return 0
    fi
    log "saved node state would not come up; falling back to the credential"
  fi

  if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
    # --force-reauth, because the interesting case is a node whose IDENTITY is gone: Tailscale
    # removed this sandbox's ephemeral node while it was paused, so tailscaled still holds a node
    # key the control plane no longer knows. A plain `up --authkey` can decide it is already
    # configured and do nothing at all with the credential; forcing reauth makes it actually go and
    # register again. Harmless on a first boot, where there is no identity to replace.
    #
    # Where THIS attempt's output starts in the log, so the classification below reads only what
    # just happened. The log accumulates for the whole session (the watchdog re-runs this script
    # every 30s), and several of the phrases matched below appear in messages this script itself
    # writes — so a line-count tail would eventually match its own previous verdict and get stuck on
    # it forever. Byte offset is exact and has no such feedback loop.
    local mark
    mark=$(wc -c <"$LOG" 2>/dev/null || echo 0)

    # shellcheck disable=SC2086
    if ts_up_retry --authkey="$TAILSCALE_AUTHKEY" --force-reauth $up_args; then
      log "up with a key minted for this sandbox"
      return 0
    fi

    local recent
    recent=$(tail -c "+$((mark + 1))" "$LOG" 2>/dev/null)
    if printf '%s' "$recent" | grep -q "fetch control key"; then
      # Nothing local can fix this and re-registering would not help, so don't burn the reset on it.
      log "cannot reach controlplane.tailscale.com from this sandbox (egress blocked?);" \
          "the credential was never presented — continuing without the tailnet"
      return 1
    fi
    if printf '%s' "$recent" | grep -qE "oauth2:|invalid_client|enough permissions|requested tags"; then
      # A rejected credential is a configuration problem — wrong secret, or an OAuth client that
      # doesn't own TAILSCALE_TAG (the API says "does not have enough permissions to use tag"). The
      # same rejected secret would fail a fresh registration identically, so say what to look at
      # instead of burning the reset below.
      log "the tailnet credential was rejected; check TAILSCALE_OAUTH_CLIENT_SECRET and that its" \
          "OAuth client owns ${TAILSCALE_TAG:-the tag} — see $LOG. Continuing without the tailnet"
      return 1
    fi

    # Everything else: the daemon has a saved identity it cannot make work. Throw it away and try
    # once more from nothing. This is the path the old code had no answer for.
    ts_reset_identity
    # shellcheck disable=SC2086
    if ts_up_retry --authkey="$TAILSCALE_AUTHKEY" $up_args; then
      log "up as a brand-new node after discarding saved state"
      return 0
    fi
    log "could not join the tailnet even from a clean state — see $LOG." \
        "Continuing without the tailnet"
    return 1
  fi

  log "no identity and no credential; continuing without the tailnet"
  return 1
}

main || log "unexpected error; continuing without the tailnet"
exit 0
