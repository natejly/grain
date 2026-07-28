from __future__ import annotations

import json
from datetime import datetime

from ...database import SessionLocal
from ...models import IntegrationAccount, SyncJob
from ..audit import record_audit
from . import garmin, gmail, strava
from .base import ConnectorError

CONNECTOR_FOR_PROVIDER = {
    "google": "gmail",
    "strava": "strava_activities",
    "garmin": "garmin_activities",
}


def run_sync_job(job_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(SyncJob, job_id)
        if job is None or job.status != "queued":
            return
        account = db.get(IntegrationAccount, job.account_id)
        if account is None:
            job.status = "failed"
            job.error = "Integration account no longer exists"
            job.finished_at = datetime.utcnow()
            db.commit()
            return
        job.status = "running"
        job.started_at = datetime.utcnow()
        db.commit()
        try:
            if job.connector == "gmail":
                stats = gmail.sync(db, account, job)
            elif job.connector == "strava_activities":
                stats = strava.sync(db, account, job)
            elif job.connector == "garmin_activities":
                stats = garmin.sync(db, account, job)
            else:
                raise ConnectorError(f"Unknown connector: {job.connector}")
            job = db.get(SyncJob, job_id) or job
            job.status = "succeeded"
            job.stats_json = json.dumps(stats)
            account = db.get(IntegrationAccount, job.account_id) or account
            account.last_sync_at = datetime.utcnow()
            record_audit(
                db,
                workspace_id=job.workspace_id,
                actor_id=account.user_id,
                action="integration.synced",
                resource_type="sync_job",
                resource_id=job.id,
                detail={"connector": job.connector, **stats},
            )
        except Exception as exc:
            db.rollback()
            job = db.get(SyncJob, job_id)
            if job is None:
                return
            job.status = "failed"
            job.error = str(exc)[:1000]
        job.finished_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()
