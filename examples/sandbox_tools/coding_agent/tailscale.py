# ABOUTME: The SandboxConfig BACKEND PROVIDER for this example — an async callable that mints a
# fresh, reusable, ephemeral, tagged Tailscale auth key from the Tailscale API and hands back an
# E2B config carrying it as TAILSCALE_AUTHKEY. Registered on the worker by name
# (`sandbox_activities({PROVIDER_NAME: e2b_with_tailscale})`, see worker.py); named by the agent
# as its `SandboxConfig.backend` (see tools.py).
#
# Why a provider rather than a literal config: an auth key is minted per sandbox by an HTTP call, so
# no literal in tools.py could express it. That is exactly the case SandboxConfig.backend's provider
# form exists for — the callable runs inside the `sandbox_activate` activity (worker-side, off the
# workflow thread), and its result is recorded in activity history so it is never re-run on replay.
#
# WORKER-SIDE ONLY. Deliberately NOT imported by tools.py: remote-box re-imports tools.py inside the
# sandbox on every tool call, and the sandbox has no business importing the module that holds the
# tailnet credential path. tools.py owns the template's identity (SANDBOX_BACKEND); this module only
# copies it and adds env_vars.
#
# The key is ephemeral + tagged + short-lived by design, so a leaked key is worth little: every node
# it joins is auto-removed from the tailnet when it disconnects, and the key expires on its own
# (TAILSCALE_KEY_EXPIRY_SECONDS, default one day) whether used or not. Its blast radius inside the
# tailnet is whatever your ACLs grant TAILSCALE_TAG — that tag is the actual security boundary, so
# write the ACL for it before turning this on.
#
# It is deliberately REUSABLE, which is the one property here that costs you something. It buys the
# sandbox the ability to rejoin the tailnet after Tailscale garbage-collects its ephemeral node during
# a long pause — without it the tailnet silently disappears mid-session and never comes back. See
# mint_ephemeral_authkey for the full argument, and TAILSCALE_KEY_REUSABLE=0 to opt out.
#
# That matters HERE specifically, not just in the sandbox: the provider's return value is recorded in
# Temporal activity history (that's what makes it replay-safe) and re-supplied to every later sandbox
# activity, so the key lands in your Temporal namespace's history and in the sandbox's environment,
# where the agent's own `bash` tool can read it. Keep the expiry short and the ACL tight; those, not
# single-use, are what bound a key at rest.

import logging
import os
import re

import aiohttp
from remote import E2B

from .tools import SANDBOX, SANDBOX_BACKEND

logger = logging.getLogger(__name__)

# The name the worker registers this provider under and the agent names as its backend. The two
# halves are wired by name because a live async callable can be neither constructed in workflow code
# nor serialized into an activity input.
PROVIDER_NAME = "e2b-tailscale"

# tools.py names the provider with a LITERAL (it must not import this module — see the header), so
# nothing but this check keeps the two spellings together. A mismatch would otherwise surface only at
# runtime, as a non-retryable activation failure on the first tool call of a real session.
assert SANDBOX.backend == PROVIDER_NAME, (
    f"tools.SANDBOX.backend ({SANDBOX.backend!r}) must equal PROVIDER_NAME ({PROVIDER_NAME!r})"
)

# The env var the template's tailscale_up.sh reads to run `tailscale up --authkey=...`. It reaches the
# sandbox through E2B's create-time `envs`, and is seen by post_create_cmd (fired after creation) —
# NOT by a template start command, which runs at template BUILD time and cannot see it.
AUTHKEY_ENV_VAR = "TAILSCALE_AUTHKEY"

_KEYS_URL = "https://api.tailscale.com/api/v2/tailnet/{tailnet}/keys"

# The label the minted key carries in the Tailscale admin console. See the payload below for why it
# is this plain: Tailscale rejects the request outright for punctuation or length, and the failure
# lands at sandbox-creation time — i.e. it breaks the agent, not just the label.
_DESCRIPTION = "sandboxed-coding-agent"

