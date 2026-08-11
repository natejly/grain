"""Races. Every guarantee the app states in the singular, attacked in parallel.

The suite has no concurrency tests at all, so every "single use", "already
decided", "idempotent" and "at most N" claim in the codebase is currently proved
only against a caller that waits its turn. An attacker does not wait.

How these tests are made deterministic, since "spawn threads and hope" is not a
test:

* every worker opens its **own** `SessionLocal()`. The engine is a `QueuePool`
  with `check_same_thread=False`, so that is a distinct DBAPI connection and a
  distinct transaction — the same shape as two Uvicorn workers.
* a `threading.Barrier` releases all workers at the same instant, so the
  interleaving is forced rather than timing-dependent. There is no `sleep`
  anywhere and nothing reads the wall clock.
* the assertion is always on the **final database state**, never on which thread
  won. "Both threads got a 200" is flaky; "two rows exist where the constraint
  says one" is not.

Tests that record a defect are `xfail(strict=True)`: they fail today, which
keeps CI green, and they turn into a hard failure the moment the defect is
fixed, which is when the xfail should be deleted.
"""
from __future__ import annotations

from typing import Optional, Tuple

import pytest
from conftest import TEST_BASE_URL, authenticate, create_identity
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    AgentToolCall,
    Conversation,
    EmailToken,
    IdempotencyRecord,
    MemoryItem,
    Run,
    RunEvent,
    User,
    UserSession,
)
from app.services.auth import email as email_service
from app.services.auth.passwords import hash_password
from app.services.auth.sessions import resolve_session
from app.services.events import append_event


def session_scope():
    return SessionLocal()


# --------------------------------------------------------------------------
# The email token's single-use guarantee


@pytest.fixture
def resettable_user() -> Tuple[str, str]:
    """A user with a live password-reset token. Returns (user_id, raw token)."""
    identity = create_identity()
    db = session_scope()
    try:
        user = db.get(User, identity.user_id)
        assert user is not None
        user.password_hash = hash_password("original-passw0rd!")
        raw = email_service.issue_email_token(
            db,
            user_id=user.id,
            purpose=email_service.PURPOSE_PASSWORD_RESET,
            ttl=email_service.RESET_TOKEN_TTL,
        )
        db.commit()
    finally:
        db.close()
    return identity.user_id, raw


def test_a_reset_token_is_refused_the_second_time_it_is_used(
    resettable_user: Tuple[str, str],
) -> None:
    """The sequential guarantee, so the concurrent test below means something.

    Deleting `EmailToken.consumed_at.is_(None)` from the `consume_email_token`
    query (email.py:140-146) makes the second call succeed and fails this.
    """
    _user_id, raw = resettable_user
    client = TestClient(app, base_url=TEST_BASE_URL)
    first = client.post(
        "/api/auth/password/reset/confirm",
        json={"token": raw, "password": "first-replacement-1!"},
    )
    second = client.post(
        "/api/auth/password/reset/confirm",
        json={"token": raw, "password": "second-replacement-2!"},
    )
    assert first.status_code == 200
    assert second.status_code == 400


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: consume_email_token (services/auth/email.py:133-151) is a "
        "SELECT for `consumed_at IS NULL` followed by a Python-side "
        "`token.consumed_at = now`, in separate transactions per request. There "
        "is no `UPDATE ... WHERE consumed_at IS NULL` and no unique guard, so "
        "concurrent redemptions of one link all see it unconsumed and all "
        "proceed to set a password. The single-use property that the whole "
        "password-reset design rests on holds only against a serial caller. "
        "Remove the xfail when consumption is a conditional UPDATE whose "
        "rowcount decides."
    ),
)
def test_one_reset_token_cannot_be_consumed_by_two_transactions(
    resettable_user: Tuple[str, str],
) -> None:
    """Two transactions redeem one link, with the interleaving written out.

    Not threads: the two overlapping requests differ from the serial case only
    in that both `SELECT`s land before either `UPDATE` commits, and that is a
    statement about the code, not about the scheduler. Spelling it out makes the
    test say the same thing on every engine and at every load.
    """
    _user_id, raw = resettable_user
    first = session_scope()
    second = session_scope()
    try:
        one = email_service.consume_email_token(
            first, raw_token=raw, purpose=email_service.PURPOSE_PASSWORD_RESET
        )
        two = email_service.consume_email_token(
            second, raw_token=raw, purpose=email_service.PURPOSE_PASSWORD_RESET
        )
        first.commit()
        second.commit()
    finally:
        first.close()
        second.close()

    assert one is not None, "the first redemption should succeed"
    assert two is None, (
        "a second overlapping redemption of a single-use reset link was accepted"
    )


