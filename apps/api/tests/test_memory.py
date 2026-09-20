from __future__ import annotations

import json
import os
import time
from datetime import timedelta

from conftest import TEST_BASE_URL, Identity, authenticate, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth import DEV_SEED_USER_ID, DEV_SEED_WORKSPACE_ID
from app.clock import utcnow
from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AuditEvent,
    Conversation,
    Membership,
    MemoryItem,
    Message,
    Run,
    User,
    WorkspaceEvent,
)
from app.services import memory as memory_service
from app.services.coworking import append_workspace_event


def _send_and_wait(client, conversation_id: str, content: str, key: str) -> str:
    sent = client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": key},
        json={"content": content},
    )
    assert sent.status_code == 202
    run_id = sent.json()["run"]["id"]
    for _ in range(50):
        messages = client.get(f"/api/conversations/{conversation_id}/messages").json()
        if any(m["run_id"] == run_id and m["role"] == "assistant" for m in messages):
            return run_id
        time.sleep(0.1)
    raise AssertionError("run did not complete in time")


def test_completed_run_writes_memory_with_provenance(client):
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "memory-conversation-1"},
        json={"title": "Memory write"},
    ).json()
    run_id = _send_and_wait(
        client,
        conversation["id"],
        "Project Atlas is led by Dana Reyes at Acme Labs.",
        "memory-message-1",
    )

    items = client.get("/api/memory").json()
    assert items, "expected memory items after a completed run"
    atlas = next(
        (item for item in items if "Project Atlas" in item["content"]), None
    )
    assert atlas is not None
    assert atlas["kind"] == "fact"
    assert atlas["conversation_id"] == conversation["id"]
    assert atlas["message_ids"], "memory must carry message provenance"

    # The run event log records what was recalled before answering.
    events = client.get(
        f"/api/runs/{run_id}/events?after=0",
        headers={"Accept": "text/event-stream"},
    )
    assert "memory.recalled" in events.text


def test_memory_is_recalled_in_later_conversations(client):
    first = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "memory-conversation-2"},
        json={"title": "Teach"},
    ).json()
    _send_and_wait(
        client,
        first["id"],
        "Remember that Orion Initiative belongs to the platform team.",
        "memory-message-2",
    )

    second = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "memory-conversation-3"},
        json={"title": "Recall"},
    ).json()
    run_id = _send_and_wait(
        client,
        second["id"],
        "What do you know about Orion Initiative?",
        "memory-message-3",
    )
    events = client.get(
        f"/api/runs/{run_id}/events?after=0",
        headers={"Accept": "text/event-stream"},
    ).text
    assert "memory.recalled" in events
    assert "Orion Initiative" in events


def test_forgotten_memory_is_excluded_from_recall_and_listing(client):
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "memory-conversation-4"},
        json={"title": "Forget"},
    ).json()
    _send_and_wait(
        client,
        conversation["id"],
        "Zephyr Program launches in March.",
        "memory-message-4",
    )
    items = client.get("/api/memory").json()
    target = next(item for item in items if "Zephyr Program" in item["content"])

    deleted = client.delete(
        f"/api/memory/{target['id']}",
        headers={"Idempotency-Key": "memory-forget-1"},
    )
    assert deleted.status_code == 204
    remaining = client.get("/api/memory").json()
    assert all(item["id"] != target["id"] for item in remaining)

    # A forgotten memory must not resurface after the same fact is repeated.
    _send_and_wait(
        client,
        conversation["id"],
        "Zephyr Program launches in March.",
        "memory-message-5",
    )
    after = client.get("/api/memory").json()
    assert all(item["id"] != target["id"] for item in after)


def test_memory_is_workspace_scoped(client):
    items = client.get("/api/memory").json()
    assert items, "seed memories exist in the demo workspace"
    denied = client.get(
        "/api/memory",
        headers={"X-Workspace-ID": "22222222-2222-4222-8222-222222222222"},
    )
    assert denied.status_code == 403


