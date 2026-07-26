"""The shared Gemini Interactions tool-calling loop for both coding agents.

Both the callback and sandboxed coding-agent workflows drive the model the same way: stream one
``interactions.create``, reduce it into (reply text, function calls, interaction id), dispatch any
calls, feed the results back, and repeat until the model replies with no further calls. That whole
loop lives here so the two workflows only differ in what genuinely differs (which tools, how each is
dispatched, the approval policy, whether tools run in a sandbox).

IMPORTANT — this module imports ONLY ``google.genai`` + ``temporalio``, never
``temporal_agent_harness``. That's deliberate: the workflows import it inside their
``with workflow.unsafe.imports_passed_through():`` block, and keeping every harness import out of
here avoids any chance of loading a second copy of the harness's ``agent_workflow`` module (the
``_CURRENT_RUNNER`` split that breaks sandboxed tools). The runner is passed in and duck-typed.

NB: no ``from __future__ import annotations`` needed here — nothing reflects these annotations.
"""

import asyncio
import json
from functools import partial
from typing import Any, Awaitable, Callable, Sequence

from temporalio.exceptions import ApplicationError

from google.genai._interactions.types import (
    ErrorEvent,
    FunctionCallStep,
    InteractionCompletedEvent,
    StepDelta,
    StepStart,
    ToolParam,
)
from google.genai._interactions.types.error_event import Error
from google.genai._interactions.types.function_result_step_param import FunctionResultStepParam
from google.genai._interactions.types.interaction_create_params import Input
from google.genai._interactions.types.step_delta import DeltaArgumentsDelta, DeltaText
from google.genai.client import AsyncClient

# A workflow supplies this: run one model function-call and return its result step for the model.
DispatchTool = Callable[[FunctionCallStep], Awaitable[FunctionResultStepParam]]


def render_tool_result(result: object) -> str:
    """Render a tool's return value to text for the model. Callback tools return ``str``; sandboxed
    tools return a pydantic ``BaseModel`` (rendered as JSON so the model sees clean structure)."""
    if isinstance(result, str):
        return result
    model_dump_json = getattr(result, "model_dump_json", None)  # duck-typed pydantic BaseModel
    if callable(model_dump_json):
        return model_dump_json()
    try:
        return json.dumps(result)
    except TypeError:
        return str(result)


async def run_chat_turn(
    gemini: AsyncClient,
    *,
    model: str,
    system_instruction: str,
    user_text: str,
    tools: Sequence[ToolParam],
    previous_interaction_id: str | None,
    dispatch_tool: DispatchTool,
    generation_config: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Run one conversational turn to completion.

    Streams the model, dispatches any function calls via ``dispatch_tool`` (concurrently), feeds the
    results back, and loops until the model replies with no further calls. Returns
    ``(reply_text, new_previous_interaction_id)`` — the caller persists the id to chain the next turn.
    """
    next_input: Input = user_text
    while True:
        reply_text, pending_calls, previous_interaction_id = await _execute_agent_interaction(
            gemini=gemini,
            model=model,
            input=next_input,
            tools=tools,
            system_instruction=system_instruction,
            previous_interaction_id=previous_interaction_id,
            generation_config=generation_config,
        )
        if not pending_calls:
            return reply_text, previous_interaction_id
        next_input = await asyncio.gather(*(dispatch_tool(fc) for fc in pending_calls))


async def _execute_agent_interaction(
    *,
    gemini: AsyncClient,
    model: str,
    input: Input,
    tools: Sequence[ToolParam],
    system_instruction: str,
    previous_interaction_id: str | None,
    generation_config: dict[str, Any] | None,
) -> tuple[str, list[FunctionCallStep], str]:
    """Stream one ``interactions.create`` and reduce it into ``(reply_text, function_calls,
    interaction_id)``. Text comes from ``DeltaText`` events; function calls are captured from each
    ``StepStart``/``FunctionCallStep``, their JSON-string ``arguments`` fragments buffered per step
    index and ``json.loads``-ed once the stream ends. Raises ``ApplicationError`` on stream errors
    or if the stream ends without a completed event."""
    create_kwargs: dict[str, Any] = dict(
        model=model,
        input=input,
        system_instruction=system_instruction,
        tools=tools,
        stream=True,
    )
    if generation_config is not None:
        create_kwargs["generation_config"] = generation_config
    interactions_create_fn = partial(gemini.interactions.create, **create_kwargs)
    if previous_interaction_id:
        stream = await interactions_create_fn(previous_interaction_id=previous_interaction_id)
    else:
        stream = await interactions_create_fn()

    text_parts: list[str] = []
    calls_by_index: dict[int, FunctionCallStep] = {}
    arg_buffers: dict[int, str] = {}
    interaction_id: str | None = None
    async for event in stream:
        match event:
            case ErrorEvent(error=Error(message=msg, code=code)):
                raise ApplicationError(msg or "stream error", type=code or "stream_error")
            case ErrorEvent():
                raise ApplicationError("unknown stream error", type="stream_error")
            case StepStart(index=idx, step=FunctionCallStep() as call):
                calls_by_index[idx] = call
            case StepDelta(index=idx, delta=DeltaArgumentsDelta(arguments=args)) if args:
                arg_buffers[idx] = arg_buffers.get(idx, "") + args
            case StepDelta(delta=DeltaText(text=text)) if text:
                text_parts.append(text)
            case InteractionCompletedEvent(interaction=interaction):
                interaction_id = interaction.id

    if interaction_id is None:
        raise ApplicationError(
            "stream ended without interaction.completed event", type="stream_error"
        )

    function_calls = [
        calls_by_index[idx].model_copy(update={"arguments": json.loads(arg_buffers[idx])})
        if arg_buffers.get(idx)
        else calls_by_index[idx]
        for idx in sorted(calls_by_index)
    ]
    return "".join(text_parts), function_calls, interaction_id


async def dispatch_via_runner(
    call: FunctionCallStep,
    *,
    runner: Any,
    tools_by_name: dict[str, Any],
    todo_sink: list,
    todo_tool_names: tuple[str, ...] = ("todowrite", "todoread"),
) -> FunctionResultStepParam:
    """A ready-made :data:`DispatchTool` for a harness ``AgentWorkflowRunner``.

    Looks the tool up by name, runs it via ``runner.run_tool`` (applying the approval policy +
    tool_start/end; for a sandboxed tool this dispatches the durable in-sandbox activity, for a
    callback tool it parks on ``callback_requested``), and shapes the result (or any error) into the
    Gemini ``function_result`` step. The two todo tools get ``todo_sink`` injected as their ``sink``
    so they read/write the workflow's durable todo state. ``runner`` is duck-typed to avoid importing
    the harness here (see the module docstring)."""
    try:
        tool = tools_by_name.get(call.name)
        if tool is None:
            raise ValueError(f"unknown tool: {call.name!r}")
        injections = {"sink": todo_sink} if call.name in todo_tool_names else None
        result = await runner.run_tool(call.id, tool, injections=injections, **call.arguments)
        response: FunctionResultStepParam = {
            "type": "function_result",
            "call_id": call.id,
            "name": call.name,
            "result": render_tool_result(result),
        }
        if call.signature:
            response["signature"] = call.signature
        return response
    except Exception as e:
        response = {
            "type": "function_result",
            "call_id": call.id,
            "name": call.name,
            "result": str(e),
            "is_error": True,
        }
        if call.signature:
            response["signature"] = call.signature
        return response