def test_a_consumed_token_leaves_exactly_one_row_marked_consumed(
    resettable_user: Tuple[str, str],
) -> None:
    """Redemption must not mint or lose token rows.

    Separate from the assertions above so that a fix which serialises redemption
    is not also free to, say, delete the row — the audit trail is the row, and
    `issue_email_token` invalidates by UPDATE rather than by DELETE precisely so
    a used link stays visible.
    """
    user_id, raw = resettable_user
    client = TestClient(app, base_url=TEST_BASE_URL)
    for index in range(3):
        client.post(
            "/api/auth/password/reset/confirm",
            json={"token": raw, "password": f"replacement-{index}-9!"},
        )

    db = session_scope()
    try:
        rows = list(
            db.scalars(
                select(EmailToken).where(
                    EmailToken.user_id == user_id,
                    EmailToken.purpose == email_service.PURPOSE_PASSWORD_RESET,
                )
            )
        )
    finally:
        db.close()
    assert len(rows) == 1
    assert rows[0].consumed_at is not None


# --------------------------------------------------------------------------
# Sessions


def test_a_revoked_session_is_dead_on_every_later_request() -> None:
    """Replay of a logged-out cookie, including against the raw resolver.

    Going through `resolve_session` as well as HTTP matters: a cache added in
    front of it later would keep the HTTP assertion passing. Deleting
    `UserSession.revoked_at.is_(None)` from the resolver query
    (services/auth/sessions.py:88) fails this.
    """
    identity = create_identity()
    client = TestClient(app, base_url=TEST_BASE_URL)
    authenticate(client, identity)
    assert client.post("/api/auth/logout").status_code == 204

    replayed = TestClient(app, base_url=TEST_BASE_URL)
    authenticate(replayed, identity)
    assert replayed.get("/api/bootstrap").status_code == 401

    db = session_scope()
    try:
        assert resolve_session(db, identity.token, settings=get_settings()) is None
    finally:
        db.close()


def test_two_overlapping_logouts_leave_one_revocation_and_no_error() -> None:
    """Two transactions revoke the same session.

    `revoke_session` is `if session.revoked_at is None: session.revoked_at = ...`
    — a read-then-write — so this is the shape that would double-write. It must
    converge on one timestamp rather than raising or resurrecting the row.
    """
    identity = create_identity()
    first = session_scope()
    second = session_scope()
    failure = ""
    try:
        from app.services.auth.sessions import revoke_session

        one = resolve_session(first, identity.token, settings=get_settings())
        two = resolve_session(second, identity.token, settings=get_settings())
        assert one is not None and two is not None
        revoke_session(first, one)
        revoke_session(second, two)
        first.commit()
        try:
            second.commit()
        except Exception as exc:  # noqa: BLE001
            failure = type(exc).__name__
            second.rollback()
    finally:
        first.close()
        second.close()

    assert not failure, f"an overlapping logout raised {failure}"
    db = session_scope()
    try:
        rows = list(
            db.scalars(select(UserSession).where(UserSession.user_id == identity.user_id))
        )
        assert len(rows) == 1
        assert rows[0].revoked_at is not None
        # And it really is dead afterwards.
        assert resolve_session(db, identity.token, settings=get_settings()) is None
    finally:
        db.close()


def test_csrf_is_enforced_for_every_shape_of_a_missing_or_wrong_token() -> None:
    """Double-submit, attacked from the outside: absent, empty, wrong, the
    session id, another session's secret, and a non-ASCII header.

    Deleting the `csrf_token_matches` branch in `app/auth.py:231-234` fails this
    on every case.
    """
    victim = create_identity()
    attacker = create_identity()
    settings = get_settings()
    header = settings.csrf_header_name

    def call(csrf: Optional[str]):
        client = TestClient(app, base_url=TEST_BASE_URL)
        client.cookies.set(settings.session_cookie_name, victim.token)
        headers = {"Idempotency-Key": "csrf-probe-0001"}
        if csrf is not None:
            headers[header] = csrf
        return client.post("/api/conversations", json={"title": "x"}, headers=headers)

    for csrf in (
        None,
        "",
        " ",
        "wrong",
        victim.token,  # the session token is not the csrf secret
        victim.user_id,
        attacker.csrf_token,  # another live session's secret
        victim.csrf_token + "a",
        victim.csrf_token[:-1],
    ):
        response = call(csrf)
        assert response.status_code == 403, (
            f"csrf={csrf!r} was accepted with {response.status_code}"
        )

    # And the control is not simply "everything is refused".
    assert call(victim.csrf_token).status_code == 201