def test_graph_rebuild_includes_memory_entities(client):
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "memory-conversation-5"},
        json={"title": "Graph memory"},
    ).json()
    # Shared, because there is one `GraphProjection` per workspace and every
    # member reads the same nodes out of it, so it may only be built from shared
    # memory — see ADR 0010 and the sibling test below. A thread is personal
    # until somebody shares it, which is exactly the decision being made here.
    assert (
        client.put(
            f"/api/conversations/{conversation['id']}/share", json={"shared": True}
        ).status_code
        == 200
    )
    _send_and_wait(
        client,
        conversation["id"],
        "Nimbus Platform pairs with Quasar Engine for deployments.",
        "memory-message-6",
    )
    rebuilt = client.post(
        "/api/graph/rebuild",
        headers={"Idempotency-Key": "memory-graph-rebuild"},
    )
    assert rebuilt.status_code == 202
    # 30s, not 5s. The rebuild runs as a background task and extracts entities
    # from every memory in the workspace, so its wall time tracks machine load
    # rather than anything the assertion is about. At 5s this passed alone in
    # 0.27s and failed roughly one full-suite run in four — which is the margin
    # that teaches people to re-run a real assertion until it is green.
    for _ in range(300):
        graph = client.get("/api/graph").json()
        if graph["status"] == "ready":
            break
        time.sleep(0.1)
    graph = client.get("/api/graph").json()
    nimbus = next(
        (e for e in graph["entities"] if e["name"] == "Nimbus Platform"), None
    )
    assert nimbus is not None
    assert nimbus["memory_ids"], "memory-derived entity should carry memory provenance"


def test_a_personal_threads_memory_stays_out_of_the_shared_graph(client):
    """The cost of ADR 0010, asserted rather than assumed.

    The graph is one projection per workspace and every member reads it, so a
    personal memory folded into it would surface another member's private thread
    as an entity name — the leak, one indirection along. Threads are personal by
    default, so this is the *ordinary* case and not a corner: memory stops
    feeding the graph until a thread is shared, which is a real capability lost
    and the reason a per-scope projection is the next change worth making.
    """
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "memory-conversation-personal"},
        json={"title": "Personal graph memory"},
    ).json()
    _send_and_wait(
        client,
        conversation["id"],
        "Zephyr Ledger reconciles with Marlow Clearing every Friday.",
        "memory-message-personal",
    )
    assert (
        client.post(
            "/api/graph/rebuild",
            headers={"Idempotency-Key": "memory-graph-rebuild-personal"},
        ).status_code
        == 202
    )
    for _ in range(300):
        graph = client.get("/api/graph").json()
        if graph["status"] == "ready":
            break
        time.sleep(0.1)
    graph = client.get("/api/graph").json()
    assert graph["status"] == "ready"
    names = {entity["name"] for entity in graph["entities"]}
    assert "Zephyr Ledger" not in names
    # The memory itself is untouched — it is recallable by its owner, it is only
    # the shared projection it stays out of.
    assert any(
        "Zephyr Ledger" in item["content"] for item in client.get("/api/memory").json()
    )


# --------------------------------------------------------------------------- #
# The rolling summary's window
# --------------------------------------------------------------------------- #


def test_rolling_summary_tracks_the_conversations_tail(client):
    """The pinned summary follows the LAST eight user messages, not the first.

    Sliced `[:8]`, the summary froze after the eighth user turn and described
    a conversation that had long since moved on.
    """
    db = SessionLocal()
    try:
        conversation = Conversation(
            workspace_id=DEV_SEED_WORKSPACE_ID, created_by=DEV_SEED_USER_ID
        )
        db.add(conversation)
        db.flush()
        agent_id = db.scalar(
            select(Agent.id).where(Agent.workspace_id == DEV_SEED_WORKSPACE_ID)
        )
        run = Run(
            workspace_id=DEV_SEED_WORKSPACE_ID,
            conversation_id=conversation.id,
            agent_id=agent_id,
            created_by=DEV_SEED_USER_ID,
            status="completed",
            prompt="topic ten",
        )
        db.add(run)
        db.flush()
        base = utcnow()
        names = [
            "one", "two", "three", "four", "five",
            "six", "seven", "eight", "nine", "ten",
        ]
        for index, name in enumerate(names):
            db.add(
                Message(
                    workspace_id=DEV_SEED_WORKSPACE_ID,
                    conversation_id=conversation.id,
                    run_id=run.id,
                    role="user",
                    content=f"Tell me about topic {name}",
                    created_at=base + timedelta(seconds=index),
                )
            )
        db.flush()

        memory_service._refresh_summary(db, run, get_settings())
        db.commit()

        summary = db.scalar(
            select(MemoryItem).where(
                MemoryItem.workspace_id == DEV_SEED_WORKSPACE_ID,
                MemoryItem.conversation_id == conversation.id,
                MemoryItem.kind == "summary",
            )
        )
        assert summary is not None
        assert "topic ten" in summary.content
        assert "topic nine" in summary.content
        assert "topic one" not in summary.content
        assert "topic two" not in summary.content
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Manual add and edit (POST /api/memory, PATCH /api/memory/{id})
# --------------------------------------------------------------------------- #


