# ABOUTME: The SandboxConfig BACKEND PROVIDER for this example — an async callable that mints a
# fresh, single-use, ephemeral, tagged Tailscale auth key from the Tailscale API and hands back a
# Daytona config carrying it as TAILSCALE_AUTHKEY. Registered on the worker by name
# (`sandbox_activities({PROVIDER_NAME: daytona_with_tailscale})`, see worker.py); named by the agent
# as its `SandboxConfig.backend` (see tools.py).
#
# Why a provider rather than a literal config: an auth key is minted per sandbox by an HTTP call, so
# no literal in tools.py could express it. That is exactly the case SandboxConfig.backend's provider
# form exists for — the callable runs inside the `sandbox_activate` activity (worker-side, off the
# workflow thread), and its result is recorded in activity history so it is never re-run on replay.
#
# WORKER-SIDE ONLY. Deliberately NOT imported by tools.py: remote-box re-imports tools.py inside the
# sandbox on every tool call, and the sandbox has no business importing the module that holds the
# tailnet credential path. tools.py owns the snapshot's identity (SANDBOX_BACKEND); this module only
# copies it and adds env_vars.
#
# The key is single-use + ephemeral by design, so a leaked key is worth almost nothing: it can join
# ONE node, that node is auto-removed from the tailnet when it disconnects, and the key expires on
# its own (TAILSCALE_KEY_EXPIRY_SECONDS) whether used or not. Its blast radius inside the tailnet is
# whatever your ACLs grant TAILSCALE_TAG — that tag is the actual security boundary, so write the ACL
# for it before turning this on.
#
# Those properties are doing real work HERE specifically, not just in the sandbox: the provider's
# return value is recorded in Temporal activity history (that's what makes it replay-safe) and
# re-supplied to every later sandbox activity, so the key lands in your Temporal namespace's history
# and in the sandbox's environment, where the agent's own `bash` tool can read it. A key that is
# already redeemed, cannot be redeemed twice, and belongs to a node that removes itself is close to
# inert by the time it is at rest in either place. Do NOT swap in a long-lived reusable key without
# accepting that trade.

import logging
import os
import re

import aiohttp
from remote import Daytona

from .tools import SANDBOX, SANDBOX_BACKEND

logger = logging.getLogger(__name__)

# The name the worker registers this provider under and the agent names as its backend. The two
# halves are wired by name because a live async callable can be neither constructed in workflow code
# nor serialized into an activity input.
PROVIDER_NAME = "daytona-tailscale"

# tools.py names the provider with a LITERAL (it must not import this module — see the header), so
# nothing but this check keeps the two spellings together. A mismatch would otherwise surface only at
# runtime, as a non-retryable activation failure on the first tool call of a real session.
assert SANDBOX.backend == PROVIDER_NAME, (
    f"tools.SANDBOX.backend ({SANDBOX.backend!r}) must equal PROVIDER_NAME ({PROVIDER_NAME!r})"
)

# The env var the snapshot's supervise.sh reads on first boot to run `tailscale up --authkey=...`.
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
    """Mint one single-use, ephemeral, pre-authorized auth key tagged with :func:`_tag`.

    Each property is doing a job:
      * **ephemeral** — the node is removed from the tailnet automatically once it disconnects, so
        dead sandboxes (of which this deployment makes many) don't accumulate as stale machines.
      * **reusable: false** — the key joins exactly one node, so a key captured from the sandbox's
        own environment can't be replayed to join another.
      * **preauthorized** — the node is usable immediately, without a manual approve step in the
        admin console, which no one is present for when a workflow creates a sandbox.
      * **tags** — the node gets an identity your ACLs can grant/deny against. Nothing else about
        the sandbox is trusted by the tailnet.

    The caller's API token must itself be an owner of that tag (`tagOwners` in your ACL), or the
    API rejects the request — that error is surfaced as-is, since it needs an ACL edit to fix.
    """
    token = os.environ["TAILSCALE_API_KEY"]
    tailnet = os.environ.get("TAILSCALE_TAILNET", "-").strip() or "-"
    expiry = int(os.environ.get("TAILSCALE_KEY_EXPIRY_SECONDS", "3600"))

    payload = {
        "capabilities": {
            "devices": {
                "create": {
                    "reusable": False,
                    "ephemeral": True,
                    "preauthorized": True,
                    "tags": [_tag()],
                }
            }
        },
        # Bounds how long an UNUSED key is worth stealing. It does not bound the session: a key that
        # has already been redeemed keeps its node online past this.
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


async def daytona_with_tailscale() -> Daytona:
    """The provider itself: the run's Daytona config, with a freshly minted key in its env.

    Copied from :data:`~examples.sandbox_tools.coding_agent.tools.SANDBOX_BACKEND` rather than
    rebuilt, so the fields the snapshot's IDENTITY is derived from (`snapshot_name`,
    `dockerfile_path`, `sandbox_class`) cannot drift from what was built ahead of time — drift there
    would fail activation's `require_prebuilt` check. `env_vars` are applied to the sandbox at
    creation, not baked into the snapshot, which is what lets a per-run secret ride along at all.

    Tailscale is OPTIONAL: with no TAILSCALE_API_KEY set, this returns the plain config and the
    sandbox simply never joins a tailnet (supervise.sh no-ops on the missing env var). That keeps
    this example runnable for anyone who doesn't use Tailscale.

    Safe to run more than once per sandbox, as the provider contract requires: `sandbox_activate` is
    an ordinary activity, so a retried attempt calls this again before any result was recorded. Each
    call mints a NEW key rather than consuming a shared one, and the abandoned key from the previous
    attempt is single-use-but-unused, so it simply expires.
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