# --------------------------------------------------------------------------
# Idempotency keys


def _identity_client() -> TestClient:
    identity = create_identity()
    client = TestClient(app, base_url=TEST_BASE_URL)
    authenticate(client, identity)
    client.identity = identity  # type: ignore[attr-defined]
    return client


def test_replaying_an_idempotency_key_sequentially_creates_one_conversation() -> None:
    """The guarantee the concurrent test below attacks.

    Deleting the `IdempotencyRecord` lookup in `chat.create_conversation`
    (api/chat.py:73-98) fails this with two conversations.
    """
    client = _identity_client()
    headers = {"Idempotency-Key": "seq-replay-0001"}
    first = client.post("/api/conversations", json={"title": "One"}, headers=headers)
    second = client.post("/api/conversations", json={"title": "One"}, headers=headers)
    assert first.status_code == 201
    assert second.status_code in (200, 201)
    assert first.json()["id"] == second.json()["id"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: every idempotency-guarded handler is SELECT-then-INSERT "
        "against `UniqueConstraint('workspace_id','operation','key')` "
        "(models.py:218) and nothing anywhere catches the violation — the only "
        "`except IntegrityError` in app/api is signup (api/auth.py:317) and "
        "main.py installs no exception handler. `create_conversation` "
        "(api/chat.py:73-88) only *returns* on replay when the resource still "
        "exists; once it has been deleted the handler falls through and inserts "
        "the same key again, so the retry an Idempotency-Key exists to make safe "
        "answers 500. The same fall-through is in `send_message` "
        "(api/chat.py:228-296), and it is also what a concurrent replay hits. "
        "Remove the xfail when the violation is answered as a replay."
    ),
)
def test_replaying_a_key_whose_resource_was_deleted_does_not_500() -> None:
    """Deterministic, single-threaded, and the same defect a race would find.

    No barrier needed: deleting the resource reproduces exactly the state a
    losing racer sees — a key that is already recorded and a resource the replay
    branch cannot return.
    """
    identity = create_identity()
    client = TestClient(app, base_url=TEST_BASE_URL, raise_server_exceptions=False)
    authenticate(client, identity)
    headers = {"Idempotency-Key": "replay-after-delete-1"}

    created = client.post("/api/conversations", json={"title": "One"}, headers=headers)
    assert created.status_code == 201
    removed = client.delete(
        f"/api/conversations/{created.json()['id']}",
        headers={"Idempotency-Key": "replay-after-delete-2"},
    )
    assert removed.status_code == 204

    replay = client.post("/api/conversations", json={"title": "One"}, headers=headers)
    assert replay.status_code != 500, (
        f"replaying a recorded idempotency key answered {replay.status_code}"
    )


def test_a_key_cannot_be_recorded_twice_however_the_writes_interleave() -> None:
    """The half of idempotency that does hold, and the reason it holds.

    The unique constraint — not the SELECT — is what makes a key buy at most one
    resource, so this test proves it by racing the constraint directly: two
    transactions insert the same (workspace, operation, key) and the second must
    be refused by the database. If `UniqueConstraint('workspace_id','operation',
    'key')` (models.py:218) were dropped, both would land and every
    idempotency-guarded route would silently duplicate under concurrency.
    """
    from sqlalchemy.exc import IntegrityError

    client = _identity_client()
    workspace_id = client.identity.workspace_id  # type: ignore[attr-defined]
    created = client.post(
        "/api/conversations",
        json={"title": "Kept"},
        headers={"Idempotency-Key": "constraint-probe-0001"},
    )
    assert created.status_code == 201

    duplicate = session_scope()
    try:
        duplicate.add(
            IdempotencyRecord(
                workspace_id=workspace_id,
                operation="conversation.create",
                key="constraint-probe-0001",
                resource_id=created.json()["id"],
            )
        )
        with pytest.raises(IntegrityError):
            duplicate.commit()
        duplicate.rollback()
    finally:
        duplicate.close()

    db = session_scope()
    try:
        records = list(
            db.scalars(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.workspace_id == workspace_id,
                    IdempotencyRecord.key == "constraint-probe-0001",
                )
            )
        )
        rows = list(
            db.scalars(
                select(Conversation).where(Conversation.workspace_id == workspace_id)
            )
        )
    finally:
        db.close()
    assert len(records) == 1
    assert len(rows) == 1


