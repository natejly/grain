"""Keeping what an execution produced.

A chart that exists only in a tool result is a chart the user cannot open. So
every image, figure and document that comes back from a sandbox is written to
the same object store uploads use and given a `Source` row, which is the one
place in this workspace where a file has an id, a size and a UI that lists it.
Inventing a second store would mean a second retention policy, a second delete
path and a second thing to back up.

The rows are written with `status="stored"` rather than "ready". "ready" means
"ingested and indexed" everywhere else in this codebase, and these files never
went through ingest — retrieval must not be led to believe it can quote them.

The caps exist because a plotting loop is three lines long. `for col in df:
plt.plot(...); plt.show()` over a wide frame emits one PNG per column, and
without a ceiling a single careless cell turns into hundreds of megabytes of
workspace storage that nobody asked for and nobody will ever look at.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import Settings
from ...models import SandboxSession, Source, new_id
from ..ingestion import content_path, object_path
from .types import Artifact, ExecResult

#: Per execution. Eight is roughly "a figure per subplot of a normal analysis";
#: past that the model is looping, and the ninth chart adds nothing a human will
#: read. Extras are reported to the model rather than dropped silently, so it can
#: choose to plot less instead of wondering where its output went.
MAX_ARTIFACTS_PER_EXECUTION = 8

#: Total across one execution, independent of the count cap: eight 12 MB PDFs is
#: still 96 MB of storage bought by one tool call.
MAX_ARTIFACT_TOTAL_BYTES = 16 * 1024 * 1024

#: Not "ready" — see the module docstring.
ARTIFACT_STATUS = "stored"

#: base64 on the wire, bytes on disk.
_BINARY_KINDS: Dict[str, Tuple[str, str]] = {
    "png": ("image/png", "png"),
    "jpeg": ("image/jpeg", "jpg"),
    "pdf": ("application/pdf", "pdf"),
}

#: Text already, so it is stored as UTF-8 rather than decoded.
_TEXT_KINDS: Dict[str, Tuple[str, str]] = {
    "svg": ("image/svg+xml", "svg"),
    "html": ("text/html", "html"),
    "json": ("application/json", "json"),
    "chart": ("application/json", "json"),
    "text": ("text/plain", "txt"),
}


def _payload(artifact: Artifact) -> Optional[bytes]:
    """The bytes to store, or None when the provider handed us something we
    cannot decode.

    A corrupt base64 blob is not worth failing an otherwise successful execution
    over: the stdout the user asked for is still good. It is skipped and counted.
    """
    if artifact.kind in _BINARY_KINDS:
        try:
            # binascii.Error is a ValueError, so one clause covers both a
            # malformed blob and a non-string payload.
            return base64.b64decode(artifact.data, validate=True)
        except (ValueError, TypeError):
            return None
    # A chart carries its structured description in `chart_json`; that is the
    # thing worth keeping, because it can be re-rendered in the app's theme.
    charted = artifact.kind == "chart" and bool(artifact.chart_json)
    text = artifact.chart_json if charted else artifact.data
    if not text:
        return None
    return text.encode("utf-8")


def _dimensions(kind: str, data: bytes) -> Optional[Tuple[int, int]]:
    """Width and height read from the file header, or None.

    Worth the twenty lines: "image: png 480x320" tells the model whether its
    figure came out at all, where "image: png, 41 kB" does not. Pillow is not a
    dependency of this service and adding one to read eight bytes would be silly.
    """
    if kind == "png":
        # 8-byte signature, then a length+type header; IHDR's first two fields
        # are the dimensions. Anything shorter is not a PNG.
        if len(data) >= 24 and data[12:16] == b"IHDR":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        return None
    if kind == "jpeg":
        # Walk the marker segments to the start-of-frame, which is the only one
        # that carries the size. C4/C8/CC share the SOF range but are Huffman
        # tables, not frames.
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                return None
            marker = data[index + 1]
            length = int.from_bytes(data[index + 2 : index + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(data[index + 5 : index + 7], "big")
                width = int.from_bytes(data[index + 7 : index + 9], "big")
                return width, height
            if length <= 0:
                return None
            index += 2 + length
    return None


def _chart(artifact: Artifact) -> Optional[Any]:
    """The structured chart, parsed, so the descriptor stays JSON-safe.

    Unparseable chart JSON is dropped rather than passed through as a string: a
    consumer that expects an object and gets a string is a bug at the far end of
    the system, and the PNG beside it already carries the picture.
    """
    if not artifact.chart_json:
        return None
    try:
        parsed = json.loads(artifact.chart_json)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _owner(db: Session, *, workspace_id: str, session_id: str) -> str:
    """Whose sandbox this is, for `Source.created_by`.

    Read off the session row rather than taken as an argument, and filtered by
    workspace like every other read of that table.
    """
    session = db.scalar(
        select(SandboxSession).where(
            SandboxSession.id == session_id,
            SandboxSession.workspace_id == workspace_id,
        )
    )
    return session.created_by if session is not None else ""


def persist_artifacts(
    db: Session,
    *,
    workspace_id: str,
    session_id: str,
    result: ExecResult,
    settings: Settings,
) -> List[Dict[str, Any]]:
    """Store an execution's artifacts and describe them.

    Returns one JSON-safe descriptor per artifact that survived the caps. The
    caller renders these for the model and compares the count against
    `result.artifacts` to say how many were dropped — this function never raises
    for a bad artifact, because losing a figure must not lose the run's stdout.
    """
    if not result.artifacts:
        return []

    created_by = _owner(db, workspace_id=workspace_id, session_id=session_id)
    descriptors: List[Dict[str, Any]] = []
    total = 0
    for index, artifact in enumerate(result.artifacts, start=1):
        if len(descriptors) >= MAX_ARTIFACTS_PER_EXECUTION:
            break
        known = _BINARY_KINDS.get(artifact.kind) or _TEXT_KINDS.get(artifact.kind)
        if known is None:
            continue
        mime, extension = known
        data = _payload(artifact)
        if data is None:
            continue
        # A single artifact over the upload ceiling is refused for the same
        # reason an upload is: it is the size at which this workspace stops
        # holding files. The total ceiling stops eight legal ones adding up.
        if len(data) > settings.max_upload_bytes:
            continue
        if total + len(data) > MAX_ARTIFACT_TOTAL_BYTES:
            break
        total += len(data)

        source = Source(
            id=new_id(),
            workspace_id=workspace_id,
            created_by=created_by,
            filename=f"sandbox-{artifact.kind}-{index}.{extension}",
            media_type=mime,
            object_key="",
            byte_size=len(data),
            status=ARTIFACT_STATUS,
        )
        path = object_path(workspace_id, source.id, source.filename)
        path.write_bytes(data)
        source.object_key = str(path)
        db.add(source)

        descriptor: Dict[str, Any] = {
            "id": source.id,
            "kind": artifact.kind,
            "mime": mime,
            "bytes": len(data),
            # An id names the row; this names the bytes. Relative to the API
            # root rather than absolute, because the API's public origin is not
            # a thing this process reliably knows — the client already holds the
            # base it talks to, and joining is its job.
            "url": content_path(source.id),
        }
        size = _dimensions(artifact.kind, data)
        if size is not None:
            descriptor["width"], descriptor["height"] = size
        chart = _chart(artifact)
        if chart is not None:
            descriptor["chart"] = chart
        descriptors.append(descriptor)

    db.commit()
    return descriptors
