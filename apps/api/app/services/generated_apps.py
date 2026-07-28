from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Dashboard
from ..schemas import DashboardSpec
from .analytics import AnalyticsValidationError, execute_dataset_query

MAX_APP_DASHBOARDS = 12


class GeneratedAppValidationError(ValueError):
    pass


def build_release_manifest(
    db: Session,
    *,
    workspace_id: str,
    dashboard_ids: List[str],
) -> Tuple[Dict[str, Any], str]:
    ordered_ids = list(dict.fromkeys(dashboard_ids))
    if not ordered_ids or len(ordered_ids) > MAX_APP_DASHBOARDS:
        raise GeneratedAppValidationError(
            f"An app release requires 1–{MAX_APP_DASHBOARDS} dashboards"
        )
    dashboards = list(
        db.scalars(
            select(Dashboard).where(
                Dashboard.workspace_id == workspace_id,
                Dashboard.id.in_(ordered_ids),
            )
        )
    )
    by_id = {dashboard.id: dashboard for dashboard in dashboards}
    if any(dashboard_id not in by_id for dashboard_id in ordered_ids):
        raise GeneratedAppValidationError("One or more dashboards are not available")

    snapshots: List[Dict[str, Any]] = []
    for dashboard_id in ordered_ids:
        dashboard = by_id[dashboard_id]
        spec = DashboardSpec.model_validate(json.loads(dashboard.spec_json))
        try:
            result = execute_dataset_query(
                db,
                workspace_id=workspace_id,
                dataset_id=dashboard.dataset_id,
                query=spec.query,
            )
        except AnalyticsValidationError as exc:
            raise GeneratedAppValidationError(
                f"Dashboard {dashboard.name} could not be snapshotted"
            ) from exc
        snapshots.append(
            {
                "id": dashboard.id,
                "name": dashboard.name,
                "description": dashboard.description,
                "visualization": spec.visualization,
                "x_field": spec.x_field,
                "y_fields": spec.y_fields,
                "result": result.model_dump(mode="json"),
            }
        )
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "dashboards": snapshots,
    }
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return manifest, hashlib.sha256(canonical.encode()).hexdigest()
