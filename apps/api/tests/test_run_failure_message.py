"""What a failed run is allowed to tell the person who was waiting for it.

`_fail_run` catches every exception an agent turn can raise, and it used to put
`str(exc)` straight onto `run.error`. That field is published twice — into the
`run.failed` event the browser streams, and into the member-facing Inbox — so a
driver error became SQL, bound parameters and row ids on a user's screen. A live
probe caught exactly that: four turns died mid-sentence and the text delivered
to the client opened

    (sqlite3.OperationalError) database is locked [SQL: INSERT INTO run_events
    (id, workspace_id, run_id, sequence, event_type, payload_json, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)] [parameters: ('0161a432-…', …)]

These tests pin the rule in both directions, because a blanket "something went
wrong" would have been its own regression: `OrgBoundExceeded` exists precisely so
a user is told their organization disallowed the model rather than being sent to
debug a failure that never happened.
"""

from __future__ import annotations

import json

from conftest import create_identity
from sqlalchemy.exc import OperationalError

from app.database import SessionLocal
from app.models import Conversation, Run, RunEvent
from app.services.agent_loop import OrgBoundExceeded
from app.services.errors import UserFacingError, user_facing_message
from app.services.runs import _fail_run


def _run_for(identity) -> str:
    """One queued run belonging to a fresh tenant."""
    db = SessionLocal()
    try:
        conversation = Conversation(
            workspace_id=identity.workspace_id,
            created_by=identity.user_id,
            title="failure thread",
        )
        db.add(conversation)
        db.flush()
        run = Run(
            workspace_id=identity.workspace_id,
            conversation_id=conversation.id,
            agent_id="",
            created_by=identity.user_id,
            status="running",
            prompt="prompt",
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _fail_with(run_id: str, exc: Exception) -> tuple[str, str]:
    """Fail the run, and report what the row and the streamed event now say."""
    db = SessionLocal()
    try:
        _fail_run(db, run_id, exc)
    finally:
        db.close()
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        event = (
            db.query(RunEvent)
            .filter(RunEvent.run_id == run_id, RunEvent.event_type == "run.failed")
            .one()
        )
        return run.error, json.loads(event.payload_json)["error"]
    finally:
        db.close()


def _database_is_locked() -> OperationalError:
    """The real shape of the leak: a driver error carrying SQL and parameters."""
    return OperationalError(
        "INSERT INTO run_events (id, workspace_id, run_id, sequence, event_type,"
        " payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("0161a432-daa4-4f24-885f-d91d9d11e4b2", "00000000-0000-4000-8000-000000000001"),
        Exception("database is locked"),
    )


def test_a_driver_error_never_reaches_the_client() -> None:
    identity = create_identity(name="Leak owner", workspace_name="Leak workspace")
    run_id = _run_for(identity)

    stored, streamed = _fail_with(run_id, _database_is_locked())

    # Both publication paths carry the same safe text.
    assert stored == streamed
    # Nothing about the query, the parameters, or the rows survives.
    for leaked in ("INSERT INTO", "run_events", "VALUES", "0161a432", "sqlite3"):
        assert leaked not in stored, f"{leaked!r} leaked into run.error"
    # And what is said is useful: the run id is the handle support needs.
    assert run_id in stored
    assert "could not finish" in stored


def test_a_message_written_for_a_person_passes_through() -> None:
    identity = create_identity(name="Bound owner", workspace_name="Bound workspace")
    run_id = _run_for(identity)

    stored, streamed = _fail_with(
        run_id, OrgBoundExceeded("your organization does not allow this model")
    )

    assert stored == streamed
    assert stored == "your organization does not allow this model"
    # Specifically NOT replaced by the generic sentence — that substitution is
    # the regression this exception type exists to prevent.
    assert run_id not in stored


def test_the_rule_is_the_type_not_the_text() -> None:
    """A plain exception is generic however friendly its message reads.

    Written down because the tempting implementation is a sanitiser that greps
    for SQL. There is no reliable way to scrub identifiers out of arbitrary
    exception text, so opting in by type is the whole design.
    """

    class Polite(Exception):
        pass

    class Rude(UserFacingError):
        pass

    assert "run-7" in user_facing_message(Polite("looks harmless"), run_id="run-7")
    assert user_facing_message(Rude("say this"), run_id="run-7") == "say this"
    # An opted-in exception with nothing to say still gets the honest fallback
    # rather than an empty error.
    assert "run-7" in user_facing_message(Rude(""), run_id="run-7")