# --------------------------------------------------------------------------
# Approving and denying the same tool call at once


def _parked_call(client: TestClient) -> Tuple[str, str]:
    """A run waiting for approval, with a proposed AgentToolCall. (run, call)."""
    identity = client.identity  # type: ignore[attr-defined]
    db = session_scope()
    try:
        conversation = Conversation(
            workspace_id=identity.workspace_id,
            created_by=identity.user_id,
            title="Approval race",
        )
        db.add(conversation)
        db.flush()
        run = Run(
            workspace_id=identity.workspace_id,
            conversation_id=conversation.id,
            agent_id=_agent_id(db, identity.workspace_id),
            created_by=identity.user_id,
            status="waiting_for_approval",
            prompt="do the thing",
        )
        db.add(run)
        db.flush()
        call = AgentToolCall(
            workspace_id=identity.workspace_id,
            run_id=run.id,
            name="edit_document",
            arguments_json='{"title":"t","content":"c"}',
            status="proposed",
            proposal_preview="diff",
        )
        db.add(call)
        db.commit()
        return run.id, call.id
    finally:
        db.close()


def _agent_id(db, workspace_id: str) -> str:
    from app.models import Agent

    agent = db.scalar(select(Agent).where(Agent.workspace_id == workspace_id))
    assert agent is not None
    return agent.id


def test_a_decided_tool_call_refuses_a_second_decision() -> None:
    """The sequential guarantee. Deleting the `call.status != "proposed"` check
    in `api/tools.py:265-266` fails this."""
    client = _identity_client()
    _run_id, call_id = _parked_call(client)
    first = client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        json={"decision": "denied"},
        headers={"Idempotency-Key": "decide-seq-0001"},
    )
    second = client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        json={"decision": "approved"},
        headers={"Idempotency-Key": "decide-seq-0002"},
    )
    assert first.status_code == 200
    assert second.status_code == 409


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: the approval gate is not enforced by the store. "
        "decide_agent_tool_call (api/tools.py:265-272) reads "
        "`call.status != 'proposed'`, raises 409 if it has moved, and otherwise "
        "assigns the new status — a read-then-write with no "
        "`UPDATE ... WHERE status='proposed'`, no version column and no "
        "constraint. Two transactions that both read `proposed` therefore both "
        "commit a decision, and the route schedules `resume_run` for each, so a "
        "denial can be overwritten by an approval that a reviewer raced. "
        "SQLite's single-writer lock happens to serialise the HTTP handlers in "
        "this suite, which is why the interleaving is written out explicitly "
        "here rather than left to threads — on any engine with row-level "
        "concurrency it is the default outcome, not a rare one. Remove the "
        "xfail when the transition is a conditional UPDATE whose rowcount "
        "decides."
    ),
)
def test_a_second_decision_cannot_land_on_a_call_that_was_already_decided() -> None:
    """The exact interleaving the route performs, made explicit.

    Two transactions, both reading the gate open, both writing. Threads are
    deliberately not used: the assertion is about the absence of a guard in the
    write path, and a test whose verdict depends on which thread the GIL
    released first is a test that reports "fixed" the day the timing changes.
    """
    client = _identity_client()
    _run_id, call_id = _parked_call(client)

    reviewer = session_scope()
    attacker = session_scope()
    try:
        denied = reviewer.get(AgentToolCall, call_id)
        approved = attacker.get(AgentToolCall, call_id)
        assert denied is not None and approved is not None
        # Both observe the gate open, which is what the route's check reads.
        assert denied.status == "proposed"
        assert approved.status == "proposed"

        denied.status = "denied"
        reviewer.commit()

        approved.status = "approved"
        attacker.commit()
    finally:
        reviewer.close()
        attacker.close()

    db = session_scope()
    try:
        final = db.get(AgentToolCall, call_id)
        assert final is not None
        status = final.status
    finally:
        db.close()
    assert status == "denied", (
        f"a denial was overwritten by a concurrent {status!r} decision"
    )


# --------------------------------------------------------------------------
# Run events


