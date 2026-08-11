"""Workflow automations: natural language to a validated DAG (ADR 0007).

`dag` is the document grammar, `validate` is every compile-time check, and
`compiler` is the model call around them. `executor` walks a compiled graph on
the run/event/approval machinery in `services/agent_loop.py` — ADR 0007 records
why that substrate rather than a second orchestration runtime beside it — with
`refs` resolving `{{ node.output }}` at run time and `schedule` deciding what a
cron fires.

`executor` and `schedule` are imported as modules rather than re-exported
symbol by symbol: `agent_loop` imports the executor lazily to resume a parked
workflow, and keeping the seam a module attribute makes that one line instead of
an import list that has to be kept in step.
"""
from __future__ import annotations

from . import executor, refs, schedule
from .compiler import (
    WORKFLOW_INSTRUCTIONS,
    CompiledWorkflow,
    CompilerStep,
    compile_document,
    compile_workflow,
    render_tool_catalogue,
    summarize,
)
from .dag import EdgeSpec, NodeSpec, TriggerSpec, WorkflowGraph, references
from .validate import (
    CompileError,
    CompileReport,
    WorkflowCompileError,
    cron_error,
    cron_matches,
    parse_graph,
    topological_order,
    validate_graph,
)

__all__ = [
    "WORKFLOW_INSTRUCTIONS",
    "CompileError",
    "CompileReport",
    "CompiledWorkflow",
    "CompilerStep",
    "EdgeSpec",
    "NodeSpec",
    "TriggerSpec",
    "WorkflowCompileError",
    "WorkflowGraph",
    "compile_document",
    "compile_workflow",
    "cron_error",
    "cron_matches",
    "executor",
    "parse_graph",
    "references",
    "refs",
    "render_tool_catalogue",
    "schedule",
    "summarize",
    "topological_order",
    "validate_graph",
]
