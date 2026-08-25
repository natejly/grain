"""SKILL.md interop: `POST /api/skills/import` and `GET /api/skills/{id}/export`.

The bridge itself (`services/skill_markdown`) is unit-tested in
`test_skill_markdown.py`; these tests prove the *routes* wire it correctly —
the parser's message surfaces as the 422 detail, a name collision answers the
same 409 the create route does, an idempotent replay returns the same skill,
and export emits a downloadable file that imports back without drift.

Every skill row these tests create lives in the shared seeded workspace, so
each test deletes what it made (the DELETE route cascades the versions).
"""
from __future__ import annotations

import uuid

IMPORT = "/api/skills/import"


def _headers() -> dict:
    return {"Idempotency-Key": "interop-" + uuid.uuid4().hex}


def _skill_md(
    name: str,
    *,
    title: str | None = None,
    description: str = "Sum up the week",
    body: str = "Do the thing.\n",
) -> str:
    lines = ["---", f"name: {name}", f"description: {description}"]
    if title is not None:
        lines.append(f"title: {title}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def _delete_skill(client, skill_id: str) -> None:
    client.delete(f"/api/skills/{skill_id}")


def test_import_creates_a_private_argless_skill_with_a_derived_title(client):
    """A minimal two-key SKILL.md lands as a private skill with no args, its
    title reconstructed from the slug ('my-skill' -> 'My Skill')."""
    name = "my-skill"
    response = client.post(
        IMPORT, headers=_headers(), json={"markdown": _skill_md(name)}
    )
    assert response.status_code == 201
    skill = response.json()
    try:
        assert skill["name"] == name
        assert skill["title"] == "My Skill"
        assert skill["description"] == "Sum up the week"
        assert skill["body"] == "Do the thing.\n"
        assert skill["args"] == []
        assert skill["shared"] is False
    finally:
        _delete_skill(client, skill["id"])


def test_import_refuses_bad_frontmatter_with_the_parsers_message(client):
    """Format errors are 422s carrying the parser's user-ready message, not a
    generic validation blob."""
    missing_name = client.post(
        IMPORT,
        headers=_headers(),
        json={"markdown": "---\ndescription: no name here\n---\nBody\n"},
    )
    assert missing_name.status_code == 422
    assert missing_name.json()["detail"] == "frontmatter is missing 'name'"

    no_fence = client.post(
        IMPORT, headers=_headers(), json={"markdown": "just a paragraph"}
    )
    assert no_fence.status_code == 422
    assert (
        no_fence.json()["detail"]
        == "SKILL.md must start with a --- frontmatter block"
    )


def test_import_colliding_with_an_existing_name_is_a_409(client):
    name = "interop-collision-" + uuid.uuid4().hex[:8]
    first = client.post(IMPORT, headers=_headers(), json={"markdown": _skill_md(name)})
    assert first.status_code == 201
    try:
        second = client.post(
            IMPORT, headers=_headers(), json={"markdown": _skill_md(name)}
        )
        assert second.status_code == 409
        assert name in second.json()["detail"]
    finally:
        _delete_skill(client, first.json()["id"])


def test_import_replayed_on_the_same_idempotency_key_creates_one_skill(client):
    name = "interop-replay-" + uuid.uuid4().hex[:8]
    headers = _headers()
    first = client.post(IMPORT, headers=headers, json={"markdown": _skill_md(name)})
    assert first.status_code == 201
    try:
        replay = client.post(
            IMPORT, headers=headers, json={"markdown": _skill_md(name)}
        )
        assert replay.status_code == 201
        assert replay.json()["id"] == first.json()["id"]
        listed = [
            skill
            for skill in client.get("/api/skills").json()
            if skill["name"] == name
        ]
        assert len(listed) == 1
    finally:
        _delete_skill(client, first.json()["id"])


def test_export_is_a_markdown_attachment_and_round_trips_through_import(client):
    """import(export(x)) preserves name/title/description/body — the law that
    lets a skill leave for a git repo and come back without drift."""
    name = "interop-roundtrip-" + uuid.uuid4().hex[:8]
    markdown = _skill_md(
        name,
        title="Weekly Digest",
        description="Summarize: the week in review",
        body="First line.\n\nSecond paragraph with {{ placeholder }}.\n",
    )
    created = client.post(IMPORT, headers=_headers(), json={"markdown": markdown})
    assert created.status_code == 201
    original = created.json()

    exported = client.get(f"/api/skills/{original['id']}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/markdown")
    assert (
        exported.headers["content-disposition"] == 'attachment; filename="SKILL.md"'
    )

    # Delete between the trips so the re-import is not a name collision.
    assert client.delete(f"/api/skills/{original['id']}").status_code == 204

    reimported = client.post(
        IMPORT, headers=_headers(), json={"markdown": exported.text}
    )
    assert reimported.status_code == 201
    round_tripped = reimported.json()
    try:
        for field in ("name", "title", "description", "body"):
            assert round_tripped[field] == original[field]
    finally:
        _delete_skill(client, round_tripped["id"])


def test_export_of_a_missing_or_foreign_skill_is_a_404(client, identity_client):
    assert client.get(f"/api/skills/{uuid.uuid4()}/export").status_code == 404

    other = identity_client(name="Exporter", workspace_name="Foreign skills")
    foreign = other.post(
        IMPORT,
        headers=_headers(),
        json={"markdown": _skill_md("interop-foreign-" + uuid.uuid4().hex[:8])},
    )
    assert foreign.status_code == 201
    foreign_id = foreign.json()["id"]
    try:
        # A foreign id must be indistinguishable from a missing one.
        assert client.get(f"/api/skills/{foreign_id}/export").status_code == 404
        # And its owner can still export it, proving the 404 above was scoping.
        assert other.get(f"/api/skills/{foreign_id}/export").status_code == 200
    finally:
        _delete_skill(other, foreign_id)