def _second_member_client() -> tuple[TestClient, str]:
    """A second member of the demo workspace, holding a real session."""
    db = SessionLocal()
    try:
        user = User(
            email=f"member-{os.urandom(4).hex()}@example.com", name="Second member"
        )
        db.add(user)
        db.flush()
        db.add(
            Membership(
                workspace_id=DEV_SEED_WORKSPACE_ID, user_id=user.id, role="member"
            )
        )
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf_token = issue_session(user_id)
    other = TestClient(app, base_url=TEST_BASE_URL)
    authenticate(
        other,
        Identity(
            user_id=user_id,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            token=token,
            csrf_token=csrf_token,
        ),
    )
    return other, user_id


def test_manual_memory_add_scopes_between_mine_and_everyones(client):
    mine = client.post(
        "/api/memory",
        headers={"Idempotency-Key": "manual-add-mine-1"},
        json={
            "content": "Nate prefers metric units in dashboards.",
            "kind": "preference",
        },
    )
    assert mine.status_code == 201, mine.text
    mine_row = mine.json()
    assert mine_row["shared"] is False
    assert mine_row["kind"] == "preference"

    everyone = client.post(
        "/api/memory",
        headers={"Idempotency-Key": "manual-add-shared-1"},
        json={"content": "The team demo happens on Thursdays.", "shared": True},
    )
    assert everyone.status_code == 201, everyone.text
    everyone_row = everyone.json()
    assert everyone_row["shared"] is True

    other, other_id = _second_member_client()
    listed = {item["id"] for item in other.get("/api/memory").json()}
    assert everyone_row["id"] in listed
    assert mine_row["id"] not in listed, "a personal memory leaked to a colleague"

    # And recall answers the same way the listing does.
    db = SessionLocal()
    try:
        found = memory_service.recall(
            db,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            conversation_id="none",
            query="metric units in dashboards",
            viewer_id=other_id,
        )
        assert all(item.id != mine_row["id"] for item in found.items)
        found = memory_service.recall(
            db,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            conversation_id="none",
            query="metric units in dashboards",
            viewer_id=DEV_SEED_USER_ID,
        )
        assert any(item.id == mine_row["id"] for item in found.items)
    finally:
        db.close()


def test_manual_add_replays_reinforces_and_proves_the_space(client):
    body = {"content": "Orion launch reviews happen quarterly."}
    first = client.post(
        "/api/memory", headers={"Idempotency-Key": "manual-add-replay-1"}, json=body
    )
    assert first.status_code == 201, first.text
    row = first.json()

    # The same key replays the same row rather than minting a second one.
    replay = client.post(
        "/api/memory", headers={"Idempotency-Key": "manual-add-replay-1"}, json=body
    )
    assert replay.json()["id"] == row["id"]

    # A fresh key with the same sentence reinforces instead of duplicating.
    reinforced = client.post(
        "/api/memory", headers={"Idempotency-Key": "manual-add-replay-2"}, json=body
    )
    assert reinforced.json()["id"] == row["id"]
    assert reinforced.json()["importance"] == row["importance"] + 1

    # A foreign or deleted space is a 404, never a silently unscoped memory.
    missing = client.post(
        "/api/memory",
        headers={"Idempotency-Key": "manual-add-badspace-1"},
        json={"content": "Never lands anywhere.", "space_id": "no-such-space"},
    )
    assert missing.status_code == 404


def test_editing_a_content_keyed_memory_moves_its_key(client):
    created = client.post(
        "/api/memory",
        headers={"Idempotency-Key": "manual-edit-key-1"},
        json={"content": "The staging API deploys on Fly.io."},
    ).json()

    edited = client.patch(
        f"/api/memory/{created['id']}",
        json={"content": "The staging API deploys on Railway."},
    )
    assert edited.status_code == 200, edited.text

    # The content-hash key moved with the edit: the OLD sentence is free again
    # and creates a fresh row...
    re_old = client.post(
        "/api/memory",
        headers={"Idempotency-Key": "manual-edit-key-2"},
        json={"content": "The staging API deploys on Fly.io."},
    ).json()
    assert re_old["id"] != created["id"]

    # ...while the NEW sentence lands on the edited row and reinforces it.
    re_new = client.post(
        "/api/memory",
        headers={"Idempotency-Key": "manual-edit-key-3"},
        json={"content": "The staging API deploys on Railway."},
    ).json()
    assert re_new["id"] == created["id"]
    assert re_new["importance"] == edited.json()["importance"] + 1


