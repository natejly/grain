from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..database import SessionLocal
from ..models import GraphEdge, GraphEntity, MemoryItem, Message, Run
from .audit import record_audit
from .embeddings import cosine_similarity, embed_texts, unpack_vector
from .graph import extract_entities, mark_graph_stale
from .model import extract_memories
from .retrieval import tokenize

MAX_GRAPH_DIGEST_LINES = 10
SUMMARY_REFRESH_EVERY = 10


@dataclass
class MemoryContext:
    items: List[MemoryItem] = field(default_factory=list)
    graph_digest: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.items and not self.graph_digest


def _content_key(content: str) -> str:
    digest = hashlib.sha256(content.casefold().encode()).hexdigest()
    return digest[:40]


def _upsert_item(
    db: Session,
    *,
    workspace_id: str,
    conversation_id: Optional[str],
    run_id: str,
    kind: str,
    normalized_key: str,
    content: str,
    entity_names: List[str],
    message_ids: List[str],
) -> MemoryItem:
    existing = db.scalar(
        select(MemoryItem).where(
            MemoryItem.workspace_id == workspace_id,
            MemoryItem.kind == kind,
            MemoryItem.normalized_key == normalized_key,
        )
    )
    if existing is not None:
        if existing.status == "deleted":
            # The user explicitly forgot this memory; do not resurrect it.
            return existing
        existing.content = content
        existing.importance += 1
        merged = set(json.loads(existing.message_ids_json)) | set(message_ids)
        existing.message_ids_json = json.dumps(sorted(merged)[:50])
        existing.entity_names_json = json.dumps(
            sorted(set(json.loads(existing.entity_names_json)) | set(entity_names))[:16]
        )
        existing.embedding = None
        existing.updated_at = datetime.utcnow()
        return existing
    item = MemoryItem(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        run_id=run_id,
        kind=kind,
        content=content,
        normalized_key=normalized_key,
        entity_names_json=json.dumps(entity_names[:16]),
        message_ids_json=json.dumps(message_ids[:50]),
    )
    db.add(item)
    return item


def _deterministic_memories(prompt: str) -> List[Dict[str, object]]:
    excerpt = " ".join(prompt.split())[:160]
    memories: List[Dict[str, object]] = []
    for normalized, display in extract_entities(prompt):
        memories.append(
            {
                "kind": "entity_note",
                "content": f"{display} came up in conversation: “{excerpt}”",
                "entities": [display],
                "normalized_key": normalized,
            }
        )
    return memories


def _refresh_summary(
    db: Session,
    run: Run,
    settings: Settings,
) -> None:
    messages = list(
        db.scalars(
            select(Message)
            .where(
                Message.workspace_id == run.workspace_id,
                Message.conversation_id == run.conversation_id,
            )
            .order_by(Message.created_at.asc())
        )
    )
    if len(messages) < SUMMARY_REFRESH_EVERY:
        return
    user_lines = [
        " ".join(message.content.split())[:120]
        for message in messages
        if message.role == "user"
    ][:8]
    content = "Conversation topics so far: " + "; ".join(user_lines)
    _upsert_item(
        db,
        workspace_id=run.workspace_id,
        conversation_id=run.conversation_id,
        run_id=run.id,
        kind="summary",
        normalized_key=run.conversation_id,
        content=content[:900],
        entity_names=[],
        message_ids=[message.id for message in messages[-4:]],
    )


