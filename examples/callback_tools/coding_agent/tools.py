"""Tools the callback coding agent uses to work on a project.

The six worker-facing tools are ``@agent.callback_tool_defn`` — tools with NO worker-side body. The
agent (picture it running in a cloud worker with no access to your disk) pauses in-workflow and
publishes a ``callback_requested`` event; the OpenCode shim on the user's laptop executes the
operation against the local project directory (via ``opencode_shim/local_tools.py``, which runs the
shared ``examples.coding_agent_common.tool_impls``) and returns the result. The agent never touches a
filesystem — it just calls these like any tool and reasons over the results.

``todowrite``/``todoread`` are shared inline tools (imported from ``coding_agent_common.todo_tools``)
— recording a plan is the agent's own state, so they run INLINE in the workflow.

The tool NAMES and parameter names are deliberately snake_case (idiomatic Python); the shim maps
them to OpenCode's canonical camelCase (``filePath``/``oldString``/``newString``) when it renders the
tool card. (The sandboxed coding agent declares the SAME tools as ``BaseModel``-in/out sandboxed
activity tools — different declaration shape, same underlying ``tool_impls``.)

NB: no ``from __future__ import annotations`` — parameter/return annotations are read directly to
build the model-facing tool schemas and the output-type validator, so they must be concrete types.
"""

from temporal_agent_harness.harness import agent

from examples.coding_agent_common.todo_tools import TodoItem, todoread, todowrite

__all__ = ["CODING_TOOLS", "TodoItem", "bash", "read", "write", "edit", "grep", "glob", "todowrite", "todoread"]


@agent.callback_tool_defn()
async def bash(command: str) -> str:
    """Run a shell command in the project directory and return its combined stdout+stderr. Use it
    to build, run tests, inspect the tree (`ls`, `cat`), use `git`, or anything else a shell can
    do. The command runs on the user's machine, in their project root. Prefer the dedicated
    `read`/`write`/`edit` tools for file edits so the user sees a clean diff. Every call is gated
    on the user's approval, so explain risky commands in your reply."""
    ...


# read/grep/glob are inherently_safe: they only READ the project, so `allow_inherently_safe()`
# auto-approves them (no permission prompt) and they run concurrently during the "orient" phase.
# The mutating tools (bash/write/edit) stay gated.
@agent.callback_tool_defn(inherently_safe=True)
async def read(file_path: str) -> str:
    """Read a UTF-8 text file from the project and return its full contents. `file_path` is
    relative to the project root, e.g. "src/main.py". Always read a file before editing it, so
    your `edit` matches the exact current text."""
    ...


@agent.callback_tool_defn()
async def write(file_path: str, content: str) -> str:
    """Create a new file, or OVERWRITE an existing one, with `content` (UTF-8), creating parent
    directories as needed. This replaces the WHOLE file — use `edit` for a surgical change to a
    large file. `file_path` is relative to the project root. Returns a short confirmation."""
    ...


@agent.callback_tool_defn()
async def edit(file_path: str, old_string: str, new_string: str) -> str:
    """Replace an exact substring in a file. `old_string` must occur EXACTLY ONCE in the file
    (include enough surrounding context to make it unique) and is replaced with `new_string`.
    `read` the file first so the match is exact. Returns a short confirmation. `file_path` is
    relative to the project root."""
    ...


@agent.callback_tool_defn(inherently_safe=True)
async def grep(pattern: str) -> str:
    """Search every text file in the project for a Python regular expression, returning matching
    lines as "path:lineno: line". Use it to locate a symbol, string, or definition before reading
    or editing. Results are capped."""
    ...


@agent.callback_tool_defn(inherently_safe=True)
async def glob(pattern: str) -> str:
    """List project files whose path matches a glob `pattern` (e.g. "**/*.py", "src/**/*.ts"), one
    per line, relative to the project root. Use it to discover files by name/extension before
    reading them. Results are capped."""
    ...


# The full toolset, in a stable order — handed to the model as its tool menu and used to build the
# name -> tool dispatch map in the workflow. All are callback tools EXCEPT `todowrite`/`todoread`,
# which run inline in the workflow (they edit/read agent state, not the user's disk).
CODING_TOOLS = [bash, read, write, edit, grep, glob, todowrite, todoread]
