#!/usr/bin/env bash
#
# /usr/local/bin/boot.sh — the E2B sandbox's whole boot sequence, run once per sandbox as
# `post_create_cmd` (see tools.py's SANDBOX_BACKEND). Root, backgrounded, and — the reason it exists
# at all — able to see the sandbox's `env_vars`, which a template start command cannot (that runs at
# template BUILD time and is snapshotted into every sandbox).
#
# Three jobs, deliberately in separate scripts so each is usable alone:
#   1. tailscale_up.sh — join the tailnet with the per-sandbox key in TAILSCALE_AUTHKEY. Runs FIRST
#      and to completion, so a server started below can already reach tailnet services.
#   2. a watchdog      — re-run tailscale_up.sh forever, so the node REJOINS after a long pause has
#      cost it its place on the tailnet. Backgrounded. See the comment on it below.
#   3. supervise.sh    — keep the agent's server running: wait for the project's start.sh, launch it,
#      relaunch on crash. Runs in the FOREGROUND of this script, i.e. for the sandbox's lifetime.
#
# Why supervise.sh is still here on E2B, where pause() preserves memory and processes (so a server
# survives pause/resume without any relaunch, unlike Daytona where stop killed it): it's what gives
# the agent the `start.sh` contract its instructions are written against, plus restart-on-crash. The
# preview proxy needs neither — it only resumes the sandbox.
#
# Never exits nonzero in a way that matters: remote-box backgrounds this and only logs a launch
# failure, and neither job is worth losing a sandbox over.

set -u

LOG="${BOOT_LOG:-/var/log/boot.log}"

log() { printf '[boot %s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$LOG" >&2; }

# The project dir the agent works in, and where it writes start.sh. Must match tools.py's
# PROJECT_ROOT; supervise.sh reads these (its own defaults are the Daytona paths).
export START_FILE="${START_FILE:-/home/user/project/start.sh}"
export ENV_FILE="${ENV_FILE:-/home/user/project/.env}"
export SERVER_LOG="${SERVER_LOG:-/home/user/server.log}"

log "starting: tailnet, then server supervisor"

# How often the watchdog below re-checks the tailnet. 0 disables it (one join attempt at boot, the
# old behavior). Cheap: tailscale-up.sh returns immediately when the node is already Running.
TAILSCALE_WATCH_SECONDS="${TAILSCALE_WATCH_SECONDS:-30}"

if [ -x /usr/local/bin/tailscale-up.sh ]; then
  /usr/local/bin/tailscale-up.sh || log "tailscale-up.sh failed; continuing without the tailnet"

  # THE WATCHDOG. Joining once at boot is not enough, because this sandbox does not run
  # continuously: the harness PAUSES it between chat turns, and Tailscale removes an ephemeral node
  # some time after it stops talking to the control plane. Come back to the chat an hour later and
  # the sandbox resumes with a tailscaled holding a node key for a machine that no longer exists —
  # off the tailnet, and with nothing in the old boot sequence that would ever notice. The user sees
  # the AI gateway (and every other tailnet service) stop answering, mid-session, for no reason.
  #
  # So: re-run the join script forever. It no-ops while the node is Running, and re-redeems
  # TAILSCALE_AUTHKEY when it isn't — which is why that key is minted REUSABLE (see tailscale.py).
  # Backgrounded, output discarded (the script logs to its own file), and never able to fail this
  # script.
  #
  # Gated on there being a key to re-redeem: without one there is nothing the watchdog could ever
  # fix, and tailscale-up.sh would log "not joining a tailnet" every 30 seconds forever.
  if [ -n "${TAILSCALE_AUTHKEY:-}" ] && [ "$TAILSCALE_WATCH_SECONDS" -gt 0 ] 2>/dev/null; then
    log "tailnet watchdog every ${TAILSCALE_WATCH_SECONDS}s"
    (
      while true; do
        sleep "$TAILSCALE_WATCH_SECONDS"
        /usr/local/bin/tailscale-up.sh >/dev/null 2>&1 || true
      done
    ) &
  fi
else
  log "tailscale-up.sh not present; skipping the tailnet"
fi

if [ -x /usr/local/bin/supervise.sh ]; then
  log "handing off to supervise.sh (START_FILE=$START_FILE)"
  exec /usr/local/bin/supervise.sh
fi

log "supervise.sh not present; nothing left to do"
