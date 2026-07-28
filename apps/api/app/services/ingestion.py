from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import List, Tuple

from pypdf import PdfReader
from sqlalchemy import delete

from ..config import get_settings
from ..database import SessionLocal
from ..models import Chunk, Source
from .audit import record_audit
from .graph import rebuild_graph

SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".json", ".pdf"}


def sanitize_filename(filename: str) -> str:
    name = Path(filename).name.strip().replace("\x00", "")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
    return name[:255] or "source.txt"


def validate_filename(filename: str) -> None:
    if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ValueError("Unsupported source type. Supported extensions: " + supported)


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode source text")


def extract_text(path: Path, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    data = path.read_bytes()
    if suffix == ".pdf":
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > 200:
            raise ValueError("PDF exceeds the 200-page limit")
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    text = _decode_text(data)
    if suffix == ".json":
        parsed = json.loads(text)
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    if suffix == ".csv":
        rows = list(csv.reader(io.StringIO(text)))
        if len(rows) > 50_000:
            raise ValueError("CSV exceeds the 50,000-row limit")
        return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
    return text


def make_chunks(
    text: str, target_chars: int = 900, overlap_chars: int = 120
) -> Iterable[Tuple[int, int, str]]:
    normalized = re.sub(r"\r\n?", "\n", text)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if not normalized:
        return []
    chunks: List[Tuple[int, int, str]] = []
    start = 0
    length = len(normalized)
    while start < length:
        ideal_end = min(start + target_chars, length)
        end = ideal_end
        if ideal_end < length:
            boundary = max(
                normalized.rfind("\n\n", start + target_chars // 2, ideal_end),
                normalized.rfind(". ", start + target_chars // 2, ideal_end),
            )
            if boundary > start:
                end = boundary + (2 if normalized[boundary : boundary + 2] == ". " else 0)
        content = normalized[start:end].strip()
        if content:
            content_start = normalized.find(content, start, end + 1)
            chunks.append((content_start, content_start + len(content), content))
        if end >= length:
            break
        start = max(end - overlap_chars, start + 1)
    return chunks


def ingest_source(source_id: str, actor_id: str) -> None:
    db = SessionLocal()
    try:
        source = db.get(Source, source_id)
        if source is None or source.deleted_at is not None:
            return
        source.status = "processing"
        source.error = ""
        db.commit()
        try:
            text = extract_text(Path(source.object_key), source.filename)
            chunks = list(make_chunks(text))
            if not chunks:
                raise ValueError("No readable text was found in this source")
            db.execute(delete(Chunk).where(Chunk.source_id == source.id))
            for ordinal, (char_start, char_end, content) in enumerate(chunks):
                db.add(
                    Chunk(
                        workspace_id=source.workspace_id,
                        source_id=source.id,
                        ordinal=ordinal,
                        content=content,
                        char_start=char_start,
                        char_end=char_end,
                        token_count=max(1, len(content.split())),
                    )
                )
            source.status = "ready"
            source.chunk_count = len(chunks)
            record_audit(
                db,
                workspace_id=source.workspace_id,
                actor_id=actor_id,
                action="source.ingested",
                resource_type="source",
                resource_id=source.id,
                detail={"chunks": len(chunks)},
            )
            db.commit()
            rebuild_graph(source.workspace_id, actor_id)
        except Exception as exc:
            source.status = "failed"
            source.error = str(exc)[:1000]
            record_audit(
                db,
                workspace_id=source.workspace_id,
                actor_id=actor_id,
                action="source.ingestion_failed",
                resource_type="source",
                resource_id=source.id,
                detail={"error": source.error},
            )
            db.commit()
    finally:
        db.close()


def object_path(workspace_id: str, source_id: str, filename: str) -> Path:
    settings = get_settings()
    directory = settings.objects_dir / workspace_id / source_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory / sanitize_filename(filename)
