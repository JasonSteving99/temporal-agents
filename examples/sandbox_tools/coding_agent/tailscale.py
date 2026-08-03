# ABOUTME: The SandboxConfig BACKEND PROVIDER for this example — an async callable that hands back an
# E2B config carrying a Tailscale OAuth client secret as TAILSCALE_AUTHKEY, plus the ACL tag the node
# must advertise. Registered on the worker by name
# (`sandbox_activities({PROVIDER_NAME: e2b_with_tailscale})`, see worker.py); named by the agent
# as its `SandboxConfig.backend` (see tools.py).
#
# THE SANDBOX MINTS ITS OWN KEYS. What lands in TAILSCALE_AUTHKEY is not an auth key at all — it is an
# OAuth client secret (`tskey-client-...`) with `?preauthorized=true&ephemeral=true` appended. The
# tailscale CLI recognizes that prefix and, on every `tailscale up`, does the OAuth2
# client-credentials exchange against api.tailscale.com and mints itself a fresh single-use,
# ephemeral, pre-authorized, tagged auth key before logging in. (See tailscale's
# feature/oauthkey/oauthkey.go; this is exactly what `tailscale/github-action` puts on a CI runner.)
#
# Why that shape, and not the obvious "mint a key here and pass it down": the sandbox is PAUSED
# between chat turns, and Tailscale removes an ephemeral node that has been silent long enough
# (measured on this tailnet: still listed at an hour, gone at ~74 minutes). A session picked up after
# lunch therefore has to log in AGAIN, and a pre-minted key can only support that if it is reusable
# and has not yet expired — which is why this file used to mint reusable keys with a tunable expiry,
# and why a long enough idle could still strand a sandbox off the tailnet permanently. Carrying the
# minting capability instead removes both limits: the sandbox can re-register as many times as it
# needs, days later, and each registration burns a brand-new single-use key.
#
# WHAT THAT COSTS, stated plainly, because it is a real regression and not a free win: the credential
# now sitting in the sandbox's environment — readable by the agent's own `bash` tool, and recorded in
# Temporal activity history, since a provider's return value is what makes it replay-safe — is
# LONG-LIVED. OAuth client secrets do not expire, so `TAILSCALE_KEY_EXPIRY_SECONDS` no longer bounds
# the theft window; rotating the client in the admin console is the only revocation. What it does NOT
# do is widen reach: the CLI can only mint keys for tags the OAuth client owns, every node it joins is
# stamped with that tag, and the tag is what your ACLs grant against. So a stolen secret buys an
# attacker exactly what the sandbox already had, indefinitely.
#
# Give the OAuth client the `auth_keys` scope on ONE tag and nothing else
# (https://login.tailscale.com/admin/settings/oauth), write the ACL for that tag before turning this
# on, and treat rotation as a scheduled chore.
#
# WORKER-SIDE ONLY. Deliberately NOT imported by tools.py: remote-box re-imports tools.py inside the
# sandbox on every tool call, and the sandbox has no business importing the module that holds the
# tailnet credential path. tools.py owns the template's identity (SANDBOX_BACKEND); this module only
# copies it and adds env_vars.

import logging
import os
import re

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

# The env vars the template's tailscale_up.sh reads. They reach the sandbox through E2B's create-time
# `envs`, and are seen by post_create_cmd (fired after creation) — NOT by a template start command,
# which runs at template BUILD time and cannot see a per-sandbox env var.
#
# AUTHKEY_ENV_VAR keeps its name because its value is passed verbatim to `tailscale up --authkey=`,
# which is what the CLI's OAuth path expects; it no longer holds an auth key. TAG_ENV_VAR is new and
# is REQUIRED by that path — `tailscale up` refuses an OAuth secret without `--advertise-tags`, since
# there is otherwise nothing to stamp the minted key with.
AUTHKEY_ENV_VAR = "TAILSCALE_AUTHKEY"
TAG_ENV_VAR = "TAILSCALE_TAG"

# The env var holding the OAuth client secret on the WORKER. Its presence is what turns the tailnet on.
CLIENT_SECRET_ENV_VAR = "TAILSCALE_OAUTH_CLIENT_SECRET"

# Appended to the secret before it is handed to `--authkey`. The CLI parses these off the value and
# they become the capabilities of the key it mints:
#   * preauthorized — the node is usable the moment it registers, with no manual approve step in the
#     admin console, which nobody is present for when a workflow creates a sandbox. Defaults to FALSE
#     in the CLI, so this one has to be said.
#   * ephemeral — the node removes itself from the tailnet once it disconnects, so dead sandboxes (of
#     which this deployment makes many) don't accumulate as stale machines. Already the CLI's default;
#     spelled out because it is load-bearing, and because this is the exact string the GitHub Action
#     uses and matching it makes the two easy to compare.
# The minted key is always single-use (`reusable: false`) and that is not configurable — nor does it
# need to be any more, which is the whole point of this pivot.
_KEY_ATTRIBUTES = "?preauthorized=true&ephemeral=true"

