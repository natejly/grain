from __future__ import annotations

import time


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
    for _ in range(50):
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