def test_editing_a_claim_keyed_memory_keeps_the_slot(client):
    settings = get_settings()
    db = SessionLocal()
    try:
        written = memory_service.apply_extracted_memories(
            db,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            conversation_id=None,
            run_id="",
            extracted=[
                {
                    "kind": "fact",
                    "content": "Rivka owns the payments on-call rotation.",
                    "normalized_key": "payments|oncall_lead",
                }
            ],
            message_ids=[],
            settings=settings,
        )
        db.commit()
        item_id = written[0].id
    finally:
        db.close()

    edited = client.patch(
        f"/api/memory/{item_id}",
        json={"content": "Sasha owns the payments on-call rotation."},
    )
    assert edited.status_code == 200, edited.text

    db = SessionLocal()
    try:
        row = db.get(MemoryItem, item_id)
        assert row.normalized_key == "payments|oncall_lead", (
            "an edit keeps a claim key — the slot outlives the value"
        )
        # A later extractor correction on that key still supersedes the edit.
        memory_service.apply_extracted_memories(
            db,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            conversation_id=None,
            run_id="",
            extracted=[
                {
                    "kind": "fact",
                    "content": "Timo owns the payments on-call rotation now.",
                    "normalized_key": "payments|oncall_lead",
                }
            ],
            message_ids=[],
            settings=settings,
        )
        db.commit()
        db.refresh(row)
        assert row.status == "superseded"
    finally:
        db.close()


def test_patch_refuses_foreign_summary_and_duplicate_rows(client):
    # Another member's personal row is invisible, not forbidden: 404.
    other, _other_id = _second_member_client()
    theirs = other.post(
        "/api/memory",
        headers={"Idempotency-Key": "manual-other-private-1"},
        json={"content": "Second member keeps this note private."},
    ).json()
    denied = client.patch(
        f"/api/memory/{theirs['id']}", json={"content": "overwritten by a colleague"}
    )
    assert denied.status_code == 404

    # The rolling summary rewrites itself: 422.
    db = SessionLocal()
    try:
        summary = MemoryItem(
            workspace_id=DEV_SEED_WORKSPACE_ID,
            kind="summary",
            content="Conversation topics so far: testing.",
            normalized_key=f"summary-{os.urandom(6).hex()}",
        )
        db.add(summary)
        db.commit()
        summary_id = summary.id
    finally:
        db.close()
    refused = client.patch(
        f"/api/memory/{summary_id}", json={"content": "hand-written summary"}
    )
    assert refused.status_code == 422

    # Editing to a sentence another active row already holds: 409.
    first = client.post(
        "/api/memory",
        headers={"Idempotency-Key": "manual-dup-1"},
        json={"content": "Alpha rota starts at nine."},
    ).json()
    second = client.post(
        "/api/memory",
        headers={"Idempotency-Key": "manual-dup-2"},
        json={"content": "Alpha rota ends at five."},
    ).json()
    assert first["id"] != second["id"]
    conflicted = client.patch(
        f"/api/memory/{second['id']}", json={"content": "Alpha rota starts at nine."}
    )
    assert conflicted.status_code == 409
    assert "already says this" in conflicted.json()["detail"]


# --------------------------------------------------------------------------- #
# Per-member opt-out and per-conversation incognito
# --------------------------------------------------------------------------- #


def test_memory_opt_out_skips_recall_and_extraction(client):
    flipped = client.put("/api/me/memory", json={"enabled": False})
    assert flipped.status_code == 200, flipped.text
    assert flipped.json() == {"enabled": False}
    try:
        conversation = client.post(
            "/api/conversations",
            headers={"Idempotency-Key": "memory-optout-conversation"},
            json={"title": "Opted out"},
        ).json()
        # The scripted extractor DOES script a memory for this prompt — the
        # rows' absence below proves the opt-out skipped it, not the script.
        run_id = _send_and_wait(
            client,
            conversation["id"],
            "Helios Array powers the beacon relay.",
            "memory-optout-message",
        )
        time.sleep(1.0)  # the post-run writer runs after the answer lands

        events = client.get(
            f"/api/runs/{run_id}/events?after=0",
            headers={"Accept": "text/event-stream"},
        ).text
        assert "memory.recalled" not in events, (
            "silence says memory did not run; a count-0 event would say it did"
        )
        items = client.get("/api/memory").json()
        assert not any("Helios Array" in item["content"] for item in items)

        # The explicit remember tool still works while opted out.
        db = SessionLocal()
        try:
            memory_service.remember_memory(
                db,
                workspace_id=DEV_SEED_WORKSPACE_ID,
                conversation_id=conversation["id"],
                user_id=DEV_SEED_USER_ID,
                content="Keep the Helios badge inventory current.",
            )
            db.commit()
        finally:
            db.close()
        items = client.get("/api/memory").json()
        assert any("Helios badge inventory" in item["content"] for item in items)
    finally:
        restored = client.put("/api/me/memory", json={"enabled": True})
        assert restored.status_code == 200


