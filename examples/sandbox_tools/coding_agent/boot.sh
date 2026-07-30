#!/usr/bin/env bash
#
# /usr/local/bin/boot.sh — the E2B sandbox's whole boot sequence, run once per sandbox as
# `post_create_cmd` (see tools.py's SANDBOX_BACKEND). Root, backgrounded, and — the reason it exists
# at all — able to see the sandbox's `env_vars`, which a template start command cannot (that runs at
# template BUILD time and is snapshotted into every sandbox).
#
# Two jobs, deliberately in separate scripts so each is usable alone:
#   1. tailscale_up.sh — join the tailnet with the per-sandbox key in TAILSCALE_AUTHKEY. Runs FIRST
#      and to completion, so a server started below can already reach tailnet services.
#   2. supervise.sh    — keep the agent's server running: wait for the project's start.sh, launch it,
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

if [ -x /usr/local/bin/tailscale-up.sh ]; then
  /usr/local/bin/tailscale-up.sh || log "tailscale-up.sh failed; continuing without the tailnet"
else
  log "tailscale-up.sh not present; skipping the tailnet"
fi

if [ -x /usr/local/bin/supervise.sh ]; then
  log "handing off to supervise.sh (START_FILE=$START_FILE)"
  exec /usr/local/bin/supervise.sh
fi

log "supervise.sh not present; nothing left to do"