def test_a_run_event_sequence_cannot_be_reused() -> None:
    """`append_event` allocates the sequence itself; the database is the only
    thing enforcing it.

    `append_event` (services/events.py:27-33) reads `max(sequence)` and writes
    `+ 1`, so two writers on one run — a streaming turn and a cancel, or a run
    recovered while its background task is still going — allocate the same
    number from a stale read. What happens then is decided entirely by
    `UniqueConstraint('run_id','sequence')` (models.py:203): the insert is
    refused, and since there is no retry the refusal surfaces inside whatever was
    streaming. This pins the constraint, which is the half of the design that
    holds; the missing retry is recorded in the report rather than here, because
    SQLite's single-writer lock makes two overlapping *writes* untestable in
    this suite.

    Dropping the constraint fails this test, and would turn the collision into
    two events sharing a sequence — which the SSE resume protocol uses as its
    cursor, so a client would silently skip one.
    """
    from sqlalchemy.exc import IntegrityError

    client = _identity_client()
    run_id, _call_id = _parked_call(client)
    identity = client.identity  # type: ignore[attr-defined]

    db = session_scope()
    try:
        first = append_event(
            db,
            workspace_id=identity.workspace_id,
            run_id=run_id,
            event_type="message.delta",
            payload={"from": "stream"},
        )
        db.commit()
        assert first.sequence == 1

        # Exactly what a second writer computes from a read taken before that
        # commit landed.
        db.add(
            RunEvent(
                workspace_id=identity.workspace_id,
                run_id=run_id,
                sequence=1,
                event_type="run.cancelling",
                payload_json="{}",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()

    db = session_scope()
    try:
        sequences = [
            row.sequence
            for row in db.scalars(select(RunEvent).where(RunEvent.run_id == run_id))
        ]
    finally:
        db.close()
    assert sequences == [1]


# --------------------------------------------------------------------------
# Memory supersession


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: _upsert_item (services/memory.py:186-282) selects the rows "
        "holding a claim key, retires them by rewriting normalized_key, and "
        "inserts a replacement — all guarded only by "
        "`UniqueConstraint('workspace_id','kind','normalized_key')` "
        "(models.py:769) and never by a lock. Two runs writing the same claim "
        "both see no live row and both insert, so one commit dies on the "
        "constraint. Its caller, write_conversation_memory (memory.py:455-465), "
        "catches `Exception`, logs a warning and rolls back — so the losing "
        "turn's entire memory set is discarded silently. Remove the xfail when "
        "the upsert is retried or serialised."
    ),
)
def test_two_runs_writing_one_claim_key_both_survive() -> None:
    identity = create_identity()
    first = session_scope()
    second = session_scope()
    failure = ""
    try:
        _write_claim(first, identity.workspace_id, "value from the first run")
        _write_claim(second, identity.workspace_id, "value from the second run")
        first.commit()
        try:
            second.commit()
        except Exception as exc:  # noqa: BLE001
            failure = type(exc).__name__
            second.rollback()
    finally:
        first.close()
        second.close()

    assert not failure, (
        f"the second run's memory write died with {failure}; "
        "write_conversation_memory catches this and discards the whole turn"
    )


def _write_claim(
    db, workspace_id: str, content: str, *, key: str = "the-contested-claim"
) -> None:
    from app.services import memory as memory_service

    memory_service._upsert_item(
        db,
        workspace_id=workspace_id,
        conversation_id=None,
        run_id="",
        kind="fact",
        normalized_key=key,
        content=content,
        entity_names=[],
        message_ids=[],
        supersede=True,
    )


def test_a_claim_key_never_ends_up_with_two_live_rows() -> None:
    """The half of the property the unique constraint does deliver.

    Two transactions both write the same claim key. Whatever happens to the
    loser, the workspace must never end up holding two live rows for one claim —
    that is what would make `recall` inject a fact and its own contradiction into
    the same prompt. Dropping
    `UniqueConstraint('workspace_id','kind','normalized_key')` from `MemoryItem`
    (models.py:769) fails this with two live rows.
    """
    identity = create_identity()
    first = session_scope()
    second = session_scope()
    try:
        _write_claim(first, identity.workspace_id, "the first value", key="shared-key")
        _write_claim(second, identity.workspace_id, "the second value", key="shared-key")
        first.commit()
        try:
            second.commit()
        except Exception:  # noqa: BLE001 - the loser's fate is the other test
            second.rollback()
    finally:
        first.close()
        second.close()

    db = session_scope()
    try:
        live = [
            row
            for row in db.scalars(
                select(MemoryItem).where(
                    MemoryItem.workspace_id == identity.workspace_id,
                    MemoryItem.normalized_key == "shared-key",
                )
            )
            if row.status == "active"
        ]
    finally:
        db.close()
    assert len(live) <= 1, f"{len(live)} live memory rows hold one claim key"