def test_incognito_thread_neither_recalls_nor_stores(client):
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "memory-incognito-conversation"},
        json={"title": "Temporary", "incognito": True},
    ).json()
    assert conversation["incognito"] is True

    run_id = _send_and_wait(
        client,
        conversation["id"],
        "Nimbus Relay guards the night shift.",
        "memory-incognito-message",
    )
    time.sleep(1.0)

    events = client.get(
        f"/api/runs/{run_id}/events?after=0",
        headers={"Accept": "text/event-stream"},
    ).text
    assert "memory.recalled" not in events

    items = client.get("/api/memory").json()
    assert not any("Nimbus Relay" in item["content"] for item in items)
    db = SessionLocal()
    try:
        # Nothing at all — the rolling summary included.
        stored = db.scalar(
            select(MemoryItem.id).where(
                MemoryItem.conversation_id == conversation["id"]
            )
        )
        assert stored is None
        # No memory.updated signal either: nothing was updated.
        for event in db.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == DEV_SEED_WORKSPACE_ID,
                WorkspaceEvent.event_type == "memory.updated",
            )
        ):
            assert json.loads(event.payload_json)["run_id"] != run_id

        # The service function itself stays incognito-unaware on purpose: the
        # gate is the tool registry (llm_tools.agentic_memory_tools offers an
        # incognito thread no memory tools at all — see
        # test_incognito_thread_is_offered_no_memory_tools), so nothing
        # model-driven can reach this call from a temporary chat. A direct
        # service call still writes, which pins that the gate lives in the
        # offering, not in a second status filter here.
        memory_service.remember_memory(
            db,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            conversation_id=conversation["id"],
            user_id=DEV_SEED_USER_ID,
            content="Nimbus retro notes live in the shared drive.",
        )
        db.commit()
    finally:
        db.close()
    items = client.get("/api/memory").json()
    assert any("Nimbus retro notes" in item["content"] for item in items)


def test_incognito_thread_is_offered_no_memory_tools(client):
    """Incognito removes remember/forget/search_memory from the registry.

    Incognito is the user's explicit per-thread instruction, and `remember` is
    model-initiated — under the default auto_writes mode it would execute with
    no approval card — so the tools are never offered there at all. The
    per-member memory_enabled toggle keeps the opposite, documented behaviour:
    an opted-out member still gets the tools, because "remember this" said out
    loud outranks their default.
    """
    from app.services.llm_tools import ToolContext, build_registry

    incognito = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "memory-incognito-tools-conversation"},
        json={"title": "Temporary", "incognito": True},
    ).json()
    assert incognito["incognito"] is True
    ordinary = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "memory-ordinary-tools-conversation"},
        json={"title": "Durable"},
    ).json()

    # An agentic run in the incognito thread stores nothing durable...
    run_id = _send_and_wait(
        client,
        incognito["id"],
        "Nimbus Relay rotates its watchword weekly.",
        "memory-incognito-tools-message",
    )
    time.sleep(1.0)  # the post-run writer runs after the answer lands
    assert run_id

    db = SessionLocal()
    try:
        stored = db.scalar(
            select(MemoryItem.id).where(
                MemoryItem.conversation_id == incognito["id"]
            )
        )
        assert stored is None, "an incognito run left a durable memory behind"

        # ...and the turn's registry never offered the tools to begin with.
        offered = build_registry(
            db,
            ToolContext(
                workspace_id=DEV_SEED_WORKSPACE_ID,
                user_id=DEV_SEED_USER_ID,
                conversation_id=incognito["id"],
            ),
        )
        for name in ("remember", "forget", "search_memory"):
            assert name not in offered, f"{name} is offered in an incognito thread"

        durable = build_registry(
            db,
            ToolContext(
                workspace_id=DEV_SEED_WORKSPACE_ID,
                user_id=DEV_SEED_USER_ID,
                conversation_id=ordinary["id"],
            ),
        )
        for name in ("remember", "forget", "search_memory"):
            assert name in durable, f"{name} vanished from an ordinary thread"

        # The member-toggle carve-out is untouched: opted out still gets them.
        membership = db.scalar(
            select(Membership).where(
                Membership.workspace_id == DEV_SEED_WORKSPACE_ID,
                Membership.user_id == DEV_SEED_USER_ID,
            )
        )
        membership.memory_enabled = False
        db.commit()
        try:
            opted_out = build_registry(
                db,
                ToolContext(
                    workspace_id=DEV_SEED_WORKSPACE_ID,
                    user_id=DEV_SEED_USER_ID,
                    conversation_id=ordinary["id"],
                ),
            )
            for name in ("remember", "forget", "search_memory"):
                assert name in opted_out, (
                    f"the member toggle must not remove {name}: an explicit "
                    "instruction outranks a default"
                )
        finally:
            membership.memory_enabled = True
            db.commit()
    finally:
        db.close()


