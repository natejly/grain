"""GET /api/documents/{id}/versions/{version_id}: one version's snapshot.

The list route deliberately ships id/summary/created_at and no content —
version bodies are unbounded and the history panel is hot. This route is the
lazy half: the client asks for one version's content only when the stepper
selects it. The scoping worth pinning is the cross-document case inside ONE
workspace, which isolation.py cannot express: a version id that exists, in
the caller's workspace, but belongs to another document, answers 404.
"""
from __future__ import annotations

import uuid


def _create_document(client, title: str) -> str:
    response = client.post(
        "/api/documents", json={"title": title, "content": "v1 body\n"}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _save(client, document_id: str, content: str) -> None:
    response = client.put(f"/api/documents/{document_id}", json={"content": content})
    assert response.status_code == 200, response.text


def test_version_content_round_trips_after_two_saves(client) -> None:
    document_id = _create_document(client, f"Stepper {uuid.uuid4().hex[:8]}")
    _save(client, document_id, "v2 body\n")
    _save(client, document_id, "v3 body\n")

    versions = client.get(f"/api/documents/{document_id}/versions").json()
    # Two saves snapshot the two prior bodies; newest first per list_versions.
    assert len(versions) == 2

    by_id = {}
    for version in versions:
        response = client.get(
            f"/api/documents/{document_id}/versions/{version['id']}"
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        # Same identity fields the list gave, plus the one thing it withheld.
        assert payload["id"] == version["id"]
        assert payload["summary"] == version["summary"]
        assert payload["created_at"] == version["created_at"]
        by_id[payload["id"]] = payload["content"]

    assert set(by_id.values()) == {"v1 body\n", "v2 body\n"}


def test_a_version_of_another_document_in_the_same_workspace_is_404(client) -> None:
    """The cross-document probe: right workspace, wrong document."""
    doc_a = _create_document(client, f"Doc A {uuid.uuid4().hex[:8]}")
    doc_b = _create_document(client, f"Doc B {uuid.uuid4().hex[:8]}")
    _save(client, doc_b, "doc b, edited\n")

    version_of_b = client.get(f"/api/documents/{doc_b}/versions").json()[0]["id"]
    response = client.get(f"/api/documents/{doc_a}/versions/{version_of_b}")
    assert response.status_code == 404

    # And through its own document it still answers.
    ok = client.get(f"/api/documents/{doc_b}/versions/{version_of_b}")
    assert ok.status_code == 200
    assert ok.json()["content"] == "v1 body\n"


def test_a_missing_document_is_404_before_the_version_is_consulted(client) -> None:
    response = client.get(
        "/api/documents/not-a-document/versions/not-a-version"
    )
    assert response.status_code == 404
