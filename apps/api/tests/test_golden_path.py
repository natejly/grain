from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.clock import utcnow
from app.services import runs


def test_health_and_bootstrap(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["database"] == "ok"

    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    payload = bootstrap.json()
    assert payload["identity"]["workspace_name"] == "Acme Knowledge Lab"
    assert payload["feature_flags"]["cited_memory"] is True
    assert payload["feature_flags"]["graph_memory"] is True
    assert payload["feature_flags"]["dashboards"] is True
    assert payload["feature_flags"]["generated_apps"] is True
    # The suite runs the test double, which is the only non-openai provider left.
    assert payload["model_provider"] == {
        "provider": "scripted",
        "configured": False,
        "model": "scripted-double",
        # The per-turn composer controls: the double talks to no provider, so its
        # only selectable "model" is itself; the effort ladder is the full
        # `ReasoningEffort` and the pre-select is the deployment default.
        "selectable_models": ["scripted-double"],
        "reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"],
        "default_effort": "low",
    }

    request_id = client.get(
        "/health",
        headers={"X-Request-ID": "release-gate-request"},
    )
    assert request_id.headers["X-Request-ID"] == "release-gate-request"
    assert request_id.headers["X-Content-Type-Options"] == "nosniff"


def test_upload_chat_citation_resume_and_delete(client, headers):
    document = (
        b"# Launch brief\n\n"
        b"The Northstar project will launch in October. "
        b"Its primary goal is to reduce customer onboarding time by forty percent. "
        b"The launch owner is Maya Chen.\n\n"
        b"Risks include migration latency and incomplete billing exports."
    )
    upload = client.post(
        "/api/sources",
        headers=headers,
        files={"file": ("northstar.md", document, "text/markdown")},
    )
    assert upload.status_code == 202
    source_id = upload.json()["id"]

    replay = client.post(
        "/api/sources",
        headers=headers,
        files={"file": ("northstar.md", document, "text/markdown")},
    )
    assert replay.status_code == 202
    assert replay.json()["id"] == source_id

    sources = client.get("/api/sources").json()
    source = next(item for item in sources if item["id"] == source_id)
    assert source["status"] == "ready"
    assert source["chunk_count"] >= 1

    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "conversation-golden-path"},
        json={"title": "Northstar review"},
    ).json()
    sent = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        headers={"Idempotency-Key": "message-golden-path"},
        json={"content": "Who owns the Northstar launch and what is its goal?"},
    )
    assert sent.status_code == 202
    run_id = sent.json()["run"]["id"]

    replayed = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        headers={"Idempotency-Key": "message-golden-path"},
        json={"content": "This must not create a second run"},
    )
    assert replayed.json()["replayed"] is True
    assert replayed.json()["run"]["id"] == run_id

    stream = client.get(f"/api/runs/{run_id}/events")
    assert stream.status_code == 200
    assert "event: retrieval.completed" in stream.text
    assert "event: message.completed" in stream.text
    ids = [
        int(line.removeprefix("id: "))
        for line in stream.text.splitlines()
        if line.startswith("id: ")
    ]
    assert ids == sorted(set(ids))

    resumed = client.get(
        f"/api/runs/{run_id}/events",
        headers={"Last-Event-ID": str(ids[-2])},
    )
    resumed_ids = [
        int(line.removeprefix("id: "))
        for line in resumed.text.splitlines()
        if line.startswith("id: ")
    ]
    assert resumed_ids == [ids[-1]]

    messages = client.get(
        f"/api/conversations/{conversation['id']}/messages"
    ).json()
    assistant = next(item for item in messages if item["role"] == "assistant")
    assert assistant["citations"]
    assert assistant["citations"][0]["filename"] == "northstar.md"

    # The citation validator's verdict rides back with the answer it judged.
    # It used to exist only as a run event and an audit row, so the contract the
    # product is built on was enforced somewhere no reader ever goes — and an
    # unenforceable contract and an unread one look identical from the outside.
    report = assistant["citation_report"]
    assert report is not None
    assert report["evidence_count"] == len(assistant["citations"])
    assert report["valid"] is True
    assert report["out_of_range"] == [] and report["malformed"] == []
    assert report["summary"]
    # A user message was never checked, and "not checked" must not be dressed up
    # as a clean bill of health.
    user_message = next(item for item in messages if item["role"] == "user")
    assert user_message["citation_report"] is None

    chunk_id = assistant["citations"][0]["chunk_id"]
    provenance = client.get(f"/api/chunks/{chunk_id}")
    assert provenance.status_code == 200
    assert "Maya Chen" in provenance.json()["content"]

    deleted = client.delete(
        f"/api/sources/{source_id}",
        headers={"Idempotency-Key": "delete-northstar"},
    )
    assert deleted.status_code == 204
    assert client.get(f"/api/chunks/{chunk_id}").status_code == 404
    assert all(item["id"] != source_id for item in client.get("/api/sources").json())


def test_tool_requires_approval_and_decision_is_audited(client, monkeypatch):
    monkeypatch.setattr(
        runs,
        "execute_read_only_get",
        lambda url, settings: (200, "Keep it logically awesome."),
    )
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "conversation-tool-test"},
        json={"title": "Tool approval"},
    ).json()
    sent = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        headers={"Idempotency-Key": "message-tool-test"},
        json={"content": "/tool github-zen"},
    )
    assert sent.status_code == 202
    run_id = sent.json()["run"]["id"]
    calls = client.get("/api/tool-calls").json()
    call = next(item for item in calls if item["run_id"] == run_id)
    assert call["status"] == "proposed"
    assert call["conversation_id"] == conversation["id"]

    decision = client.post(
        f"/api/tool-calls/{call['id']}/decision",
        headers={"Idempotency-Key": "approve-tool-test"},
        json={"decision": "approved"},
    )
    assert decision.status_code == 200

    completed = next(
        item for item in client.get("/api/tool-calls").json() if item["id"] == call["id"]
    )
    assert completed["status"] == "succeeded"
    assert completed["response_status"] == 200

    messages = client.get(
        f"/api/conversations/{conversation['id']}/messages"
    ).json()
    assert any("Keep it logically awesome" in item["content"] for item in messages)

    actions = [event["action"] for event in client.get("/api/audit-events").json()]
    assert "tool.proposed" in actions
    assert "tool.approved" in actions
    assert "tool.executed" in actions