def test_memory_updated_event_fires_once_for_a_storing_run(client):
    db = SessionLocal()
    try:
        before = {
            event.id
            for event in db.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == DEV_SEED_WORKSPACE_ID,
                    WorkspaceEvent.event_type == "memory.updated",
                )
            )
        }
    finally:
        db.close()

    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "memory-signal-conversation"},
        json={"title": "Signal"},
    ).json()
    run_id = _send_and_wait(
        client,
        conversation["id"],
        "Aurora Gateway is maintained by the tools team.",
        "memory-signal-message",
    )

    new_events = []
    payloads = []
    audit = None
    for _ in range(50):
        db = SessionLocal()
        try:
            new_events = [
                event
                for event in db.scalars(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == DEV_SEED_WORKSPACE_ID,
                        WorkspaceEvent.event_type == "memory.updated",
                    )
                )
                if event.id not in before
            ]
            if new_events:
                payloads = [json.loads(event.payload_json) for event in new_events]
                audit = db.scalar(
                    select(AuditEvent).where(
                        AuditEvent.workspace_id == DEV_SEED_WORKSPACE_ID,
                        AuditEvent.action == "memory.updated",
                        AuditEvent.resource_id == run_id,
                    )
                )
                break
        finally:
            db.close()
        time.sleep(0.1)
    assert len(new_events) == 1, "exactly one memory.updated event per storing run"
    payload = payloads[0]
    assert payload["run_id"] == run_id
    assert payload["conversation_id"] == conversation["id"]
    assert payload["ids"], "the payload names what changed"
    assert payload["count"] == len(payload["ids"])
    assert audit is not None
    assert json.loads(audit.detail_json)["items"] == len(payload["ids"])


# --------------------------------------------------------------------------- #
# The memory.updated signal: manual writes emit it, the stream scopes it
# --------------------------------------------------------------------------- #


def _last_workspace_sequence() -> int:
    db = SessionLocal()
    try:
        return int(
            db.scalar(
                select(func.coalesce(func.max(WorkspaceEvent.sequence), 0)).where(
                    WorkspaceEvent.workspace_id == DEV_SEED_WORKSPACE_ID
                )
            )
            or 0
        )
    finally:
        db.close()


def _stream_once(reader: TestClient, after: int) -> str:
    response = reader.get(
        "/api/coworking/stream", params={"once": "true", "after": after}
    )
    assert response.status_code == 200
    return response.text


def test_manual_memory_writes_ping_the_stream_with_viewer_scoping(client):
    """POST and PATCH emit memory.updated, and the stream scopes it per viewer.

    Member A hand-adds a personal and a shared memory, then edits the shared
    one. A's stream carries all three pings; member B's carries only the two
    shared ones — a personal row's ping naming its id must never reach a
    viewer GET /api/memory would hide the row from.
    """
    other, _other_id = _second_member_client()
    after = _last_workspace_sequence()

    personal = client.post(
        "/api/memory",
        headers={"Idempotency-Key": "manual-signal-personal-1"},
        json={"content": "Signal probe: my private launch checklist review."},
    )
    assert personal.status_code == 201, personal.text
    personal_id = personal.json()["id"]
    shared = client.post(
        "/api/memory",
        headers={"Idempotency-Key": "manual-signal-shared-1"},
        json={"content": "Signal probe: the demo is on Thursday.", "shared": True},
    )
    assert shared.status_code == 201, shared.text
    shared_id = shared.json()["id"]
    edited = client.patch(
        f"/api/memory/{shared_id}",
        json={"content": "Signal probe: the demo moved to Friday."},
    )
    assert edited.status_code == 200, edited.text

    mine = _stream_once(client, after)
    theirs = _stream_once(other, after)

    assert personal_id in mine, "the author's own tabs must hear a personal add"
    assert mine.count(shared_id) >= 2, "POST and PATCH each ping for a shared row"
    assert personal_id not in theirs, (
        "a personal row's memory.updated reached another member"
    )
    assert theirs.count(shared_id) >= 2, "shared pings must reach every member"


