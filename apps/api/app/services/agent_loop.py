from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..config import Settings, get_settings
from ..models import AgentToolCall, Run, ToolPolicy
from .audit import record_audit
from .events import DeltaBuffer, append_event
from .llm_tools import ToolContext, ToolResult, ToolSpec, build_registry
from .model import (
    CHAT_INSTRUCTIONS,
    _openai_client,
    _openai_input,
    stream_agent_response,
)
from .retrieval import Evidence

MAX_ITERATIONS = 6

# A model step takes (input_items, tools, instructions) and returns an iterable of
# ("delta", text) events followed by ("completed", response), where response
# exposes .output (function calls have .type == "function_call", .name, .call_id,
# .arguments) and .output_text. Injectable so tests can script a model offline.
ModelStep = Callable[[List[Any], List[Dict[str, Any]], str], Iterable[Tuple[str, Any]]]

DENIAL_OUTPUT = (
    "The user denied this tool call. Do not retry it. Continue using what you "
    "already have, and tell the user what you could not do."
)


@dataclass
class AgentResult:
    answer: str
    evidence: List[Evidence] = field(default_factory=list)


@dataclass
class Done:
    answer: str
    evidence: List[Evidence]


@dataclass
class Paused:
    """The run is parked waiting for the user to decide on a tool call."""

    tool_call_id: str


@dataclass
class Cancelled:
    text: str


Outcome = Done | Paused | Cancelled


@dataclass
class LoopState:
    """Everything needed to resume a turn in a different process.

    `input_items` holds plain dicts rather than SDK objects — the Responses API
    accepts dicts on input, so a round trip through JSON is lossless.
    """

    input_items: List[Any] = field(default_factory=list)
    pending_calls: List[Dict[str, Any]] = field(default_factory=list)
    iteration: int = 0
    text_so_far: str = ""
    evidence: List[Evidence] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "input_items": self.input_items,
                "pending_calls": self.pending_calls,
                "iteration": self.iteration,
                "text_so_far": self.text_so_far,
                "evidence": [asdict(item) for item in self.evidence],
            },
            default=str,
        )

    @classmethod
    def from_json(cls, raw: str) -> LoopState:
        data = json.loads(raw)
        return cls(
            input_items=data.get("input_items") or [],
            pending_calls=data.get("pending_calls") or [],
            iteration=int(data.get("iteration") or 0),
            text_so_far=data.get("text_so_far") or "",
            evidence=[Evidence(**item) for item in data.get("evidence") or []],
        )


def _default_model_step(
    settings: Settings, run: Run, evidence: List[Evidence]
) -> ModelStep:
    """The model behind one turn.

    `evidence` only reaches the scripted test double, which quotes it when no
    script entry matches the prompt. The OpenAI path is handed the same passages
    through the prompt itself, so it has no use for them here.
    """
    if settings.active_model_provider == "scripted":
        from .scripted_model import scripted_model_step

        return scripted_model_step(settings, prompt=run.prompt, evidence=evidence)

    client = _openai_client(settings)

    def step(
        input_items: List[Any], tools: List[Dict[str, Any]], instructions: str
    ) -> Iterable[Tuple[str, Any]]:
        return stream_agent_response(
            client,
            settings,
            user_id=run.created_by,
            input_items=input_items,
            tools=tools,
            instructions=instructions,
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


def _serialize_item(item: Any) -> Any:
    """Turn one model output item into something JSON-serializable."""
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True)
    return {key: value for key, value in vars(item).items() if value is not None}


def resolve_policy(
    db: Session, *, workspace_id: str, spec: Optional[ToolSpec]
) -> str:
    """ask | allow | deny for one tool, workspace override beating the default.

    Without an override, read-only tools run unattended and write-capable tools
    ask. An unknown tool resolves to allow so the loop can hand the model back a
    plain "unknown tool" error instead of parking the run on a phantom approval.
    """
    if spec is None:
        return "allow"
    override = db.scalar(
        select(ToolPolicy).where(
            ToolPolicy.workspace_id == workspace_id,
            ToolPolicy.tool_name == spec.name,
        )
    )
    if override is not None and override.policy in {"ask", "allow", "deny"}:
        return override.policy
    return "allow" if spec.read_only else "ask"


