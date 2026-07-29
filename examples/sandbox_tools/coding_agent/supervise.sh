#!/usr/bin/env bash
#
# /usr/local/bin/supervise.sh
# Snapshot ENTRYPOINT for Daytona container sandboxes.
#
# Runs on EVERY sandbox boot — create, start, and start-from-archive — because
# it's the container's long-running entrypoint. It keeps the container alive and
# (re)launches your server, so when the proxy calls sandbox.start() the process
# comes back automatically. No agent action at runtime.
#
# Also joins the tailnet on every boot, when TAILSCALE_AUTHKEY was injected into the sandbox at
# creation (the worker's backend provider mints it per sandbox — see tailscale.py). Skipped entirely
# when neither that var nor saved node state is present, so this stays a no-op for anyone not using
# Tailscale.
#
# Contract:
#   * The agent writes ONE launch command into $START_FILE.
#   * Optional env/secrets live in $ENV_FILE and are sourced before each launch.
#   * Both live INSIDE the project dir (/home/daytona/project) so the agent can create them with its
#     own `write` tool, which confines writes to the project root (must match tools.py's PROJECT_ROOT).
#   * All of this lives on the PERSISTENT filesystem, so it survives
#     stop -> start and archive -> start cycles unchanged.

set -u -o pipefail
# `set -m` puts each launched job in its own process group, so on shutdown we
# can signal the whole tree (server + anything it spawned), not just the shell.
set -m

# START_FILE/ENV_FILE live INSIDE the project dir so the agent can create them with its `write` tool
# (writes are confined to the project root). LOG stays OUTSIDE the project so the server's log doesn't
# pollute the agent's grep/glob/git of its own project (it can still `bash cat` it if it needs to).
START_FILE="${START_FILE:-/home/daytona/project/start.sh}"
ENV_FILE="${ENV_FILE:-/home/daytona/project/.env}"
LOG="${SERVER_LOG:-/home/daytona/server.log}"
MIN_BACKOFF=1
MAX_BACKOFF=30
HEALTHY_SECONDS=15   # server stayed up at least this long => reset backoff

log() {
  printf '[supervise %s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$LOG" >&2
}

child_pgid=""

# As PID 1 we get NO default signal handlers. Without this trap, sandbox.stop()
# hangs until Daytona SIGKILLs us — slowing every stop/start the proxy drives.
# Forward the signal to the child's whole process group for a fast, clean stop.
shutdown() {
  log "shutdown signal received"
  if [ -n "$child_pgid" ]; then
    kill -TERM "-$child_pgid" 2>/dev/null || true
    for _ in $(seq 1 20); do            # up to ~5s grace
      kill -0 "-$child_pgid" 2>/dev/null || break
      sleep 0.25
    done
    kill -KILL "-$child_pgid" 2>/dev/null || true
  fi
  exit 0
}
trap shutdown TERM INT

# ------------------------------------------------------------------------------------------------
# Tailnet
# ------------------------------------------------------------------------------------------------
# Runs on EVERY boot, before the wait for $START_FILE, so the tailnet is up before any server the
# agent wrote starts — and so a fresh sandbox is reachable/able to reach the tailnet even while it
# still has no project. Never fatal: every failure here is logged and the supervisor carries on
# running the agent's server, because losing the tailnet is not a reason to lose the sandbox.
TS_STATE="${TS_STATE:-/var/lib/tailscale/tailscaled.state}"
TS_SOCK="${TS_SOCK:-/var/run/tailscale/tailscaled.sock}"
# Where the SOCKS5 + HTTP proxies listen in userspace mode (see below). Written to $TS_ENV_FILE so
# the agent can point a command at the tailnet with `bash source /home/daytona/tailscale.env && ...`.
TS_PROXY_ADDR="${TS_PROXY_ADDR:-localhost:1055}"
TS_ENV_FILE="${TS_ENV_FILE:-/home/daytona/tailscale.env}"
TS_HOSTNAME="${TAILSCALE_HOSTNAME:-agent-$(hostname)}"

