from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, Iterable, List, Set, Tuple

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import Chunk, GraphEdge, GraphEntity, GraphProjection, MemoryItem, Source
from .audit import record_audit

ENTITY_PATTERN = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9&'’-]{1,})(?:[ \t]+(?:[A-Z][A-Za-z0-9&'’-]{1,})){0,3}\b"
    r"|\b[A-Z]{2,8}\b"
)
IGNORED_ENTITIES = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "it",
    "of",
    "on",
    "or",
    "our",
    "the",
    "this",
    "to",
    "we",
    "with",
}
ORGANIZATION_SUFFIXES = {"inc", "labs", "lab", "llc", "ltd", "company", "corp", "team"}
MAX_ENTITIES_PER_CHUNK = 12
MAX_PROVENANCE_IDS = 100


def _normalized(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip(" .,:;()[]{}").casefold()


def _entity_type(name: str) -> str:
    words = {_normalized(part) for part in name.split()}
    if name.isupper() or words.intersection(ORGANIZATION_SUFFIXES):
        return "organization"
    if name.lower().startswith("project "):
        return "project"
    if len(name.split()) >= 2:
        return "named_entity"
    return "concept"


def extract_entities(text: str) -> List[Tuple[str, str]]:
    found: Dict[str, str] = {}
    for match in ENTITY_PATTERN.finditer(text):
        display = re.sub(r"\s+", " ", match.group(0)).strip()
        normalized = _normalized(display)
        if normalized in IGNORED_ENTITIES or len(normalized) < 2:
            continue
        if display[0].isupper() and match.start() == 0 and " " not in display:
            # A sentence-leading common word is weak evidence until it repeats elsewhere.
            display = display.strip()
        found.setdefault(normalized, display)
    return [(normalized, display) for normalized, display in found.items()]


def _projection(db: Session, workspace_id: str) -> GraphProjection:
    projection = db.scalar(
        select(GraphProjection).where(GraphProjection.workspace_id == workspace_id)
    )
    if projection is None:
        projection = GraphProjection(workspace_id=workspace_id)
        db.add(projection)
        db.flush()
    return projection


def mark_graph_stale(db: Session, workspace_id: str) -> None:
    projection = _projection(db, workspace_id)
    if projection.status != "building":
        projection.status = "stale"


def _bounded(values: Iterable[str]) -> List[str]:
    return sorted(set(values))[:MAX_PROVENANCE_IDS]


def rebuild_graph(workspace_id: str, actor_id: str) -> None:
    db = SessionLocal()
    try:
        projection = _projection(db, workspace_id)
        projection.status = "building"
        projection.error = ""
        db.commit()

        rows = db.execute(
            select(Chunk, Source)
            .join(Source, Source.id == Chunk.source_id)
            .where(
                Chunk.workspace_id == workspace_id,
                Source.workspace_id == workspace_id,
                Source.deleted_at.is_(None),
                Source.status == "ready",
            )
            .order_by(Source.id, Chunk.ordinal)
        ).all()

        mention_count: Counter[str] = Counter()
        display_names: Dict[str, str] = {}
        entity_sources: Dict[str, Set[str]] = defaultdict(set)
        entity_chunks: Dict[str, Set[str]] = defaultdict(set)
        entity_memories: Dict[str, Set[str]] = defaultdict(set)
        chunk_entities: List[Tuple[Chunk, List[str]]] = []
        version_hasher = hashlib.sha256()

        for chunk, source in rows:
            version_hasher.update(chunk.id.encode())
            version_hasher.update(hashlib.sha256(chunk.content.encode()).digest())
            extracted = extract_entities(chunk.content)
            normalized_names = [name for name, _display in extracted]
            for normalized, display in extracted:
                mention_count[normalized] += 1
                display_names.setdefault(normalized, display)
                entity_sources[normalized].add(source.id)
                entity_chunks[normalized].add(chunk.id)
            chunk_entities.append((chunk, normalized_names))

        memory_items = list(
            db.scalars(
                select(MemoryItem).where(
                    MemoryItem.workspace_id == workspace_id,
                    MemoryItem.status == "active",
                )
            )
        )
        memory_entities: List[Tuple[MemoryItem, List[str]]] = []
        memory_accepted: Set[str] = set()
        for item in memory_items:
            version_hasher.update(item.id.encode())
            version_hasher.update(hashlib.sha256(item.content.encode()).digest())
            extracted = extract_entities(item.content)
            curated = [
                (_normalized(name), name)
                for name in json.loads(item.entity_names_json)
                if _normalized(name) and _normalized(name) not in IGNORED_ENTITIES
            ]
            merged: Dict[str, str] = {}
            for normalized, display in extracted + curated:
                merged.setdefault(normalized, display)
            names = list(merged)[:MAX_ENTITIES_PER_CHUNK]
            for normalized in names:
                mention_count[normalized] += 1
                display_names.setdefault(normalized, merged[normalized])
                entity_memories[normalized].add(item.id)
            # Curated names come from the memory writer, so accept them outright.
            memory_accepted.update(normalized for normalized, _display in curated)
            memory_entities.append((item, names))

        accepted = {
            name
            for name, count in mention_count.items()
            if count >= 2
            or " " in display_names[name]
            or display_names[name].isupper()
        } | memory_accepted

        db.execute(delete(GraphEdge).where(GraphEdge.workspace_id == workspace_id))
        db.execute(delete(GraphEntity).where(GraphEntity.workspace_id == workspace_id))
        db.flush()

        entities: Dict[str, GraphEntity] = {}
        for normalized in sorted(accepted):
            entity = GraphEntity(
                workspace_id=workspace_id,
                name=display_names[normalized],
                normalized_name=normalized,
                entity_type=_entity_type(display_names[normalized]),
                mention_count=mention_count[normalized],
                source_ids_json=json.dumps(_bounded(entity_sources[normalized])),
                chunk_ids_json=json.dumps(_bounded(entity_chunks[normalized])),
                memory_ids_json=json.dumps(_bounded(entity_memories[normalized])),
            )
            db.add(entity)
            entities[normalized] = entity
        db.flush()

        edge_counts: Counter[Tuple[str, str]] = Counter()
        edge_sources: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
        edge_chunks: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
        edge_memories: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
        for chunk, names in chunk_entities:
            names = sorted(set(name for name in names if name in accepted))[
                :MAX_ENTITIES_PER_CHUNK
            ]
            for left, right in itertools.combinations(names, 2):
                key = (left, right)
                edge_counts[key] += 1
                edge_sources[key].add(chunk.source_id)
                edge_chunks[key].add(chunk.id)
        for item, names in memory_entities:
            names = sorted(set(name for name in names if name in accepted))[
                :MAX_ENTITIES_PER_CHUNK
            ]
            for left, right in itertools.combinations(names, 2):
                key = (left, right)
                edge_counts[key] += 1
                edge_memories[key].add(item.id)

        for (left, right), weight in edge_counts.items():
            db.add(
                GraphEdge(
                    workspace_id=workspace_id,
                    from_entity_id=entities[left].id,
                    to_entity_id=entities[right].id,
                    relation="co_occurs",
                    weight=weight,
                    source_ids_json=json.dumps(_bounded(edge_sources[(left, right)])),
                    chunk_ids_json=json.dumps(_bounded(edge_chunks[(left, right)])),
                    memory_ids_json=json.dumps(_bounded(edge_memories[(left, right)])),
                )
            )

        projection = _projection(db, workspace_id)
        projection.status = "ready"
        projection.version = version_hasher.hexdigest()
        projection.entity_count = len(entities)
        projection.edge_count = len(edge_counts)
        projection.error = ""
        projection.built_at = datetime.utcnow()
        record_audit(
            db,
            workspace_id=workspace_id,
            actor_id=actor_id,
            action="graph.rebuilt",
            resource_type="graph_projection",
            resource_id=projection.id,
            detail={
                "entities": projection.entity_count,
                "edges": projection.edge_count,
                "version": projection.version,
            },
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        projection = _projection(db, workspace_id)
        projection.status = "failed"
        projection.error = str(exc)[:1000]
        db.commit()
    finally:
        db.close()