def _render_result(result: ToolResult, evidence_offset: int) -> str:
    parts: List[str] = []
    if result.content:
        parts.append(result.bounded_content())
    for index, item in enumerate(result.evidence, start=evidence_offset + 1):
        parts.append(
            f"[{index}] {item.filename}, passage {item.ordinal + 1}\n{item.excerpt}"
        )
    return "\n\n".join(parts) if parts else "(empty result)"


def _cancelled(db: Session, run: Run, state: LoopState) -> Cancelled:
    run.status = "cancelled"
    run.lease_expires_at = None
    run.agent_state_json = None
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type="run.cancelled",
        payload={"status": "cancelled"},
    )
    db.commit()
    return Cancelled(text=state.text_so_far)


def _describe_proposal(
    db: Session, context: ToolContext, spec: ToolSpec, raw_arguments: str
) -> str:
    """Render what the call would do, for the approval card. Never fatal."""
    if spec.preview is None:
        return ""
    try:
        arguments = json.loads(raw_arguments) if raw_arguments else {}
        if not isinstance(arguments, dict):
            return ""
        return spec.preview(db, context, arguments)[:20000]
    except Exception:
        # A preview is a courtesy; failing to render one must not block the
        # approval the user is waiting on.
        db.rollback()
        return ""


def _park_for_approval(
    db: Session,
    run: Run,
    state: LoopState,
    call: Dict[str, Any],
    spec: ToolSpec,
    context: ToolContext,
) -> Paused:
    """Record the proposed call, persist loop state, and stop the turn."""
    raw_arguments = str(call.get("arguments") or "{}")
    record = AgentToolCall(
        workspace_id=run.workspace_id,
        run_id=run.id,
        name=str(call.get("name") or ""),
        arguments_json=raw_arguments[:4000],
        call_id=str(call.get("call_id") or "")[:80],
        proposal_preview=_describe_proposal(db, context, spec, raw_arguments),
        status="proposed",
    )
    db.add(record)
    db.flush()
    call["tool_call_id"] = record.id
    run.status = "waiting_for_approval"
    run.lease_expires_at = None
    run.agent_state_json = state.to_json()
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type="tool.proposed",
        payload={
            "tool_call_id": record.id,
            "tool_name": record.name,
            "description": spec.description,
            "arguments": record.arguments_json,
            "preview": record.proposal_preview,
        },
    )
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type="run.waiting_for_approval",
        payload={"status": "waiting_for_approval"},
    )
    record_audit(
        db,
        workspace_id=run.workspace_id,
        actor_id=run.created_by,
        action="agent_tool.proposed",
        resource_type="agent_tool_call",
        resource_id=record.id,
        detail={"tool": record.name},
    )
    db.commit()
    return Paused(tool_call_id=record.id)


def _execute_call(
    db: Session,
    run: Run,
    registry: Dict[str, ToolSpec],
    context: ToolContext,
    name: str,
    raw_arguments: str,
    *,
    existing_id: Optional[str] = None,
    denied: bool = False,
) -> ToolResult:
    started = time.monotonic()
    record = db.get(AgentToolCall, existing_id) if existing_id else None
    if record is None:
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

    if denied:
        result = ToolResult(content=DENIAL_OUTPUT)
        record.status = "denied"
    else:
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
                record.status = "succeeded"
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


def _drain_pending(
    db: Session,
    run: Run,
    state: LoopState,
    *,
    registry: Dict[str, ToolSpec],
    context: ToolContext,
) -> Optional[Outcome]:
    """Run queued calls until one needs approval. None means the queue emptied."""
    while state.pending_calls:
        db.refresh(run)
        if run.cancel_requested:
            return _cancelled(db, run, state)
        call = state.pending_calls[0]
        name = str(call.get("name") or "")
        spec = registry.get(name)
        decision = call.get("decision")
        if decision is None:
            policy = resolve_policy(db, workspace_id=run.workspace_id, spec=spec)
            if policy == "ask" and spec is not None:
                return _park_for_approval(db, run, state, call, spec, context)
            decision = "denied" if policy == "deny" else "approved"
        result = _execute_call(
            db,
            run,
            registry,
            context,
            name,
            str(call.get("arguments") or "{}"),
            existing_id=call.get("tool_call_id"),
            denied=decision == "denied",
        )
        state.input_items.append(
            {
                "type": "function_call_output",
                "call_id": str(call.get("call_id") or ""),
                "output": _render_result(result, evidence_offset=len(state.evidence)),
            }
        )
        state.evidence.extend(result.evidence)
        state.pending_calls.pop(0)
    return None