# What an OAuth client secret looks like. Checked because the failure mode of passing something else
# is quiet and confusing: the CLI would treat a non-matching value as a literal auth key, try to
# redeem it, and report a generic login failure with no hint that the credential was the wrong KIND.
_CLIENT_SECRET_PREFIX = "tskey-client-"


def _tag() -> str:
    """The ACL tag every node advertises — the sandbox's identity in the tailnet.

    Bare names are accepted for convenience and normalized to Tailscale's `tag:` form.

    This must be a tag the OAuth client is scoped to own, or `tailscale up` fails at the mint step
    with an API error. It is also the entire security boundary: the sandbox runs code an LLM wrote,
    so whatever your ACLs grant this tag is what a prompt-injected agent can reach.
    """
    tag = os.environ.get(TAG_ENV_VAR, "tag:agent-artifact").strip()
    return tag if tag.startswith("tag:") else f"tag:{tag}"


# Checked at import (so a bad edit or a typo'd .env fails the WORKER at startup) rather than at
# sandbox-creation time, where it would surface as a non-retryable activation error on a real user's
# first tool call.
assert re.fullmatch(r"tag:[A-Za-z0-9][A-Za-z0-9-]*", _tag()), (
    f"{TAG_ENV_VAR} must be a valid Tailscale ACL tag, got {_tag()!r}"
)


def oauth_authkey() -> str:
    """The value for `tailscale up --authkey=`: the OAuth client secret plus key attributes.

    Raises if the secret is not shaped like one, since the CLI's fallback for an unrecognized value
    is to treat it as a literal auth key and fail with an unrelated-looking error.
    """
    secret = os.environ[CLIENT_SECRET_ENV_VAR].strip()
    if not secret.startswith(_CLIENT_SECRET_PREFIX):
        raise RuntimeError(
            f"{CLIENT_SECRET_ENV_VAR} must be a Tailscale OAuth client secret starting with "
            f"{_CLIENT_SECRET_PREFIX!r} (from https://login.tailscale.com/admin/settings/oauth). "
            "An API access token or a plain auth key will not work here."
        )
    # Guard against someone pasting a secret that already carries attributes: the CLI rejects the
    # whole value on a duplicate or unknown query key, and the resulting error names the attribute
    # rather than the .env line that caused it.
    if "?" in secret:
        raise RuntimeError(
            f"{CLIENT_SECRET_ENV_VAR} must be the bare secret with no '?...' attributes; "
            f"this module appends {_KEY_ATTRIBUTES!r} itself."
        )
    return secret + _KEY_ATTRIBUTES


async def e2b_with_tailscale() -> E2B:
    """The provider itself: the run's E2B config, with the tailnet credential in its env.

    Copied from :data:`~examples.sandbox_tools.coding_agent.tools.SANDBOX_BACKEND` rather than
    rebuilt, so the fields the template's IDENTITY is derived from (`template_prefix`,
    `dockerfile_path`, `start_cmd`) cannot drift from what was built ahead of time — drift there
    would fail activation's `require_prebuilt` check. `env_vars` are applied to the sandbox at
    creation, not baked into the template, which is what lets a per-run secret ride along at all.

    Tailscale is OPTIONAL: with no ``TAILSCALE_OAUTH_CLIENT_SECRET`` set, this returns the plain
    config and the sandbox simply never joins a tailnet (tailscale_up.sh no-ops on the missing env
    var). That keeps this example runnable for anyone who doesn't use Tailscale.

    Safe to run more than once per sandbox, as the provider contract requires: `sandbox_activate` is
    an ordinary activity, so a retried attempt calls this again before any result was recorded. This
    is now trivially true — the function is pure, and nothing is consumed by calling it. (It used to
    mint a real key per attempt and leak the abandoned one until expiry.)
    """
    if not os.environ.get(CLIENT_SECRET_ENV_VAR):
        logger.info("%s unset — sandbox will not join a tailnet", CLIENT_SECRET_ENV_VAR)
        return SANDBOX_BACKEND

    logger.info("sandbox will self-register on the tailnet as %s", _tag())
    # model_copy(update=...) rather than mutation: the module-level SANDBOX_BACKEND is shared, and a
    # secret must never leak into a config that was meant to go out without one.
    return SANDBOX_BACKEND.model_copy(
        update={"env_vars": {AUTHKEY_ENV_VAR: oauth_authkey(), TAG_ENV_VAR: _tag()}}
    )