# The daemon's own view of whether it has an identity: NeedsLogin / Stopped / Running / Starting,
# or empty if the daemon isn't answering yet. `tailscale status` (non-JSON) can't be used for this —
# it exits 1 both for "no daemon at all" and for the perfectly-healthy "Logged out." that a first
# boot reports, which are opposite situations. Parsed with grep/sed rather than a JSON tool because
# this image is not guaranteed to have one, and the field is a flat string.
backend_state() {
  tailscale --socket="$TS_SOCK" status --json 2>/dev/null \
    | grep -o '"BackendState"[[:space:]]*:[[:space:]]*"[^"]*"' \
    | head -1 \
    | sed 's/.*"\([^"]*\)"$/\1/'
}

start_tailscale() {
  if ! command -v tailscaled >/dev/null 2>&1; then
    log "tailscale: not installed; skipping"
    return
  fi
  # Nothing to do on a sandbox created without a key AND without prior state.
  if [ -z "${TAILSCALE_AUTHKEY:-}" ] && [ ! -s "$TS_STATE" ]; then
    log "tailscale: no TAILSCALE_AUTHKEY and no saved state; not joining a tailnet"
    return
  fi

  mkdir -p "$(dirname "$TS_STATE")" "$(dirname "$TS_SOCK")" 2>/dev/null || true

  # A container sandbox almost never has /dev/net/tun, and tailscaled needs it for a real interface.
  # Without it, run in USERSPACE networking: tailscaled joins the tainet as a normal node, but no
  # kernel route exists, so traffic reaches the tailnet only through the SOCKS5/HTTP proxies it
  # listens on. That is the documented pattern for unprivileged containers, and the reason the
  # proxy address is published to $TS_ENV_FILE instead of being exported globally — exporting
  # *_PROXY for everything would silently pull the agent's ordinary internet traffic (pip, npm,
  # git) through the tailnet stack too.
  local up_args="--hostname=$TS_HOSTNAME"
  local userspace=""
  if [ -c /dev/net/tun ]; then
    log "tailscale: /dev/net/tun present; starting with a real TUN interface"
    tailscaled --state="$TS_STATE" --socket="$TS_SOCK" >>"$LOG" 2>&1 &
  else
    userspace=1
    log "tailscale: no /dev/net/tun; starting in userspace mode (proxies on $TS_PROXY_ADDR)"
    tailscaled --state="$TS_STATE" --socket="$TS_SOCK" \
      --tun=userspace-networking \
      --socks5-server="$TS_PROXY_ADDR" \
      --outbound-http-proxy-listen="$TS_PROXY_ADDR" >>"$LOG" 2>&1 &
    # MagicDNS would rewrite /etc/resolv.conf to point at 100.100.100.100, which nothing in this
    # container can route to without a TUN device — that breaks ALL name resolution, not just
    # tailnet names. Resolve tailnet names through the proxy (socks5h/the HTTP proxy) instead.
    up_args="$up_args --accept-dns=false"
  fi

  # tailscaled needs a moment before its socket accepts commands; `tailscale up` against a
  # not-yet-listening socket fails immediately rather than waiting for it. The socket file appearing
  # is not enough (it exists before the daemon serves on it), so wait until it actually ANSWERS —
  # which is the same call the login decision below is made from.
  local ready=""
  for _ in $(seq 1 30); do
    if [ -n "$(backend_state)" ]; then ready=1; break; fi
    sleep 0.5
  done
  [ -n "$ready" ] || log "tailscale: daemon did not answer within 15s; trying anyway"

  # Which `up` to run is decided by the DAEMON's login state, never by whether the state file
  # exists: tailscaled writes that file (its machine key) as soon as it starts, before any login,
  # so "file is non-empty" is true on a first boot and says nothing about having an identity.
  # Getting this wrong is not a soft failure — a plain `tailscale up` on a logged-out node starts an
  # INTERACTIVE login, printing an auth URL and blocking for a browser that is never coming, which
  # hangs this supervisor before it ever launches the agent's server.
  #
  #   NeedsLogin  no identity (first boot, or the node was reaped) -> the auth key is the only way in
  #   Stopped     has an identity, tailnet is just down            -> plain `up`, no key needed
  #   Running     already up (a re-exec of this script)            -> nothing to do
  local state
  state=$(backend_state)
  log "tailscale: backend state is ${state:-unknown}"

  # --timeout bounds every path, so no failure mode here can block the supervisor. $up_args is
  # deliberately unquoted: it is a list of flags, not one argument.
  # shellcheck disable=SC2086
  if [ "$state" = "Running" ]; then
    log "tailscale: already up"
  elif [ "$state" = "Stopped" ]; then
    if tailscale --socket="$TS_SOCK" up --timeout=30s $up_args >>"$LOG" 2>&1; then
      log "tailscale: up with saved node state"
    else
      log "tailscale: saved node state would not come up; continuing without the tailnet"
      return
    fi
  elif [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
    if tailscale --socket="$TS_SOCK" up --authkey="$TAILSCALE_AUTHKEY" --timeout=30s $up_args \
      >>"$LOG" 2>&1; then
      log "tailscale: up with the auth key minted for this sandbox"
    else
      # The likely cause on a RESTARTED sandbox: the key is single-use and was already redeemed, and
      # the node was ephemeral, so the tailnet reaped it once it went offline — no identity left and
      # no second key to redeem. Mint a reusable key (see tailscale.py) if sandboxes need to rejoin
      # after long stops. See $LOG for what tailscale actually said.
      log "tailscale: auth key was refused; continuing without the tailnet"
      return
    fi
  else
    log "tailscale: no identity and no auth key; continuing without the tailnet"
    return
  fi

  # Userspace mode only: publish the proxy address for the agent/its server to opt into, rather than
  # forcing it on them. In TUN mode there are no proxies and none are needed — the tailnet is just
  # routable — so no file is written and its absence is the signal that nothing special is required.
  if [ -n "$userspace" ]; then
    {
      echo "# Point ONE command at the tailnet:  set -a; . $TS_ENV_FILE; set +a; curl http://host"
      echo "export ALL_PROXY=socks5h://$TS_PROXY_ADDR"
      echo "export HTTP_PROXY=http://$TS_PROXY_ADDR"
      echo "export HTTPS_PROXY=http://$TS_PROXY_ADDR"
    } >"$TS_ENV_FILE" 2>/dev/null || true
  fi
  tailscale --socket="$TS_SOCK" status >>"$LOG" 2>&1 || true
}

start_tailscale

# A fresh sandbox may boot before the agent has written the launch command.
log "supervisor started; waiting for $START_FILE"
while [ ! -s "$START_FILE" ]; do sleep 1; done
log "found launch command"

backoff=$MIN_BACKOFF
while true; do
  # Load env/secrets fresh on each launch (survives restarts, lives on disk).
  if [ -f "$ENV_FILE" ]; then
    set -a; . "$ENV_FILE"; set +a
  fi

  started_at=$(date +%s)
  log "launching server"
  bash "$START_FILE" >>"$LOG" 2>&1 &
  child_pid=$!
  child_pgid=$child_pid          # with `set -m`, the job's pgid == its pid

  wait "$child_pid"
  code=$?
  child_pgid=""
  ran=$(( $(date +%s) - started_at ))
  log "server exited (code=$code) after ${ran}s"

  # Reset backoff if it ran healthily; otherwise back off to avoid a hot loop.
  if [ "$ran" -ge "$HEALTHY_SECONDS" ]; then
    backoff=$MIN_BACKOFF
  else
    backoff=$(( backoff * 2 ))
    [ "$backoff" -gt "$MAX_BACKOFF" ] && backoff=$MAX_BACKOFF
  fi

  log "restarting in ${backoff}s"
  sleep "$backoff"
done
