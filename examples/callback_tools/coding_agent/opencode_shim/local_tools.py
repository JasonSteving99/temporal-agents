"""Local executor for the callback tools — runs the shared tool impls on THIS machine.

The coding agent (a durable Temporal workflow) has no disk; when it calls ``bash``/``read``/
``write``/``edit``/``grep``/``glob`` it publishes a ``callback_requested`` event and parks. The shim,
running on the user's laptop, calls :func:`execute` here to run the operation against the project
directory and posts the result back so the agent resumes.

The actual filesystem/shell work lives in the SHARED ``examples.coding_agent_common.tool_impls`` —
the same implementations the sandboxed coding agent (``examples/sandbox_tools/coding_agent``) runs
inside a Daytona box. This module only adds the OpenCode-specific bits: the ``execute`` dispatcher
and the per-tool ``metadata`` each OpenCode tool CARD renders its result from. ``unified_diff`` and
``git_file_diffs`` are re-exported for the shim's permission-diff + diff-viewer use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from examples.coding_agent_common.tool_impls import (
    bash_exec,
    edit_file,
    git_file_diffs,
    glob_files,
    grep_files,
    read_file,
    unified_diff,
    write_file,
)

__all__ = ["execute", "unified_diff", "git_file_diffs"]


async def execute(
    root: Path, tool_name: str, tool_input: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Run one callback tool locally.

    Returns ``(output, metadata, error)``: on success ``(output_str, metadata, None)``; on failure
    ``(None, None, error_message)`` — the error is sent back to the agent as the tool's error result
    (the turn continues; the model sees it). ``output`` is the result the model reads; ``metadata``
    carries the extra keys each OpenCode tool CARD renders its result from (they differ per tool):
    bash -> ``output``/``exit``, edit -> ``diff``, write -> ``diagnostics`` (presence enables the
    content preview), grep -> ``matches``, glob -> ``count``.
    """
    try:
        if tool_name == "bash":
            output, exit_code = await bash_exec(root, tool_input["command"])
            return output, {"output": output, "exit": exit_code}, None
        if tool_name == "read":
            return read_file(root, tool_input["file_path"]), {}, None
        if tool_name == "write":
            # `diagnostics` present (even empty) makes OpenCode's Write card show input.content.
            return write_file(root, tool_input["file_path"], tool_input["content"]), {"diagnostics": []}, None
        if tool_name == "edit":
            msg, diff = edit_file(
                root, tool_input["file_path"], tool_input["old_string"], tool_input["new_string"]
            )
            return msg, {"diff": diff}, None
        if tool_name == "grep":
            out, count = grep_files(root, tool_input["pattern"])
            return out, {"matches": count}, None
        if tool_name == "glob":
            out, count = glob_files(root, tool_input["pattern"])
            return out, {"count": count}, None
        return None, None, f"this client does not implement the tool {tool_name!r}"
    except Exception as e:  # noqa: BLE001 — any local failure becomes a tool error result
        return None, None, f"{type(e).__name__}: {e}"
