from __future__ import annotations

import json
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from ...models import IntegrationAccount, SyncJob
from ..llm_tools import ToolContext, ToolResult, ToolSpec
from .base import ConnectorError, ensure_access_token, http_client
from .landing import upsert_dataset

STRAVA_API = "https://www.strava.com/api/v3"
SYNC_PAGE_SIZE = 100
SYNC_MAX_PAGES = 2


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _activity_row(activity: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "activity_id": str(activity.get("id", "")),
        "name": str(activity.get("name", ""))[:200],
        "type": str(activity.get("sport_type") or activity.get("type") or ""),
        "start_date": str(activity.get("start_date_local") or activity.get("start_date") or ""),
        "distance_m": float(activity.get("distance") or 0),
        "moving_time_s": int(activity.get("moving_time") or 0),
        "elapsed_time_s": int(activity.get("elapsed_time") or 0),
        "elevation_gain_m": float(activity.get("total_elevation_gain") or 0),
        "average_heartrate": float(activity.get("average_heartrate") or 0),
        "average_speed_ms": float(activity.get("average_speed") or 0),
    }


def sync(db: Session, account: IntegrationAccount, job: SyncJob) -> Dict[str, Any]:
    token = ensure_access_token(db, account)
    activities: List[Dict[str, Any]] = []
    with http_client() as client:
        for page in range(1, SYNC_MAX_PAGES + 1):
            response = client.get(
                f"{STRAVA_API}/athlete/activities",
                params={"per_page": SYNC_PAGE_SIZE, "page": page},
                headers=_headers(token),
            )
            if response.status_code != 200:
                raise ConnectorError(
                    f"Strava activities fetch failed ({response.status_code})"
                )
            batch = response.json()
            activities.extend(batch)
            if len(batch) < SYNC_PAGE_SIZE:
                break
    if not activities:
        return {"activities": 0}
    rows = [_activity_row(activity) for activity in activities]
    upsert_dataset(
        db,
        workspace_id=account.workspace_id,
        user_id=account.user_id,
        name="strava-activities",
        rows=rows,
    )
    return {"activities": len(rows)}


def _list_executor(account_id: str):
    def executor(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
        account = db.get(IntegrationAccount, account_id)
        if account is None or account.workspace_id != context.workspace_id:
            return ToolResult(content="Error: Strava account is no longer connected.")
        limit = min(int(args.get("per_page") or 10), 30)
        token = ensure_access_token(db, account)
        with http_client() as client:
            response = client.get(
                f"{STRAVA_API}/athlete/activities",
                params={"per_page": limit, "page": 1},
                headers=_headers(token),
            )
        if response.status_code != 200:
            return ToolResult(content=f"Strava fetch failed ({response.status_code}).")
        rows = [_activity_row(activity) for activity in response.json()[:limit]]
        return ToolResult(content=json.dumps({"activities": rows}))

    return executor


def _get_executor(account_id: str):
    def executor(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
        account = db.get(IntegrationAccount, account_id)
        if account is None or account.workspace_id != context.workspace_id:
            return ToolResult(content="Error: Strava account is no longer connected.")
        activity_id = str(args.get("activity_id") or "").strip()
        if not activity_id:
            return ToolResult(content="Error: activity_id is required.")
        token = ensure_access_token(db, account)
        with http_client() as client:
            response = client.get(
                f"{STRAVA_API}/activities/{activity_id}",
                headers=_headers(token),
            )
        if response.status_code != 200:
            return ToolResult(content=f"Strava fetch failed ({response.status_code}).")
        activity = response.json()
        row = _activity_row(activity)
        row["description"] = str(activity.get("description") or "")[:500]
        return ToolResult(content=json.dumps(row))

    return executor


def strava_tools(account: IntegrationAccount) -> Dict[str, ToolSpec]:
    return {
        "strava_list_activities": ToolSpec(
            name="strava_list_activities",
            description="List recent activities from the connected Strava account (read-only).",
            parameters={
                "type": "object",
                "properties": {"per_page": {"type": "integer"}},
            },
            executor=_list_executor(account.id),
        ),
        "strava_get_activity": ToolSpec(
            name="strava_get_activity",
            description="Fetch one Strava activity by activity_id (read-only).",
            parameters={
                "type": "object",
                "properties": {"activity_id": {"type": "string"}},
                "required": ["activity_id"],
            },
            executor=_get_executor(account.id),
        ),
    }
