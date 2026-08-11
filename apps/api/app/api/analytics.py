from __future__ import annotations

import json
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import (
    Dashboard,
    Dataset,
    DatasetVersion,
    Source,
    new_id,
)
from ..schemas import (
    DashboardCreate,
    DashboardOut,
    DashboardRunOut,
    DashboardSpec,
    DatasetCreate,
    DatasetOut,
    DatasetQuery,
    DatasetQueryResult,
    DatasetVersionCreate,
)
from ..services.analytics import (
    AnalyticsValidationError,
    create_dataset_version,
    current_dataset_version,
    execute_dataset_query,
)
from ..services.audit import record_audit
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api", tags=["analytics"])


def _dataset_out(dataset: Dataset, version: DatasetVersion) -> DatasetOut:
    return DatasetOut(
        id=dataset.id,
        name=dataset.name,
        description=dataset.description,
        current_version=dataset.current_version,
        version_id=version.id,
        source_id=version.source_id,
        format=version.format,
        columns=json.loads(version.schema_json),
        row_count=version.row_count,
        content_hash=version.content_hash,
        created_at=dataset.created_at,
        updated_at=dataset.updated_at,
    )


def _dashboard_out(dashboard: Dashboard) -> DashboardOut:
    return DashboardOut(
        id=dashboard.id,
        name=dashboard.name,
        description=dashboard.description,
        dataset_id=dashboard.dataset_id,
        spec=DashboardSpec.model_validate(json.loads(dashboard.spec_json)),
        created_at=dashboard.created_at,
        updated_at=dashboard.updated_at,
    )


