from __future__ import annotations

import json
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Dataset, Source
from ..analytics import create_dataset_version
from ..ingestion import ingest_source, object_path, sanitize_filename


def create_text_source(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    filename: str,
    text: str,
    media_type: str,
) -> Source:
    """Land connector-produced text as a Source and run the normal ingestion."""
    source = Source(
        workspace_id=workspace_id,
        created_by=user_id,
        filename=sanitize_filename(filename),
        media_type=media_type,
        object_key="",
        byte_size=0,
        status="queued",
    )
    db.add(source)
    db.flush()
    path = object_path(workspace_id, source.id, source.filename)
    data = text.encode("utf-8")
    path.write_bytes(data)
    source.object_key = str(path)
    source.byte_size = len(data)
    db.commit()
    ingest_source(source.id, user_id)
    db.refresh(source)
    return source


def upsert_dataset(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    name: str,
    rows: List[Dict[str, Any]],
) -> Dataset:
    """Create or version a dataset from connector rows via a JSON source."""
    source = create_text_source(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        filename=f"{name}.json",
        text=json.dumps(rows, ensure_ascii=False, default=str),
        media_type="application/json",
    )
    dataset = db.scalar(
        select(Dataset).where(
            Dataset.workspace_id == workspace_id,
            Dataset.name == name,
        )
    )
    if dataset is None:
        dataset = Dataset(
            workspace_id=workspace_id,
            created_by=user_id,
            name=name,
            description=f"Synced from the {name.split('-')[0]} integration.",
        )
        db.add(dataset)
        db.flush()
        create_dataset_version(db, dataset=dataset, source=source, version=1)
    else:
        next_version = dataset.current_version + 1
        create_dataset_version(db, dataset=dataset, source=source, version=next_version)
        dataset.current_version = next_version
    db.commit()
    return dataset
