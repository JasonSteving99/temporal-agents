#!/usr/bin/env bash
#
# /usr/local/bin/tailscale-up.sh — join the tailnet, once per E2B sandbox.
#
# Run as this example's E2B `post_create_cmd` (see tools.py's SANDBOX_BACKEND): fired once
# immediately after sandbox creation, as root, in the background, inheriting the sandbox's env_vars —
# which is where TAILSCALE_AUTHKEY arrives, minted per sandbox by the worker (tailscale.py).
#
# It CANNOT be the template's start command: that runs while the template builds and is snapshotted
# into every sandbox, so it starts before any sandbox env exists. post_create_cmd is the only hook
# that sees the credential. (The Daytona equivalent is supervise.sh, which also supervises the
# agent's server for the live-preview proxy; E2B owns sandbox lifetime and previews are Daytona-only
# for now, so this script is just the tailnet.)
#
# Never fatal: every failure is logged and the script exits 0. Losing the tailnet must not cost the
# sandbox — remote-box backgrounds this and logs a launch failure as a warning, and the agent's tools
# work regardless.
#
# Idempotent, as the hook contract requires: safe to run twice (a retried activity re-creates the
# sandbox, and a second run finds the daemon already up and returns).

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

  # Already up from an earlier run of this script? Then there is nothing to do.
  if [ "$(backend_state)" = "Running" ]; then
    log "already up"
    return 0
  fi

  # Any OTHER tailscaled must go before we start ours. A daemon can be inherited from the template
  # snapshot (E2B snapshots the build VM with its processes running, so anything that started
  # tailscaled during the build — notably the Debian package's postinst — reappears in every
  # sandbox). Such a daemon is unusable: its network died with the build VM, it reports "the network
  # is down" no matter what, and it holds the socket so ours exits with "address already in use".
  # The Dockerfile suppresses that start, but a stale socket alone is enough to break the bind, so
  # clear both here rather than trusting the image. Not reachable for a HEALTHY daemon — that case
  # returned above.
  if pkill -x tailscaled 2>/dev/null; then
    log "killed a pre-existing tailscaled (inherited from the template snapshot?)"
    sleep 1
  fi
  rm -f "$TS_SOCK" /run/tailscale/tailscaled.sock 2>/dev/null || true

  ensure_routable_addr

  local userspace=""
  if [ -c /dev/net/tun ]; then
    log "/dev/net/tun present; starting with a real TUN interface"
    tailscaled --state="$TS_STATE" --socket="$TS_SOCK" >>"$LOG" 2>&1 &
  else
    # Fallback for a sandbox class without a TUN device: tailscaled joins the tailnet as a normal
    # node, but with no kernel route, so traffic reaches the tailnet only through the proxies it
    # listens on. Published to $TS_ENV_FILE rather than exported, so the agent's ordinary internet
    # traffic (pip, npm, git) isn't silently pulled through the tailnet stack.
    userspace=1
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

  local up_args="--hostname=$TS_HOSTNAME"
  # MagicDNS points /etc/resolv.conf at 100.100.100.100, which nothing can route without a TUN
  # device — that would break ALL name resolution, not just tailnet names.
  [ -n "$userspace" ] && up_args="$up_args --accept-dns=false"

  # Which `up` to run comes from the DAEMON's state, never from whether the state file exists:
  # tailscaled writes that file (its machine key) as soon as it starts, before any login. A bare
  # `tailscale up` on a logged-out node starts an INTERACTIVE login, printing an auth URL and
  # blocking for a browser that is never coming. --timeout bounds every path regardless.
  local state
  state=$(backend_state)
  log "backend state is ${state:-unknown}"

  # shellcheck disable=SC2086  # $up_args is a flag list; word splitting is the point
  if [ "$state" = "Stopped" ]; then
    if tailscale --socket="$TS_SOCK" up --timeout=30s $up_args >>"$LOG" 2>&1; then
      log "up with saved node state"
    else
      log "saved node state would not come up; continuing without the tailnet"
      return 0
    fi
  elif [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
    if tailscale --socket="$TS_SOCK" up --authkey="$TAILSCALE_AUTHKEY" --timeout=30s $up_args \
      >>"$LOG" 2>&1; then
      log "up with the auth key minted for this sandbox"
    elif grep -q "fetch control key" "$LOG" 2>/dev/null; then
      log "cannot reach controlplane.tailscale.com from this sandbox (egress blocked?);" \
          "the auth key was never presented — continuing without the tailnet"
      return 0
    else
      log "login failed; the key may be expired or already redeemed (it is single-use)" \
          "— see $LOG. Continuing without the tailnet"
      return 0
    fi
  else
    log "no identity and no auth key; continuing without the tailnet"
    return 0
  fi

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

main || log "unexpected error; continuing without the tailnet"
exit 0