def write_conversation_memory(run_id: str) -> None:
    """Persist durable memories after a completed run. Best-effort by design."""
    settings = get_settings()
    if not settings.memory_enabled:
        return
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run is None or run.status != "completed":
            return
        messages = list(
            db.scalars(
                select(Message)
                .where(Message.run_id == run.id)
                .order_by(Message.created_at.asc())
            )
        )
        answer = next(
            (message.content for message in reversed(messages) if message.role == "assistant"),
            "",
        )
        message_ids = [message.id for message in messages]

        if settings.active_model_provider == "openai":
            extracted = extract_memories(
                run.prompt, answer, user_id=run.created_by, settings=settings
            )
        else:
            extracted = _deterministic_memories(run.prompt)

        touched: List[MemoryItem] = []
        for raw in extracted[: settings.memory_max_items_per_run]:
            content = str(raw.get("content") or "").strip()
            if not content:
                continue
            kind = str(raw.get("kind") or "fact")
            normalized_key = str(raw.get("normalized_key") or _content_key(content))[:200]
            raw_entities = raw.get("entities")
            entities = (
                [str(name) for name in raw_entities]
                if isinstance(raw_entities, list)
                else []
            )
            touched.append(
                _upsert_item(
                    db,
                    workspace_id=run.workspace_id,
                    conversation_id=run.conversation_id,
                    run_id=run.id,
                    kind=kind,
                    normalized_key=normalized_key,
                    content=content,
                    entity_names=entities,
                    message_ids=message_ids,
                )
            )
        _refresh_summary(db, run, settings)
        db.flush()

        pending = [item for item in touched if item.status == "active" and item.embedding is None]
        if pending:
            try:
                vectors = embed_texts([item.content for item in pending], settings)
            except Exception:
                vectors = None
            if vectors is not None:
                for item, vector in zip(pending, vectors):
                    item.embedding = vector
                    item.embedding_model = settings.openai_embedding_model

        if touched:
            mark_graph_stale(db, run.workspace_id)
            record_audit(
                db,
                workspace_id=run.workspace_id,
                actor_id=run.created_by,
                action="memory.updated",
                resource_type="run",
                resource_id=run.id,
                detail={"items": len(touched)},
            )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _graph_digest(db: Session, workspace_id: str, query: str) -> List[str]:
    names = [normalized for normalized, _display in extract_entities(query)]
    if not names:
        return []
    entities = list(
        db.scalars(
            select(GraphEntity).where(
                GraphEntity.workspace_id == workspace_id,
                GraphEntity.normalized_name.in_(names),
            )
        )
    )
    if not entities:
        return []
    by_id = {entity.id: entity for entity in entities}
    edges = list(
        db.scalars(
            select(GraphEdge)
            .where(
                GraphEdge.workspace_id == workspace_id,
                (
                    GraphEdge.from_entity_id.in_(by_id.keys())
                    | GraphEdge.to_entity_id.in_(by_id.keys())
                ),
            )
            .order_by(GraphEdge.weight.desc())
            .limit(MAX_GRAPH_DIGEST_LINES * 2)
        )
    )
    neighbor_ids = {edge.from_entity_id for edge in edges} | {
        edge.to_entity_id for edge in edges
    }
    missing = neighbor_ids - set(by_id.keys())
    if missing:
        for entity in db.scalars(
            select(GraphEntity).where(GraphEntity.id.in_(missing))
        ):
            by_id[entity.id] = entity
    digest: List[str] = []
    for edge in edges[:MAX_GRAPH_DIGEST_LINES]:
        left = by_id.get(edge.from_entity_id)
        right = by_id.get(edge.to_entity_id)
        if left is None or right is None:
            continue
        digest.append(f"{left.name} —{edge.relation}({edge.weight})— {right.name}")
    return digest


def recall(
    db: Session,
    *,
    workspace_id: str,
    conversation_id: str,
    query: str,
    settings: Optional[Settings] = None,
) -> MemoryContext:
    settings = settings or get_settings()
    if not settings.memory_enabled:
        return MemoryContext()
    items = list(
        db.scalars(
            select(MemoryItem).where(
                MemoryItem.workspace_id == workspace_id,
                MemoryItem.status == "active",
            )
        )
    )
    if not items:
        return MemoryContext(graph_digest=_graph_digest(db, workspace_id, query))

    query_terms = set(tokenize(query))
    query_vector: Optional[List[float]] = None
    try:
        embedded = embed_texts([query], settings)
    except Exception:
        embedded = None
    if embedded:
        query_vector = unpack_vector(embedded[0])

    scored = []
    for item in items:
        if item.kind == "summary" and item.conversation_id == conversation_id:
            # The rolling summary of the current conversation is always relevant.
            scored.append((float("inf"), item))
            continue
        item_terms = set(tokenize(item.content))
        lexical = len(query_terms & item_terms) / max(1, len(query_terms))
        semantic = 0.0
        if query_vector is not None and item.embedding is not None:
            semantic = max(0.0, cosine_similarity(query_vector, unpack_vector(item.embedding)))
        score = lexical + semantic + min(item.importance, 5) * 0.05
        if lexical > 0 or semantic > 0.3:
            scored.append((score, item))
    scored.sort(key=lambda pair: -pair[0])
    selected = [item for _score, item in scored[: settings.memory_recall_limit]]
    return MemoryContext(
        items=selected,
        graph_digest=_graph_digest(db, workspace_id, query),
    )


def render_memory_context(context: MemoryContext) -> str:
    lines: List[str] = []
    for item in context.items:
        lines.append(f"- ({item.kind}) {item.content}")
    if context.graph_digest:
        lines.append("Known entities & relations:")
        lines.extend("- " + line for line in context.graph_digest)
    return "\n".join(lines)
