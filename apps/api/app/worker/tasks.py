from __future__ import annotations

from ..services.ingestion import ingest_source
from ..services.runs import execute_tool_call, process_run

TASKS = {
    "source.ingest": ingest_source,
    "run.process": process_run,
    "tool.execute": execute_tool_call,
}