def _source_for_dataset(db: Session, actor: Actor, source_id: str) -> Source:
    source = db.scalar(
        select(Source).where(
            Source.id == source_id,
            Source.workspace_id == actor.workspace_id,
            Source.deleted_at.is_(None),
            Source.status == "ready",
        )
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Ready source not found")
    if Path(source.filename).suffix.lower() not in {".csv", ".json"}:
        raise HTTPException(
            status_code=422,
            detail="Datasets can only be created from CSV or JSON sources",
        )
    return source


@router.get("/datasets", response_model=List[DatasetOut])
def list_datasets(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[DatasetOut]:
    datasets = list(
        db.scalars(
            select(Dataset)
            .where(Dataset.workspace_id == actor.workspace_id)
            .order_by(Dataset.updated_at.desc())
        )
    )
    result: List[DatasetOut] = []
    for dataset in datasets:
        _item, version = current_dataset_version(
            db,
            workspace_id=actor.workspace_id,
            dataset_id=dataset.id,
        )
        result.append(_dataset_out(dataset, version))
    return result


@router.post("/datasets", response_model=DatasetOut, status_code=201)
def create_dataset(
    payload: DatasetCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DatasetOut:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="dataset.create",
        key=key,
    )
    if replay:
        try:
            dataset, version = current_dataset_version(
                db,
                workspace_id=actor.workspace_id,
                dataset_id=replay.resource_id,
            )
        except AnalyticsValidationError:
            # The dataset this key bought has been deleted since. Letting the
            # lookup raise here answered a legitimate retry with a 500.
            raise replayed_resource_gone() from None
        return _dataset_out(dataset, version)
    name = payload.name.strip()
    existing = db.scalar(
        select(Dataset).where(
            Dataset.workspace_id == actor.workspace_id,
            Dataset.name == name,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Dataset name already exists")
    source = _source_for_dataset(db, actor, payload.source_id)
    dataset = Dataset(
        id=new_id(),
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        name=name,
        description=payload.description.strip(),
        current_version=1,
    )
    db.add(dataset)
    db.flush()
    try:
        version = create_dataset_version(
            db,
            dataset=dataset,
            source=source,
            version=1,
        )
    except AnalyticsValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="dataset.create",
        key=key,
        resource_id=dataset.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="dataset.created",
        resource_type="dataset",
        resource_id=dataset.id,
        detail={
            "name": dataset.name,
            "version": 1,
            "rows": version.row_count,
            "source_id": source.id,
        },
    )
    db.commit()
    db.refresh(dataset)
    db.refresh(version)
    return _dataset_out(dataset, version)


@router.post("/datasets/{dataset_id}/versions", response_model=DatasetOut, status_code=201)
def create_version(
    dataset_id: str,
    payload: DatasetVersionCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DatasetOut:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation=f"dataset.version:{dataset_id}",
        key=key,
    )
    # A dataset id from another workspace — or one that never existed — has to
    # come back as 404. Left unhandled this raised out of the route as a 500,
    # which is an attacker-controlled path to an unhandled error.
    try:
        dataset, current = current_dataset_version(
            db,
            workspace_id=actor.workspace_id,
            dataset_id=dataset_id,
        )
    except AnalyticsValidationError as exc:
        status = 404 if str(exc) == "Dataset not found" else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    if replay:
        return _dataset_out(dataset, current)
    source = _source_for_dataset(db, actor, payload.source_id)
    next_version = dataset.current_version + 1
    try:
        version = create_dataset_version(
            db,
            dataset=dataset,
            source=source,
            version=next_version,
        )
    except AnalyticsValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    dataset.current_version = next_version
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation=f"dataset.version:{dataset_id}",
        key=key,
        resource_id=version.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="dataset.version_created",
        resource_type="dataset",
        resource_id=dataset.id,
        detail={
            "version": next_version,
            "rows": version.row_count,
            "source_id": source.id,
        },
    )
    db.commit()
    db.refresh(dataset)
    db.refresh(version)
    return _dataset_out(dataset, version)


@router.post("/datasets/{dataset_id}/query", response_model=DatasetQueryResult)
def query_dataset(
    dataset_id: str,
    payload: DatasetQuery,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DatasetQueryResult:
    try:
        return execute_dataset_query(
            db,
            workspace_id=actor.workspace_id,
            dataset_id=dataset_id,
            query=payload,
        )
    except AnalyticsValidationError as exc:
        status = 404 if str(exc) == "Dataset not found" else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/dashboards", response_model=List[DashboardOut])
def list_dashboards(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[DashboardOut]:
    dashboards = db.scalars(
        select(Dashboard)
        .where(Dashboard.workspace_id == actor.workspace_id)
        .order_by(Dashboard.updated_at.desc())
    )
    return [_dashboard_out(item) for item in dashboards]


@router.post("/dashboards", response_model=DashboardOut, status_code=201)
def create_dashboard(
    payload: DashboardCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DashboardOut:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="dashboard.create",
        key=key,
    )
    if replay:
        dashboard = db.scalar(
            select(Dashboard).where(
                Dashboard.id == replay.resource_id,
                Dashboard.workspace_id == actor.workspace_id,
            )
        )
        if dashboard is None:
            raise replayed_resource_gone()
        return _dashboard_out(dashboard)
    name = payload.name.strip()
    existing = db.scalar(
        select(Dashboard).where(
            Dashboard.workspace_id == actor.workspace_id,
            Dashboard.name == name,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Dashboard name already exists")
    try:
        result = execute_dataset_query(
            db,
            workspace_id=actor.workspace_id,
            dataset_id=payload.dataset_id,
            query=payload.spec.query,
        )
    except AnalyticsValidationError as exc:
        status = 404 if str(exc) == "Dataset not found" else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    result_fields = set(result.columns)
    selected_fields = set(payload.spec.y_fields)
    if payload.spec.x_field:
        selected_fields.add(payload.spec.x_field)
    unknown = selected_fields - result_fields
    if unknown:
        raise HTTPException(
            status_code=422,
            detail="Unknown visualization fields: " + ", ".join(sorted(unknown)),
        )
    dashboard = Dashboard(
        id=new_id(),
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        dataset_id=payload.dataset_id,
        name=name,
        description=payload.description.strip(),
        spec_json=payload.spec.model_dump_json(),
    )
    db.add(dashboard)
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="dashboard.create",
        key=key,
        resource_id=dashboard.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="dashboard.created",
        resource_type="dashboard",
        resource_id=dashboard.id,
        detail={"name": dashboard.name, "dataset_id": dashboard.dataset_id},
    )
    db.commit()
    db.refresh(dashboard)
    return _dashboard_out(dashboard)


@router.post("/dashboards/{dashboard_id}/run", response_model=DashboardRunOut)
def run_dashboard(
    dashboard_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DashboardRunOut:
    dashboard = db.scalar(
        select(Dashboard).where(
            Dashboard.id == dashboard_id,
            Dashboard.workspace_id == actor.workspace_id,
        )
    )
    if dashboard is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    spec = DashboardSpec.model_validate(json.loads(dashboard.spec_json))
    try:
        result = execute_dataset_query(
            db,
            workspace_id=actor.workspace_id,
            dataset_id=dashboard.dataset_id,
            query=spec.query,
        )
    except AnalyticsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return DashboardRunOut(dashboard=_dashboard_out(dashboard), result=result)
