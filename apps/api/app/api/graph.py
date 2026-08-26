from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import GraphEdge, GraphEntity, GraphProjection
from ..schemas import GraphEdgeOut, GraphEntityOut, GraphOut
from ..services.graph import rebuild_graph
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key
from .ratelimit import rate_limit

router = APIRouter(prefix="/api/graph", tags=["graph"])


def _graph_out(db: Session, workspace_id: str, limit: int) -> GraphOut:
    projection = db.scalar(
        select(GraphProjection).where(GraphProjection.workspace_id == workspace_id)
    )
    entities = list(
        db.scalars(
            select(GraphEntity)
            .where(GraphEntity.workspace_id == workspace_id)
            .order_by(GraphEntity.mention_count.desc(), GraphEntity.name.asc())
            .limit(limit)
        )
    )
    entity_ids = {entity.id for entity in entities}
    edges: List[GraphEdge] = []
    if entity_ids:
        edges = list(
            db.scalars(
                select(GraphEdge)
                .where(
                    GraphEdge.workspace_id == workspace_id,
                    GraphEdge.from_entity_id.in_(entity_ids),
                    GraphEdge.to_entity_id.in_(entity_ids),
                )
                .order_by(GraphEdge.weight.desc())
                .limit(limit * 4)
            )
        )
    return GraphOut(
        status=projection.status if projection else "empty",
        version=projection.version if projection else "",
        built_at=projection.built_at if projection else None,
        entities=[
            GraphEntityOut(
                id=entity.id,
                name=entity.name,
                entity_type=entity.entity_type,
                mention_count=entity.mention_count,
                source_ids=json.loads(entity.source_ids_json),
                chunk_ids=json.loads(entity.chunk_ids_json),
                memory_ids=json.loads(entity.memory_ids_json),
            )
            for entity in entities
        ],
        edges=[
            GraphEdgeOut(
                id=edge.id,
                from_entity_id=edge.from_entity_id,
                to_entity_id=edge.to_entity_id,
                relation=edge.relation,
                weight=edge.weight,
                source_ids=json.loads(edge.source_ids_json),
                chunk_ids=json.loads(edge.chunk_ids_json),
                memory_ids=json.loads(edge.memory_ids_json),
            )
            for edge in edges
        ],
    )


@router.get("", response_model=GraphOut)
def get_graph(
    limit: int = Query(default=100, ge=1, le=200),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> GraphOut:
    return _graph_out(db, actor.workspace_id, limit)


@router.post(
    "/rebuild",
    response_model=GraphOut,
    status_code=202,
    dependencies=[Depends(rate_limit("graph-rebuild", tier="heavy"))],
)
def request_graph_rebuild(
    background_tasks: BackgroundTasks,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> GraphOut:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="graph.rebuild",
        key=key,
    )
    if replay is not None:
        return _graph_out(db, actor.workspace_id, 100)
    projection = db.scalar(
        select(GraphProjection).where(GraphProjection.workspace_id == actor.workspace_id)
    )
    if projection is None:
        projection = GraphProjection(workspace_id=actor.workspace_id, status="queued")
        db.add(projection)
        db.flush()
    else:
        projection.status = "queued"
        projection.error = ""
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="graph.rebuild",
        key=key,
        resource_id=projection.id,
    )
    db.commit()
    background_tasks.add_task(rebuild_graph, actor.workspace_id, actor.user_id)
    return _graph_out(db, actor.workspace_id, 100)
