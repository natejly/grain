from __future__ import annotations

import socket

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
