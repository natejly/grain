from __future__ import annotations

import socket
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Chunk, Source
from app.services.retrieval import search_evidence
from app.services.tools import ToolSecurityError, validate_public_https_url


def test_tool_url_rejects_http_and_non_allowlisted_hosts():
    settings = Settings(tool_host_allowlist="api.github.com")
    with pytest.raises(ToolSecurityError, match="HTTPS"):
        validate_public_https_url("http://api.github.com/zen", settings)
    with pytest.raises(ToolSecurityError, match="allowlist"):
        validate_public_https_url("https://example.com", settings)


def test_tool_url_rejects_private_resolution(monkeypatch):
    settings = Settings(tool_host_allowlist="api.github.com")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(ToolSecurityError, match="blocked network"):
        validate_public_https_url("https://api.github.com/zen", settings)


def test_retrieval_is_workspace_scoped(client):
    from app.auth import DEV_SEED_USER_ID, DEV_SEED_WORKSPACE_ID
    from app.database import SessionLocal

    db: Session = SessionLocal()
    try:
        source = Source(
            workspace_id=DEV_SEED_WORKSPACE_ID,
            created_by=DEV_SEED_USER_ID,
            filename="retrieval-fixture.md",
            media_type="text/markdown",
            object_key="/tmp/not-used",
            byte_size=1,
            status="ready",
            chunk_count=1,
        )
        db.add(source)
        db.flush()
        db.add(
            Chunk(
                workspace_id=DEV_SEED_WORKSPACE_ID,
                source_id=source.id,
                ordinal=0,
                content="Project Juniper uses a violet deployment ring for canary releases.",
                char_start=0,
                char_end=72,
                token_count=11,
            )
        )
        db.commit()
        results = search_evidence(
            db,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            query="What color is the Juniper deployment ring?",
        )
        assert results
        assert results[0].filename == "retrieval-fixture.md"
        assert (
            search_evidence(
                db,
                workspace_id="22222222-2222-4222-8222-222222222222",
                query="Juniper deployment ring",
            )
            == []
        )
    finally:
        db.close()


# --- serving a source's original bytes -------------------------------------
#
# Every file this workspace holds had a row, a size and a listing but no way to
# open it, which made every sandbox-drawn chart invisible. Serving bytes back is
# the part of that hole with teeth: the response tells a browser what to *do*
# with them, and this API is the origin the session cookie belongs to.


def _stored_source(media_type: str, data: bytes, filename: str) -> str:
    """A Source row with its bytes on disk, the way an artifact is written."""
    from app.auth import DEV_SEED_USER_ID, DEV_SEED_WORKSPACE_ID
    from app.database import SessionLocal
    from app.services.ingestion import object_path

    db: Session = SessionLocal()
    try:
        source = Source(
            workspace_id=DEV_SEED_WORKSPACE_ID,
            created_by=DEV_SEED_USER_ID,
            filename=filename,
            media_type=media_type,
            object_key="",
            byte_size=len(data),
            status="stored",
        )
        db.add(source)
        db.flush()
        path = object_path(DEV_SEED_WORKSPACE_ID, source.id, filename)
        path.write_bytes(data)
        source.object_key = str(path)
        db.commit()
        return source.id
    finally:
        db.close()


PNG = bytes.fromhex("89504e470d0a1a0a") + b"fake-figure-bytes"


def test_a_stored_chart_is_served_inline_as_its_own_image_type(client):
    source_id = _stored_source("image/png", PNG, "sandbox-png-1.png")

    response = client.get(f"/api/sources/{source_id}/content")

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    # Inline, so <img src> works. A raster image cannot execute anything.
    assert response.headers["content-disposition"].startswith("inline")
    assert "sandbox-png-1.png" in response.headers["content-disposition"]
    # nosniff matters most on this route: it is what stops a browser from
    # deciding for itself that these bytes are really HTML.
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    ("media_type", "filename", "data"),
    [
        # Both are script containers, and both are on the served-types list, so
        # this is the case where "trusted Content-Type" and "safe to render" come
        # apart. Inline here would execute as the API's own origin — the origin
        # holding the session cookie.
        ("image/svg+xml", "figure.svg", b"<svg onload='alert(1)' xmlns='#'/>"),
        ("text/html", "report.html", b"<script>alert(1)</script>"),
        ("application/pdf", "paper.pdf", b"%PDF-1.4 fake"),
    ],
)
def test_anything_that_can_execute_is_downloaded_and_never_rendered(
    client, media_type, filename, data
):
    source_id = _stored_source(media_type, data, filename)

    response = client.get(f"/api/sources/{source_id}/content")

    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-disposition"].startswith("attachment")


def test_an_unrecognised_media_type_is_not_echoed_back_to_the_browser(client):
    """`media_type` on an upload is whatever the client's multipart part claimed,
    so it is caller-controlled input, not a fact about the bytes."""
    source_id = _stored_source("text/html;charset=utf-8, x=1", b"payload", "odd.bin")

    response = client.get(f"/api/sources/{source_id}/content")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment")


def test_a_key_pointing_outside_the_workspace_directory_serves_nothing(client, tmp_path):
    """The stored key is a path this app wrote, but it is still a string in a
    database. Containment is asserted against where the bytes must be."""
    from app.database import SessionLocal

    outside = tmp_path / "secrets.env"
    outside.write_text("OPENAI_API_KEY=real")
    source_id = _stored_source("text/plain", b"decoy", "decoy.txt")

    db: Session = SessionLocal()
    try:
        source = db.get(Source, source_id)
        assert source is not None
        # Traversal through the key, which is how it would arrive: a relative
        # walk out of the workspace directory rather than an absolute path.
        source.object_key = str(
            Path(source.object_key).parent / ".." / ".." / ".." / outside.name
        )
        db.commit()
    finally:
        db.close()

    # The file at the other end of that walk exists, so a pass would be a leak
    # rather than a miss.
    assert outside.is_file()
    assert client.get(f"/api/sources/{source_id}/content").status_code == 404


def test_a_row_whose_file_is_gone_is_404_rather_than_a_500(client):
    from app.database import SessionLocal

    source_id = _stored_source("text/plain", b"here for now", "gone.txt")
    db: Session = SessionLocal()
    try:
        source = db.get(Source, source_id)
        assert source is not None
        Path(source.object_key).unlink()
    finally:
        db.close()

    assert client.get(f"/api/sources/{source_id}/content").status_code == 404


def test_a_file_above_the_ceiling_is_refused_rather_than_streamed(client, monkeypatch):
    from app.config import get_settings

    source_id = _stored_source("text/plain", b"x" * 64, "big.txt")
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 16)

    assert client.get(f"/api/sources/{source_id}/content").status_code == 413


def test_a_deleted_source_stops_being_downloadable(client):
    from app.clock import utcnow
    from app.database import SessionLocal

    source_id = _stored_source("text/plain", b"retracted", "retracted.txt")
    assert client.get(f"/api/sources/{source_id}/content").status_code == 200

    db: Session = SessionLocal()
    try:
        source = db.get(Source, source_id)
        assert source is not None
        source.deleted_at = utcnow()
        db.commit()
    finally:
        db.close()

    assert client.get(f"/api/sources/{source_id}/content").status_code == 404


def test_downloading_a_source_requires_a_session(anonymous_client, client):
    source_id = _stored_source("text/plain", b"private", "private.txt")

    assert anonymous_client.get(f"/api/sources/{source_id}/content").status_code == 401