def test_a_failing_tool_names_itself_in_the_events(client, monkeypatch):
    """A tool that throws must say *which* tool threw.

    The failure path emitted `tool.failed` with an id and an error and no name,
    and then `run.failed` with the same error — so the only thing a chat could
    tell the user was "something went wrong", about a tool the user had just
    personally approved by name. The id names a row; a person needs the name.
    """
    def _explode(url, settings):
        raise OSError("connection reset")

    monkeypatch.setattr(runs, "execute_read_only_get", _explode)
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "conversation-tool-failure"},
        json={"title": "Tool failure"},
    ).json()
    run_id = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        headers={"Idempotency-Key": "message-tool-failure"},
        json={"content": "/tool github-zen"},
    ).json()["run"]["id"]
    call = next(
        item for item in client.get("/api/tool-calls").json() if item["run_id"] == run_id
    )
    client.post(
        f"/api/tool-calls/{call['id']}/decision",
        headers={"Idempotency-Key": "approve-tool-failure"},
        json={"decision": "approved"},
    )

    stream = client.get(f"/api/runs/{run_id}/events").text
    assert "event: tool.failed" in stream
    # Both events carry it, because a client that only saw the run start still
    # has to be able to say what it is waiting on.
    started, failed = (
        _payload_of(stream, "tool.started"),
        _payload_of(stream, "tool.failed"),
    )
    assert started["tool_name"] == "github-zen"
    assert failed["tool_name"] == "github-zen"
    assert "connection reset" in failed["error"]


def _payload_of(stream: str, event_type: str) -> dict:
    """The data frame of one SSE event, by name."""
    import json

    lines = stream.splitlines()
    index = lines.index(f"event: {event_type}")
    data = next(line for line in lines[index:] if line.startswith("data: "))
    return json.loads(data.removeprefix("data: "))


def test_unknown_workspace_is_denied(client):
    response = client.get(
        "/api/conversations",
        headers={"X-Workspace-ID": "11111111-1111-4111-8111-111111111111"},
    )
    assert response.status_code == 403


def test_waiting_tool_run_can_be_cancelled(client):
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "conversation-cancel-test"},
        json={"title": "Cancel approval"},
    ).json()
    sent = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        headers={"Idempotency-Key": "message-cancel-test"},
        json={"content": "/tool github-zen"},
    ).json()
    run_id = sent["run"]["id"]
    cancelled = client.post(
        f"/api/runs/{run_id}/cancel",
        headers={"Idempotency-Key": "cancel-run-test"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    stream = client.get(f"/api/runs/{run_id}/events")
    assert "event: run.cancelled" in stream.text


def test_conversation_can_be_deleted(client, headers):
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "conversation-delete-test"},
        json={"title": "Disposable chat"},
    ).json()
    client.post(
        f"/api/conversations/{conversation['id']}/messages",
        headers={"Idempotency-Key": "message-before-delete"},
        json={"content": "Who owns the Atlas pilot?"},
    )
    deleted = client.delete(
        f"/api/conversations/{conversation['id']}",
        headers={"Idempotency-Key": "delete-conversation-test"},
    )
    assert deleted.status_code == 204
    replayed = client.delete(
        f"/api/conversations/{conversation['id']}",
        headers={"Idempotency-Key": "delete-conversation-test"},
    )
    assert replayed.status_code == 204
    listed = client.get("/api/conversations").json()
    assert all(item["id"] != conversation["id"] for item in listed)
    missing = client.get(f"/api/conversations/{conversation['id']}/messages")
    assert missing.status_code == 404
    actions = [event["action"] for event in client.get("/api/audit-events").json()]
    assert "conversation.deleted" in actions


def test_expired_run_lease_is_recovered(client):
    from app.auth import DEV_SEED_AGENT_ID, DEV_SEED_USER_ID, DEV_SEED_WORKSPACE_ID
    from app.database import SessionLocal
    from app.models import Conversation, Run, RunEvent
    from app.services.recovery import recover_durable_work

    db = SessionLocal()
    try:
        conversation = Conversation(
            workspace_id=DEV_SEED_WORKSPACE_ID,
            created_by=DEV_SEED_USER_ID,
            title="Recovery gate",
        )
        db.add(conversation)
        db.flush()
        run = Run(
            workspace_id=DEV_SEED_WORKSPACE_ID,
            conversation_id=conversation.id,
            agent_id=DEV_SEED_AGENT_ID,
            created_by=DEV_SEED_USER_ID,
            status="running",
            prompt="No indexed answer is required for this recovery test.",
            lease_expires_at=utcnow() - timedelta(seconds=5),
        )
        db.add(run)
        db.commit()
        run_id = run.id
    finally:
        db.close()

    recovered = recover_durable_work()
    assert recovered[0] >= 1

    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        assert run.status == "completed"
        event_types = list(
            db.scalars(
                select(RunEvent.event_type)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.sequence)
            )
        )
        assert "run.recovered" in event_types
        assert event_types[-1] == "run.completed"
    finally:
        db.close()
