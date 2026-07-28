from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from garminconnect import Garmin  # type: ignore[import-untyped]
from sqlalchemy.orm import Session

from ...config import Settings, get_settings
from ...models import IntegrationAccount, SyncJob
from ..crypto import decrypt_secret, encrypt_secret
from ..llm_tools import ToolContext, ToolResult, ToolSpec
from .base import ConnectorError
from .landing import upsert_dataset

SYNC_ACTIVITY_LIMIT = 200


def _reject_mfa() -> str:
    raise ConnectorError(
        "Garmin accounts with multi-factor authentication are not supported yet"
    )


def login_with_credentials(email: str, password: str) -> Garmin:
    """Log in via the unofficial Garmin Connect API (python-garminconnect).

    Never prompts: MFA-protected accounts are rejected instead of hanging the
    request on stdin.
    """
    client = Garmin(email, password)
    try:
        client.garth.login(email, password, prompt_mfa=_reject_mfa)
    except ConnectorError:
        raise
    except Exception as exc:
        raise ConnectorError(f"Garmin login failed: {str(exc)[:200]}") from exc
    client.display_name = client.garth.profile["displayName"]
    client.full_name = client.garth.profile["fullName"]
    return client


def session_token(client: Garmin) -> str:
    return client.garth.dumps()


def client_from_account(
    db: Session,
    account: IntegrationAccount,
    settings: Optional[Settings] = None,
) -> Garmin:
    settings = settings or get_settings()
    if not account.refresh_token_enc:
        raise ConnectorError("Garmin session is missing; reconnect the account")
    token = decrypt_secret(account.refresh_token_enc, settings)
    client = Garmin()
    try:
        client.garth.loads(token)
        client.display_name = client.garth.profile["displayName"]
        client.full_name = client.garth.profile["fullName"]
    except Exception as exc:
        account.status = "error"
        db.commit()
        raise ConnectorError("Garmin session expired; reconnect the account") from exc
    return client


def persist_session(
    db: Session,
    account: IntegrationAccount,
    client: Garmin,
    settings: Optional[Settings] = None,
) -> None:
    """Garth refreshes OAuth tokens internally; keep the stored session current."""
    settings = settings or get_settings()
    account.refresh_token_enc = encrypt_secret(session_token(client), settings)
    db.commit()


def _activity_row(activity: Dict[str, Any]) -> Dict[str, Any]:
    activity_type = activity.get("activityType") or {}
    return {
        "activity_id": str(activity.get("activityId", "")),
        "name": str(activity.get("activityName") or "")[:200],
        "type": str(activity_type.get("typeKey") or ""),
        "start_time": str(activity.get("startTimeLocal") or ""),
        "distance_m": float(activity.get("distance") or 0),
        "duration_s": float(activity.get("duration") or 0),
        "elevation_gain_m": float(activity.get("elevationGain") or 0),
        "average_hr": float(activity.get("averageHR") or 0),
        "max_hr": float(activity.get("maxHR") or 0),
        "calories": float(activity.get("calories") or 0),
    }


def sync(db: Session, account: IntegrationAccount, job: SyncJob) -> Dict[str, Any]:
    client = client_from_account(db, account)
    try:
        activities = client.get_activities(0, SYNC_ACTIVITY_LIMIT)
    except Exception as exc:
        raise ConnectorError(f"Garmin activities fetch failed: {str(exc)[:200]}") from exc
    persist_session(db, account, client)
    if not activities:
        return {"activities": 0}
    rows = [_activity_row(activity) for activity in activities]
    upsert_dataset(
        db,
        workspace_id=account.workspace_id,
        user_id=account.user_id,
        name="garmin-activities",
        rows=rows,
    )
    return {"activities": len(rows)}


def _list_executor(account_id: str):
    def executor(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
        account = db.get(IntegrationAccount, account_id)
        if account is None or account.workspace_id != context.workspace_id:
            return ToolResult(content="Error: Garmin account is no longer connected.")
        limit = min(int(args.get("limit") or 10), 30)
        try:
            client = client_from_account(db, account)
            activities = client.get_activities(0, limit)
        except ConnectorError as exc:
            return ToolResult(content=f"Garmin error: {exc}")
        except Exception as exc:
            return ToolResult(content=f"Garmin fetch failed: {str(exc)[:200]}")
        rows: List[Dict[str, Any]] = [
            _activity_row(activity) for activity in activities[:limit]
        ]
        return ToolResult(content=json.dumps({"activities": rows}))

    return executor


def garmin_tools(account: IntegrationAccount) -> Dict[str, ToolSpec]:
    return {
        "garmin_list_activities": ToolSpec(
            name="garmin_list_activities",
            description=(
                "List recent activities from the connected Garmin Connect account "
                "(read-only, unofficial API)."
            ),
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
            executor=_list_executor(account.id),
        ),
    }
