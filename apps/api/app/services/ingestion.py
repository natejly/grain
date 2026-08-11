from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from pypdf import PdfReader
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..database import SessionLocal
from ..models import Chunk, Source
from .audit import record_audit
from .graph import rebuild_graph
from .model import situate_chunk
from .retrieval import clear_source_postings, embed_chunks, index_chunks
from .usage import usage_scope

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".json", ".pdf"}

# The blurb is one small independent call per chunk, and a 60-chunk document
# serialised would add minutes to an ingest. Modest width because this shares the
# ingest worker with the graph extractor.
CONTEXT_WORKERS = 8


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


def contextualize_chunks(
    chunks: Sequence[Chunk], document: str, settings: Optional[Settings] = None
) -> List[Chunk]:
    """Write a situating blurb onto each chunk. Returns the ones that got one.

    Best-effort per chunk, not per document: `situate_chunk` answers "" for a
    failure, so a provider hiccup costs one chunk its context rather than costing
    the document its ingest. The chunks that did gain a blurb come back because
    they — and only they — now need re-indexing and re-embedding.
    """
    settings = settings or get_settings()
    if not settings.retrieval_contextual or not chunks:
        return []
    workspace_id = chunks[0].workspace_id

    def situate(chunk: Chunk) -> str:
        # The scope is opened *inside* the worker. A ThreadPoolExecutor thread
        # starts with a fresh context rather than a copy of the submitter's, so a
        # scope opened around `pool.map` would not reach the call that spends the
        # tokens and every blurb in the corpus would be billed to nobody.
        with usage_scope(workspace_id=workspace_id):
            return situate_chunk(document, chunk.content, settings=settings)

    with ThreadPoolExecutor(max_workers=CONTEXT_WORKERS) as pool:
        blurbs = list(pool.map(situate, chunks))
    written: List[Chunk] = []
    for chunk, blurb in zip(chunks, blurbs, strict=True):
        if blurb:
            chunk.context_prefix = blurb
            written.append(chunk)
    return written


def prepare_for_search(
    db: Session,
    chunks: Sequence[Chunk],
    *,
    document: str,
    settings: Optional[Settings] = None,
) -> None:
    """Make chunks retrievable: context blurb, then term index, then vectors.

    Order matters. Both the term index and the embedding cover
    `context_prefix + content`, so the blurb has to exist before either runs or
    the two halves of hybrid retrieval would describe different text.

    The optional stage is isolated from the two that are not. `situate_chunk`
    answers "" for every failure it can see, but "every failure it can see" is not
    all of them — a pool that cannot start a thread raises straight through
    `contextualize_chunks` — and running first means anything that escapes it
    takes the index and the vectors down with it. The index would be rebuilt by
    `reconcile_index` on the next search; the vector would not be rebuilt by
    anything, so a blurb the corpus is configured not to need would have silently
    emptied the dense arm until someone ran the backfill.
    """
    settings = settings or get_settings()
    try:
        contextualize_chunks(chunks, document, settings)
    except Exception:
        logger.warning(
            "situating blurbs failed; indexing the author's text alone", exc_info=True
        )
    index_chunks(db, chunks)
    embed_chunks(db, chunks, settings)


def backfill_workspace(workspace_id: str, settings: Optional[Settings] = None) -> dict[str, int]:
    """Bring pre-existing chunks up to the current index. Idempotent.

    Chunks written before hybrid retrieval have no postings, no vectors and no
    blurb, and nothing on the read path can create the last two — an embedding is
    a network call and belongs nowhere near a user waiting for an answer. This is
    that path, for one workspace, safe to re-run.
    """
    settings = settings or get_settings()
    db = SessionLocal()
    counts = {"contextualized": 0, "indexed": 0, "embedded": 0}
    try:
        rows = list(
            db.execute(
                select(Chunk, Source)
                .join(Source, Source.id == Chunk.source_id)
                .where(
                    Chunk.workspace_id == workspace_id,
                    Source.workspace_id == workspace_id,
                    Source.deleted_at.is_(None),
                    Source.status == "ready",
                )
                .order_by(Chunk.source_id, Chunk.ordinal)
            ).all()
        )
        by_source: dict[str, List[Chunk]] = {}
        for chunk, source in rows:
            by_source.setdefault(source.id, []).append(chunk)
        for chunks in by_source.values():
            # The parent document, reassembled from the chunks themselves rather
            # than re-read from disk: the object may be gone, and overlapping
            # chunks still carry every sentence the blurb needs.
            document = "\n\n".join(chunk.content for chunk in chunks)
            contextualized = contextualize_chunks(
                [chunk for chunk in chunks if not chunk.context_prefix], document, settings
            )
            counts["contextualized"] += len(contextualized)
            # Only what actually changed: a chunk that gained a blurb now indexes
            # and embeds different text, and a chunk that never had either needs
            # both. Everything else is already current, which is what makes a
            # re-run cost nothing rather than re-billing the whole corpus.
            gained = {chunk.id for chunk in contextualized}
            stale = [
                chunk
                for chunk in chunks
                if chunk.lexical_length is None or chunk.id in gained
            ]
            index_chunks(db, stale)
            counts["indexed"] += len(stale)
            pending_vectors = [
                chunk
                for chunk in chunks
                if chunk.embedding is None
                or chunk.embedding_model != settings.openai_embedding_model
                or chunk.id in gained
            ]
            counts["embedded"] += embed_chunks(db, pending_vectors, settings)
            db.commit()
    finally:
        db.close()
    return counts


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
            # Postings first: they hang off chunk ids, and re-ingest replaces the
            # chunks those ids belong to.
            clear_source_postings(db, source.id)
            db.execute(delete(Chunk).where(Chunk.source_id == source.id))
            records: List[Chunk] = []
            for ordinal, (char_start, char_end, content) in enumerate(chunks):
                record = Chunk(
                    workspace_id=source.workspace_id,
                    source_id=source.id,
                    ordinal=ordinal,
                    content=content,
                    char_start=char_start,
                    char_end=char_end,
                    token_count=max(1, len(content.split())),
                )
                db.add(record)
                records.append(record)
            try:
                prepare_for_search(db, records, document=text)
            except Exception:
                # Search preparation is derived data. A chunk with no postings is
                # picked up by `reconcile_index` on the next search and a chunk
                # with no vector is still lexically retrievable, so none of it is
                # worth failing an ingest — and failing one would lose the
                # document itself, which is not derived from anything.
                logger.warning("search preparation failed for %s", source.id, exc_info=True)
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


#: How a browser asks for the bytes `object_path` wrote. Declared here, beside
#: the place they live, and used by the route itself (`api/sources.py`) so the
#: address a descriptor hands out and the address the server answers on cannot
#: drift apart. Only the path shape is shared: the "/api" prefix belongs to
#: every router in this app at once, and the origin belongs to the client.
SOURCE_CONTENT_ROUTE = "/sources/{source_id}/content"


def content_path(source_id: str) -> str:
    """The API path that streams one source's stored bytes.

    Relative on purpose. This process does not reliably know the public origin
    it is reached on — it sits behind whatever the deployment put in front of it
    — and the client already holds the base URL it is talking to.
    """
    return "/api" + SOURCE_CONTENT_ROUTE.format(source_id=source_id)
