from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, cast

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import AgentToolCall, Run
from .audit import record_audit
from .events import append_event
from .llm_tools import ToolContext, ToolResult, ToolSpec, build_registry
from .model import CHAT_INSTRUCTIONS, _openai_client, _openai_input, privacy_safe_identifier
from .retrieval import Evidence

MAX_ITERATIONS = 6

# A model step takes (input_items, tools, instructions) and returns a response
# exposing .output (list of items, function calls have .type == "function_call",
# .name, .call_id, .arguments) and .output_text. Injectable for offline tests.
ModelStep = Callable[[List[Any], List[Dict[str, Any]], str], Any]


@dataclass
class AgentResult:
    answer: str
    evidence: List[Evidence] = field(default_factory=list)


def _default_model_step(settings: Settings, user_id: str) -> ModelStep:
    client = _openai_client(settings)

    def step(input_items: List[Any], tools: List[Dict[str, Any]], instructions: str) -> Any:
        return client.responses.create(
            model=settings.openai_model,
            instructions=instructions,
            input=cast(Any, input_items),
            tools=cast(Any, tools),
            reasoning={"effort": settings.openai_reasoning_effort},
            text={"verbosity": "low"},
            max_output_tokens=settings.openai_max_output_tokens,
            safety_identifier=privacy_safe_identifier(user_id),
            store=False,
        )

    return step


def _tool_payload(registry: Dict[str, ToolSpec]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        }
        for spec in registry.values()
    ]


def _render_result(result: ToolResult, evidence_offset: int) -> str:
    parts: List[str] = []
    if result.content:
        parts.append(result.bounded_content())
    for index, item in enumerate(result.evidence, start=evidence_offset + 1):
        parts.append(
            f"[{index}] {item.filename}, passage {item.ordinal + 1}\n{item.excerpt}"
        )
    return "\n\n".join(parts) if parts else "(empty result)"


def _execute_call(
    db: Session,
    run: Run,
    registry: Dict[str, ToolSpec],
    context: ToolContext,
    name: str,
    raw_arguments: str,
) -> ToolResult:
    started = time.monotonic()
    record = AgentToolCall(
        workspace_id=run.workspace_id,
        run_id=run.id,
        name=name,
        arguments_json=raw_arguments[:4000],
    )
    db.add(record)
    db.flush()
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type="tool.started",
        payload={"tool_call_id": record.id, "tool_name": name},
    )
    db.commit()
    spec = registry.get(name)
    try:
        arguments = json.loads(raw_arguments) if raw_arguments else {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
    except (ValueError, TypeError):
        arguments = None
    if spec is None:
        result = ToolResult(content=f"Error: unknown tool “{name}”.")
        record.status = "failed"
        record.error = "unknown tool"
    elif arguments is None:
        result = ToolResult(content="Error: tool arguments were not valid JSON.")
        record.status = "failed"
        record.error = "invalid arguments"
    else:
        try:
            result = spec.executor(db, context, arguments)
        except Exception as exc:  # Tool bugs become model-visible errors, not crashes.
            db.rollback()
            record = db.get(AgentToolCall, record.id) or record
            result = ToolResult(content=f"Error: tool failed: {str(exc)[:300]}")
            record.status = "failed"
            record.error = str(exc)[:1000]
    record.latency_ms = int((time.monotonic() - started) * 1000)
    record.result_preview = (result.content or "")[:500]
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type="tool.completed",
        payload={
            "tool_call_id": record.id,
            "tool_name": name,
            "status": record.status,
            "preview": record.result_preview,
        },
    )
    record_audit(
        db,
        workspace_id=run.workspace_id,
        actor_id=run.created_by,
        action="agent_tool.executed",
        resource_type="agent_tool_call",
        resource_id=record.id,
        detail={"tool": name, "status": record.status},
    )
    db.commit()
    return result


def run_agent_turn(
    db: Session,
    run: Run,
    *,
    evidence: List[Evidence],
    transcript: Optional[List[Any]] = None,
    memory_context: str = "",
    settings: Optional[Settings] = None,
    model_step: Optional[ModelStep] = None,
) -> AgentResult:
    settings = settings or get_settings()
    context = ToolContext(
        workspace_id=run.workspace_id,
        user_id=run.created_by,
        conversation_id=run.conversation_id,
    )
    registry = build_registry(db, context)
    tools = _tool_payload(registry)
    step = model_step or _default_model_step(settings, run.created_by)

    collected: List[Evidence] = list(evidence)
    input_items: List[Any] = [
        {
            "role": "user",
            "content": _openai_input(run.prompt, evidence, transcript, memory_context),
        }
    ]

    for iteration in range(MAX_ITERATIONS):
        final_round = iteration == MAX_ITERATIONS - 1
        response = step(input_items, [] if final_round else tools, CHAT_INSTRUCTIONS)
        calls = [
            item
            for item in (response.output or [])
            if getattr(item, "type", None) == "function_call"
        ]
        if not calls:
            answer = (response.output_text or "").strip()
            if not answer:
                raise RuntimeError("Model returned an empty response")
            return AgentResult(answer=answer, evidence=collected)
        input_items.extend(response.output)
        for call in calls:
            result = _execute_call(
                db,
                run,
                registry,
                context,
                getattr(call, "name", ""),
                getattr(call, "arguments", "") or "{}",
            )
            output_text = _render_result(result, evidence_offset=len(collected))
            collected.extend(result.evidence)
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": getattr(call, "call_id", ""),
                    "output": output_text,
                }
            )
    raise RuntimeError("Agent loop exceeded the iteration budget")