def test_a_personal_threads_memory_event_stays_off_other_streams(client):
    """The extraction-shaped payload is scoped too, on both of its axes.

    One event names a personal owner, one names only a personal conversation
    (the pre-owner_id payload shape): the owner axis and the resolve_visible
    conversation axis must each keep the frame off another member's stream,
    exactly as _visible_runs and the presence gate hide the same thread.
    """
    other, _other_id = _second_member_client()
    after = _last_workspace_sequence()

    db = SessionLocal()
    try:
        thread = Conversation(
            workspace_id=DEV_SEED_WORKSPACE_ID,
            created_by=DEV_SEED_USER_ID,
            title="Mine alone",
        )
        db.add(thread)
        db.flush()
        append_workspace_event(
            db,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            event_type="memory.updated",
            payload={
                "run_id": "run-owner-axis",
                "conversation_id": thread.id,
                "count": 1,
                "ids": ["memory-owner-axis"],
                "owner_id": DEV_SEED_USER_ID,
            },
        )
        db.commit()
        append_workspace_event(
            db,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            event_type="memory.updated",
            payload={
                "run_id": "run-thread-axis",
                "conversation_id": thread.id,
                "count": 1,
                "ids": ["memory-thread-axis"],
            },
        )
        db.commit()
    finally:
        db.close()

    mine = _stream_once(client, after)
    theirs = _stream_once(other, after)

    assert "memory-owner-axis" in mine
    assert "memory-thread-axis" in mine
    assert "memory-owner-axis" not in theirs, "the owner axis leaked"
    assert "memory-thread-axis" not in theirs, "the conversation axis leaked"


# --------------------------------------------------------------------------- #
# Replay, tombstones, and the write-time liveness re-check
# --------------------------------------------------------------------------- #


def test_replay_of_a_spent_key_never_widens_visibility(client):
    """The replay branch goes through _active, like every other memory read.

    A spent Idempotency-Key must never hand back what GET would hide: another
    member replaying the same key gets the refusal, not A's personal content,
    and a key whose row was since forgotten replays as gone rather than as a
    live 201 for a tombstone.
    """
    body = {"content": "Replay guard: rotate the beacon keys in October."}
    created = client.post(
        "/api/memory", headers={"Idempotency-Key": "replay-guard-1"}, json=body
    )
    assert created.status_code == 201, created.text
    row = created.json()

    other, _other_id = _second_member_client()
    foreign = other.post(
        "/api/memory", headers={"Idempotency-Key": "replay-guard-1"}, json=body
    )
    assert foreign.status_code == 409, (
        "another member's replay must be refused, not answered with A's row"
    )

    deleted = client.delete(
        f"/api/memory/{row['id']}",
        headers={"Idempotency-Key": "replay-guard-delete-1"},
    )
    assert deleted.status_code == 204
    replayed = client.post(
        "/api/memory", headers={"Idempotency-Key": "replay-guard-1"}, json=body
    )
    assert replayed.status_code == 409, (
        "a tombstoned resource must replay as gone, not as a live 201"
    )
    assert "no longer exists" in replayed.json()["detail"]


def test_patch_into_a_forgotten_sentence_names_the_tombstone(client):
    """The 409 for a tombstone collision says what actually blocks the edit.

    The colliding row is invisible to GET, so "another memory already says
    this" would blame a duplicate the caller can never find. POSTing the same
    sentence restores the tombstone (the explicit-write override), and the
    message points there.
    """
    first = client.post(
        "/api/memory",
        headers={"Idempotency-Key": "tombstone-message-1"},
        json={"content": "Tombstone probe: gate reviews happen in March."},
    ).json()
    second = client.post(
        "/api/memory",
        headers={"Idempotency-Key": "tombstone-message-2"},
        json={"content": "Tombstone probe: gate reviews happen in April."},
    ).json()
    forgotten = client.delete(
        f"/api/memory/{first['id']}",
        headers={"Idempotency-Key": "tombstone-message-delete-1"},
    )
    assert forgotten.status_code == 204

    conflicted = client.patch(
        f"/api/memory/{second['id']}",
        json={"content": "Tombstone probe: gate reviews happen in March."},
    )
    assert conflicted.status_code == 409, conflicted.text
    assert "previously forgotten" in conflicted.json()["detail"]
    assert "already says this" not in conflicted.json()["detail"]