# Checked at import (so a bad edit fails the WORKER at startup) rather than at mint time (where it
# would fail a real user's first tool call, as a non-retryable sandbox-activation error). The
# character class is deliberately narrower than whatever Tailscale actually accepts — there's no
# upside to probing the boundary of an undocumented validator.
assert re.fullmatch(r"[A-Za-z0-9 ._-]{1,50}", _DESCRIPTION), (
    f"Tailscale rejects key descriptions over 50 chars or with punctuation: {_DESCRIPTION!r}"
)


def _tag() -> str:
    """The ACL tag every minted key is stamped with — the sandbox's identity in the tailnet.

    Bare names are accepted for convenience and normalized to Tailscale's `tag:` form.
    """
    tag = os.environ.get("TAILSCALE_TAG", "tag:agent-artifact").strip()
    return tag if tag.startswith("tag:") else f"tag:{tag}"


async def mint_ephemeral_authkey(session: aiohttp.ClientSession) -> str:
    """Mint one reusable, ephemeral, pre-authorized auth key tagged with :func:`_tag`.

    Each property is doing a job:
      * **ephemeral** — the node is removed from the tailnet automatically once it disconnects, so
        dead sandboxes (of which this deployment makes many) don't accumulate as stale machines.
      * **preauthorized** — the node is usable immediately, without a manual approve step in the
        admin console, which no one is present for when a workflow creates a sandbox.
      * **tags** — the node gets an identity your ACLs can grant/deny against. Nothing else about
        the sandbox is trusted by the tailnet.
      * **reusable** — the key can be REDEEMED AGAIN by the same sandbox, which is what lets it get
        back on the tailnet after a long idle. See below; this one was `false` and it was a bug.

    Why reusable, when single-use is obviously the safer setting: it is what makes the tailnet
    survive a paused session. The harness pauses the sandbox between turns, and Tailscale eventually
    reaps an EPHEMERAL node that has stopped talking to the control plane — measured on this tailnet
    by pausing a joined sandbox and watching the devices API: still listed at 10 minutes and at an
    hour, gone at ~74 minutes. A user who leaves the chat over lunch therefore comes back to a sandbox
    whose node no longer exists, and tailscaled can only get back in by logging in again — which needs
    a key that can still be redeemed. A single-use key was already spent at boot, so the sandbox was
    stuck OFF the tailnet for the rest of the session with no way to recover: the AI gateway and every
    other tailnet service simply stopped answering, mid-session, for no visible reason. The in-sandbox
    watchdog (tailscale_up.sh, re-run by boot.sh) redeems this key again and the node comes back.

    Note what does NOT cause the removal, since it looks like the obvious suspect: this key expiring.
    Verified with a deliberately 5-minute key — the node it registered was still on the tailnet 12
    minutes later, because a tagged node has key expiry disabled. The expiry below matters for the
    REJOIN (an expired key can't be redeemed again), not for the node's survival.

    What that costs, stated plainly: the key sits in the sandbox's own environment, where the agent's
    `bash` tool can read it, and it can now join MORE THAN ONE node to your tailnet rather than
    exactly one. Every node it joins is still stamped with :func:`_tag`, so it can reach nothing the
    sandbox itself couldn't already reach — the tag remains the entire security boundary, and a
    prompt-injected agent inside the sandbox never needed a second node to abuse it. It is also still
    ephemeral (those nodes remove themselves) and still expires on its own
    (``TAILSCALE_KEY_EXPIRY_SECONDS``). If that trade is wrong for your tailnet, set
    ``TAILSCALE_KEY_REUSABLE=0`` — sandboxes then join once and stay off the tailnet after a long
    idle, which is the old behavior, bug included.

    The caller's API token must itself be an owner of that tag (`tagOwners` in your ACL), or the
    API rejects the request — that error is surfaced as-is, since it needs an ACL edit to fix.
    """
    token = os.environ["TAILSCALE_API_KEY"]
    tailnet = os.environ.get("TAILSCALE_TAILNET", "-").strip() or "-"
    expiry = int(os.environ.get("TAILSCALE_KEY_EXPIRY_SECONDS", "86400"))
    # Empty counts as unset (a blank line in a .env file is not a decision), so only an explicit
    # off-value turns this off.
    reusable = (os.environ.get("TAILSCALE_KEY_REUSABLE", "").strip().lower() or "1") not in (
        "0",
        "false",
        "no",
    )

    payload = {
        "capabilities": {
            "devices": {
                "create": {
                    "reusable": reusable,
                    "ephemeral": True,
                    "preauthorized": True,
                    "tags": [_tag()],
                }
            }
        },
        # Bounds how long the key is worth stealing — and, because the key is redeemed AGAIN
        # whenever the sandbox has to rejoin (see above), it also bounds how long a session can
        # idle and still recover its tailnet. Default one day: long enough to cover a chat someone
        # comes back to tomorrow, short enough that a key recovered from a dead sandbox is not a
        # standing credential.
        "expirySeconds": expiry,
        # Shown against the key in the admin console. Tailscale validates this tightly: max 50
        # characters, and punctuation beyond `-`/`_`/`.` is rejected outright with
        # "keys: description had invalid characters" — parentheses and colons included. Keep it
        # boring; this is not the place for a sentence.
        "description": _DESCRIPTION,
    }

    async with session.post(
        _KEYS_URL.format(tailnet=tailnet),
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        body = await response.text()
        if response.status >= 400:
            # No key text can appear in a failure body, so this is safe to log/raise verbatim.
            raise RuntimeError(
                f"Tailscale key creation failed ({response.status}) for tailnet {tailnet!r} "
                f"tag {_tag()!r}: {body}"
            )
        key = (await response.json())["key"]

    if not isinstance(key, str) or not key:
        raise RuntimeError("Tailscale key creation returned no key")
    return key


async def e2b_with_tailscale() -> E2B:
    """The provider itself: the run's E2B config, with a freshly minted key in its env.

    Copied from :data:`~examples.sandbox_tools.coding_agent.tools.SANDBOX_BACKEND` rather than
    rebuilt, so the fields the template's IDENTITY is derived from (`template_prefix`,
    `dockerfile_path`, `start_cmd`) cannot drift from what was built ahead of time — drift there
    would fail activation's `require_prebuilt` check. `env_vars` are applied to the sandbox at
    creation, not baked into the template, which is what lets a per-run secret ride along at all.

    Tailscale is OPTIONAL: with no TAILSCALE_API_KEY set, this returns the plain config and the
    sandbox simply never joins a tailnet (tailscale_up.sh no-ops on the missing env var). That keeps
    this example runnable for anyone who doesn't use Tailscale.

    Safe to run more than once per sandbox, as the provider contract requires: `sandbox_activate` is
    an ordinary activity, so a retried attempt calls this again before any result was recorded. Each
    call mints a NEW key rather than consuming a shared one, and the abandoned key from the previous
    attempt was never redeemed, so it simply expires.
    """
    if not os.environ.get("TAILSCALE_API_KEY"):
        logger.info("TAILSCALE_API_KEY unset — sandbox will not join a tailnet")
        return SANDBOX_BACKEND

    async with aiohttp.ClientSession() as session:
        key = await mint_ephemeral_authkey(session)

    logger.info("minted ephemeral Tailscale auth key tagged %s for a new sandbox", _tag())
    # model_copy(update=...) rather than mutation: the module-level SANDBOX_BACKEND is shared, and a
    # per-run secret must never leak into the next run's config.
    return SANDBOX_BACKEND.model_copy(update={"env_vars": {AUTHKEY_ENV_VAR: key}})