def _advance(
    db: Session,
    run: Run,
    state: LoopState,
    *,
    registry: Dict[str, ToolSpec],
    context: ToolContext,
    step: ModelStep,
) -> Outcome:
    tools = _tool_payload(registry)
    while True:
        blocked = _drain_pending(db, run, state, registry=registry, context=context)
        if blocked is not None:
            return blocked
        if state.iteration >= MAX_ITERATIONS:
            raise RuntimeError("Agent loop exceeded the iteration budget")
        db.refresh(run)
        if run.cancel_requested:
            return _cancelled(db, run, state)

        final_round = state.iteration == MAX_ITERATIONS - 1
        buffer = DeltaBuffer(db, workspace_id=run.workspace_id, run_id=run.id)
        response: Any = None
        for kind, value in step(
            state.input_items, [] if final_round else tools, CHAT_INSTRUCTIONS
        ):
            if kind == "delta":
                buffer.add(str(value))
            elif kind == "completed":
                response = value
        buffer.flush()
        state.text_so_far += buffer.text
        state.iteration += 1
        if response is None:
            raise RuntimeError("Model stream ended without a completed response")

        calls = [
            item
            for item in (response.output or [])
            if getattr(item, "type", None) == "function_call"
        ]
        if not calls:
            answer = (state.text_so_far or response.output_text or "").strip()
            if not answer:
                raise RuntimeError("Model returned an empty response")
            return Done(answer=answer, evidence=state.evidence)
        state.input_items.extend(_serialize_item(item) for item in response.output)
        state.pending_calls = [
            {
                "call_id": getattr(call, "call_id", ""),
                "name": getattr(call, "name", ""),
                "arguments": getattr(call, "arguments", "") or "{}",
            }
            for call in calls
        ]


def _finish(db: Session, run: Run, outcome: Outcome) -> Optional[AgentResult]:
    if isinstance(outcome, Done):
        run.agent_state_json = None
        db.commit()
        return AgentResult(answer=outcome.answer, evidence=outcome.evidence)
    return None


def run_agent_turn(
    db: Session,
    run: Run,
    *,
    evidence: List[Evidence],
    transcript: Optional[List[Any]] = None,
    memory_context: str = "",
    settings: Optional[Settings] = None,
    model_step: Optional[ModelStep] = None,
) -> Optional[AgentResult]:
    """Start a turn. None means the run parked for approval or was cancelled."""
    settings = settings or get_settings()
    context = ToolContext(
        workspace_id=run.workspace_id,
        user_id=run.created_by,
        conversation_id=run.conversation_id,
    )
    registry = build_registry(db, context)
    state = LoopState(
        input_items=[
            {
                "role": "user",
                "content": _openai_input(
                    run.prompt, evidence, transcript, memory_context
                ),
            }
        ],
        evidence=list(evidence),
    )
    outcome = _advance(
        db,
        run,
        state,
        registry=registry,
        context=context,
        step=model_step or _default_model_step(settings, run, list(evidence)),
    )
    return _finish(db, run, outcome)


def resume_agent_turn(
    db: Session,
    run: Run,
    *,
    tool_call_id: str,
    decision: str,
    settings: Optional[Settings] = None,
    model_step: Optional[ModelStep] = None,
) -> Optional[AgentResult]:
    """Continue a parked turn once the user has decided on the proposed call."""
    settings = settings or get_settings()
    if not run.agent_state_json:
        raise RuntimeError("Run has no saved agent state to resume")
    state = LoopState.from_json(run.agent_state_json)
    if not state.pending_calls:
        raise RuntimeError("Run has no pending tool call to resume")
    head = state.pending_calls[0]
    if head.get("tool_call_id") != tool_call_id:
        raise RuntimeError("Decision does not match the parked tool call")
    head["decision"] = decision

    record = db.get(AgentToolCall, tool_call_id)
    if record is not None:
        record.decided_at = utcnow()
    run.status = "running"
    run.agent_state_json = None
    db.commit()

    context = ToolContext(
        workspace_id=run.workspace_id,
        user_id=run.created_by,
        conversation_id=run.conversation_id,
    )
    registry = build_registry(db, context)
    outcome = _advance(
        db,
        run,
        state,
        registry=registry,
        context=context,
        step=model_step or _default_model_step(settings, run, state.evidence),
    )
    return _finish(db, run, outcome)