def test_patch_refuses_a_row_the_extractor_just_retired(client, monkeypatch):
    """An edit cannot land on a row supersession retired mid-request.

    The row is loaded through _active, then — while the PATCH is in flight —
    the extractor's worker supersedes the same claim from its own session.
    Without the write-time re-check the edit commits onto the retired row,
    answers 200, and is invisible everywhere afterwards.
    """
    import app.api.memory as memory_api

    settings = get_settings()
    db = SessionLocal()
    try:
        written = memory_service.apply_extracted_memories(
            db,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            conversation_id=None,
            run_id="",
            extracted=[
                {
                    "kind": "fact",
                    "content": "Relay deploys on Fly.io.",
                    "normalized_key": "relay|deploy_host",
                }
            ],
            message_ids=[],
            settings=settings,
        )
        db.commit()
        item_id = written[0].id
    finally:
        db.close()

    real_edit = memory_api.edit_memory

    def race(db_, *, item, content, settings):
        # The extractor lands its correction between the route's load and its
        # write, from its own session, exactly as write_conversation_memory's
        # worker thread does.
        worker = SessionLocal()
        try:
            memory_service.apply_extracted_memories(
                worker,
                workspace_id=DEV_SEED_WORKSPACE_ID,
                conversation_id=None,
                run_id="",
                extracted=[
                    {
                        "kind": "fact",
                        "content": "Relay deploys on Railway now.",
                        "normalized_key": "relay|deploy_host",
                    }
                ],
                message_ids=[],
                settings=get_settings(),
            )
            worker.commit()
        finally:
            worker.close()
        return real_edit(db_, item=item, content=content, settings=settings)

    monkeypatch.setattr(memory_api, "edit_memory", race)
    denied = client.patch(
        f"/api/memory/{item_id}",
        json={"content": "Relay deploys on Fly.io, eastern region."},
    )
    assert denied.status_code == 409, denied.text
    assert "superseded" in denied.json()["detail"]

    db = SessionLocal()
    try:
        row = db.get(MemoryItem, item_id)
        assert row.status == "superseded", "the extractor's retirement stands"
        assert row.content == "Relay deploys on Fly.io.", (
            "the refused edit must roll back, not land on the retired row"
        )
    finally:
        db.close()


def test_editing_a_claim_row_to_an_unrelated_sentence_leaves_the_slot(client):
    """A rewrite that drops the claim's subject re-keys to the content hash.

    Keeping the claim key is right for a correction of the same fact
    (test_editing_a_claim_keyed_memory_keeps_the_slot pins that); for a
    rewrite into a different fact it parks the user's sentence on a slot the
    extractor will supersede. The line is the key's own subject tokens, so
    the check is deterministic and needs no model call.
    """
    settings = get_settings()
    db = SessionLocal()
    try:
        written = memory_service.apply_extracted_memories(
            db,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            conversation_id=None,
            run_id="",
            extracted=[
                {
                    "kind": "fact",
                    "content": "The beacon API deploys to Railway.",
                    "normalized_key": "beacon_api|deploy_host",
                }
            ],
            message_ids=[],
            settings=settings,
        )
        db.commit()
        item_id = written[0].id
    finally:
        db.close()

    edited = client.patch(
        f"/api/memory/{item_id}",
        json={"content": "Standup moved to half past nine."},
    )
    assert edited.status_code == 200, edited.text

    db = SessionLocal()
    try:
        row = db.get(MemoryItem, item_id)
        assert "|" not in row.normalized_key, (
            "a rewrite that drops the subject must leave the claim slot"
        )
        # The vacated slot learns fresh, and the manual sentence survives it.
        memory_service.apply_extracted_memories(
            db,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            conversation_id=None,
            run_id="",
            extracted=[
                {
                    "kind": "fact",
                    "content": "The beacon API deploys to Fly.io now.",
                    "normalized_key": "beacon_api|deploy_host",
                }
            ],
            message_ids=[],
            settings=settings,
        )
        db.commit()
        db.refresh(row)
        assert row.status == "active", (
            "the extractor's next pass on the old slot destroyed a manual edit"
        )
        assert row.content == "Standup moved to half past nine."
    finally:
        db.close()
