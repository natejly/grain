"""Workflow automations: natural language to a validated DAG (ADR 0007).

`dag` is the document grammar, `validate` is every compile-time check, and
`compiler` is the model call around them. The executor is deliberately not here
yet — ADR 0007 records why the run/event/approval machinery in
`services/agent_loop.py` is the substrate it will be built on rather than a
second orchestration runtime beside it.
"""
from __future__ import annotations

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
    "parse_graph",
    "references",
    "render_tool_catalogue",
    "summarize",
    "topological_order",
    "validate_graph",
]
