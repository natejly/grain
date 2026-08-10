"""Server-side code execution in hosted microVMs. See docs/adr/0005."""
from .types import (
    Artifact,
    ExecResult,
    FileEntry,
    SandboxError,
    SandboxHandle,
    SandboxProvider,
    SandboxQuotaError,
    SandboxSpec,
)

__all__ = [
    "Artifact",
    "ExecResult",
    "FileEntry",
    "SandboxError",
    "SandboxHandle",
    "SandboxProvider",
    "SandboxQuotaError",
    "SandboxSpec",
    "registry_tools",
]


def registry_tools(db, context):  # type: ignore[no-untyped-def]
    """Agent tools for this family. Imported lazily by llm_tools.build_registry.

    Local import for the same reason `mcp` and `projects` do it: tool modules
    import `llm_tools` for ToolSpec, so a top-level import here is a cycle.
    """
    from .tools import registry_tools as _tools

    return _tools(db, context)
