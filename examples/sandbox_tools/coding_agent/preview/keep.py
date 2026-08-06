"""
KEEPING a preview: fork the sandbox so the site outlives the chat that built it.

The problem this solves is the first caveat in the example's README. A preview's
lifetime is the CHAT SESSION's lifetime — the harness deletes the sandbox when
the workflow ends — so the moment you close the session, the site you liked 404s
and the sandbox id it lived at is gone with it. Nothing in the gallery could save
one, because every sandbox in there is owned by a workflow that will eventually
tear it down.

Keeping breaks that ownership with `AsyncSandbox.fork`: E2B checkpoints the
running sandbox (memory included) and boots a SECOND sandbox from that snapshot.
The copy has a new id that no workflow knows, so nothing will ever delete it —
the agent, the worker and Temporal are not involved in any part of this, which is
the whole point. Everything below happens between a button in the gallery and the
E2B API.

WHY A FORK AND NOT A PAUSE. Pausing the original would preserve it just as well
until the workflow ended and killed it anyway. Forking is the only operation here
that produces a sandbox with no owner.

THE THREE THINGS THAT MAKE A COPY ACTUALLY PERSIST, in order:

  1. **It is a fork, so no workflow will delete it.** (above)
  2. **It is PARKED IMMEDIATELY.** A fork boots RUNNING, on a `timeout` after
     which E2B does not pause it but KILLS it. The proxy's idle timer would park
     it within AUTO_STOP_MINUTES — but if this process dies in between, nothing
     survives to do it and the kept sandbox is destroyed by that timeout, which
     is precisely the loss the feature exists to prevent. So we pause it here,
     while we still hold the request: from that instant it is disk+memory at
     rest, and only a request wakes it.
  3. **It is written to the registry.** No agent ever saw this sandbox's id, so
     no transcript contains it — registry.py is the only record that it exists,
     which is why kept records are never evicted.

WHAT THE USER PAYS FOR IT. A parked sandbox bills storage, not compute, and it
does so until someone deletes it. That is the trade the gallery states on the
button and in the confirm dialog, and it is why `destroy` exists next door: a
feature that can only ever create persistent things is a leak.

WHAT A FORK INHERITS, worth knowing because two of them surprise people:

  * **The running server.** The snapshot includes memory, so the agent's process
    is already serving inside the copy — there is no start.sh to re-run and no
    build to wait for. `supervise.sh` is in there too, still restarting it.
  * **The source's tailnet identity**, which two sandboxes cannot both hold. The
    control plane keeps whichever node re-registers, and the loser's watchdog
    (boot.sh) notices `Self.Online` go false and registers as a brand-new node
    within about a minute. So both boxes end up on the tailnet, but a kept copy
    that talks to the AI gateway may fail to for the first minute of its life.
  * **The source's env**, including TAILSCALE_AUTHKEY. A kept sandbox is
    therefore exactly as privileged as the sandbox it came from; keeping one is
    not a way to make an agent-built box safe to hand out.

AND WHAT IT COSTS THE SOURCE: the checkpoint briefly pauses it. E2B resumes it
itself and leaves its id and expiration untouched (so this does not disturb the
idle timer's `end_at` heartbeat), but a tool call in flight can still feel it —
hence the warning in the confirm dialog rather than a silent stall in the chat.
"""

import logging

from aiohttp import ClientSession
from e2b import AsyncSandbox

from .config import E2B_API_KEY, SANDBOX_TIMEOUT_SECONDS
from .proxy import Preview, cancel_idle_timer, ensure_ready, park, wait_for_server
from .registry import registry
from .screenshots import screenshotter

logger = logging.getLogger(__name__)


class KeepError(Exception):
    """A keep that failed early enough to leave nothing behind."""


async def fork_sandbox(sandbox_id: str, port: int, session: ClientSession) -> tuple:
    """Fork one sandbox. Returns (new sandbox id, how to reach `port` on it).

    The source is woken first, and not only because E2B cannot fork a paused
    sandbox: `ensure_ready` also proves the site is actually up, so a fork of a
    broken box fails here, before we have created anything to clean up. The COPY
    is not waited on — that belongs to the caller, which by then has somewhere to
    record it (see `keep`).
    """
    await ensure_ready(sandbox_id, port, session)

    forks = await AsyncSandbox.fork(
        sandbox_id,
        # Only covers the seconds until we park it below. It has to be generous
        # anyway — this is a kill, not a pause — and reusing the proxy's own
        # backstop keeps one number in play instead of two.
        timeout=SANDBOX_TIMEOUT_SECONDS,
        count=1,
        api_key=E2B_API_KEY,
    )
    # One entry per requested fork, and each is a sandbox OR the exception that
    # stopped it (a quota refusal arrives this way, not as a raise).
    fork = forks[0]
    if isinstance(fork, Exception):
        raise fork

    preview = Preview(
        url=f"https://{fork.get_host(port)}",
        token=fork.traffic_access_token or "",
    )
    return fork.sandbox_id, preview


async def keep(app: dict, session: ClientSession) -> tuple:
    """Fork `app`'s sandbox, register the copy, screenshot it, and park it.

    Returns (the new registry record, whether it is parked). Raises KeepError if
    the fork never happened; once it has, this always succeeds — a copy that
    exists must be recorded even if the screenshot or the pause afterwards fails,
    because an unrecorded sandbox is an invisible one nobody can reach or delete.

    `parked` is returned rather than stored on the record because it is a fact
    about this request, not about the app: the very next visitor resumes the
    sandbox and it stops being true. What the record keeps is `asleep_since`,
    which `park` writes only when the pause actually happened.
    """
    source_key, port = app["key"], app["port"]
    try:
        fork_id, preview = await fork_sandbox(app["sandbox_id"], port, session)
    except Exception as exc:
        raise KeepError(f"{type(exc).__name__}: {exc}") from exc

    logger.info("kept %s as %s-%s", source_key, fork_id, port)
    kept = await registry.register_kept(fork_id, port, app)

    # Screenshot BEFORE parking, since this is the one moment the copy is awake
    # for free. Best-effort: capture() never raises, and a kept app with no
    # thumbnail is still a kept app.
    try:
        await wait_for_server(session, preview.url, preview.token)
        await screenshotter.capture(kept["key"], preview.url, preview.token)
    except Exception as exc:
        logger.info("kept %s but could not screenshot it — %s: %s",
                    kept["key"], type(exc).__name__, exc)

    # The step that makes it durable (see the module docstring). Reported, not
    # raised: the copy exists either way, and if this failed the caller needs to
    # say so rather than imply the whole keep did.
    parked = True
    try:
        await park(fork_id)
    except Exception as exc:
        parked = False
        logger.warning("kept %s but could not park it — %s: %s",
                       kept["key"], type(exc).__name__, exc)
    # Belt and braces: nothing should have armed a timer for a sandbox we never
    # served, but a leftover one would pause an already-paused box and log noise.
    cancel_idle_timer(fork_id)
    return kept, parked


async def destroy(sandbox_id: str) -> None:
    """Delete a sandbox for good. Only ever called for kept ones.

    The counterweight to `keep`: a kept sandbox is owned by nobody, so nothing
    else will ever reclaim it. `kill` works on a paused sandbox, which is the
    state every kept one is in.
    """
    cancel_idle_timer(sandbox_id)
    await AsyncSandbox.kill(sandbox_id=sandbox_id, api_key=E2B_API_KEY)
