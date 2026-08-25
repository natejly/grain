from __future__ import annotations

import io
import time

import pytest

from app.services.app_codegen import AppCodegenError, lint_generated_html


def _upload_csv(client, name: str) -> str:
    csv_bytes = b"team,revenue\nAtlas,120\nOrion,80\n"
    response = client.post(
        "/api/sources",
        headers={"Idempotency-Key": f"codegen-upload-{name}"},
        files={"file": (f"{name}.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert response.status_code == 202
    source_id = response.json()["id"]
    for _ in range(50):
        sources = client.get("/api/sources").json()
        current = next(item for item in sources if item["id"] == source_id)
        if current["status"] == "ready":
            return source_id
        time.sleep(0.1)
    raise AssertionError("source did not become ready")


@pytest.fixture(scope="module")
def code_app(client):
    source_id = _upload_csv(client, "codegen-revenue")
    dataset = client.post(
        "/api/datasets",
        headers={"Idempotency-Key": "codegen-dataset-1"},
        json={"name": "codegen-revenue", "description": "", "source_id": source_id},
    ).json()
    app = client.post(
        "/api/apps",
        headers={"Idempotency-Key": "codegen-app-1"},
        json={
            "name": "Revenue Studio",
            "slug": "revenue-studio",
            "visibility": "public",
            "app_type": "code",
            "dashboard_ids": [],
        },
    ).json()
    return {"app": app, "dataset": dataset}


def test_code_app_created_without_release(code_app):
    assert code_app["app"]["app_type"] == "code"
    assert code_app["app"]["releases"] == []


def test_generate_creates_code_draft_with_snapshots(client, code_app):
    response = client.post(
        f"/api/apps/{code_app['app']['id']}/generate",
        headers={"Idempotency-Key": "codegen-generate-1"},
        json={
            "prompt": "Show revenue by team",
            "dataset_ids": [code_app["dataset"]["id"]],
        },
    )
    assert response.status_code == 201
    release = response.json()["releases"][0]
    assert release["status"] == "draft"
    manifest = release["manifest"]
    assert manifest["kind"] == "code"
    assert manifest["schema_version"] == 2
    assert "window.grain" in manifest["html"]
    # The aliases are the compatibility contract for app bodies written against
    # the old names; dropping either silently breaks them.
    assert "window.jasmine = window.grain;" in manifest["html"]
    assert "window.fieldnote = window.grain;" in manifest["html"]
    assert manifest["data_bindings"] == [
        {"dataset_id": code_app["dataset"]["id"], "name": "codegen-revenue"}
    ]
    assert manifest["snapshots"]["codegen-revenue"]["row_count"] == 2


def test_preview_frame_serves_html_with_lockdown_csp(client, code_app):
    app = client.get("/api/apps").json()
    target = next(item for item in app if item["id"] == code_app["app"]["id"])
    release_id = target["releases"][0]["id"]
    response = client.get(
        f"/api/apps/{code_app['app']['id']}/releases/{release_id}/frame"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "connect-src 'none'" in csp
    assert "frame-ancestors" in csp
    assert response.headers["cache-control"] == "no-store"
    assert "window.grain" in response.text
    assert "window.jasmine = window.grain;" in response.text
    assert "window.fieldnote = window.grain;" in response.text


def test_published_frame_requires_publication(client, code_app):
    # Not published yet: the public frame must 404.
    response = client.get("/published/apps/revenue-studio/frame")
    assert response.status_code == 404

    apps = client.get("/api/apps").json()
    target = next(item for item in apps if item["id"] == code_app["app"]["id"])
    release_id = target["releases"][0]["id"]
    published = client.post(
        f"/api/apps/{code_app['app']['id']}/releases/{release_id}/publish",
        headers={"Idempotency-Key": "codegen-publish-1"},
    )
    assert published.status_code == 200

    response = client.get("/published/apps/revenue-studio/frame")
    assert response.status_code == 200
    assert "default-src 'none'" in response.headers["content-security-policy"]


def test_frame_is_workspace_scoped(client, code_app):
    apps = client.get("/api/apps").json()
    target = next(item for item in apps if item["id"] == code_app["app"]["id"])
    release_id = target["releases"][0]["id"]
    denied = client.get(
        f"/api/apps/{code_app['app']['id']}/releases/{release_id}/frame",
        headers={"X-Workspace-ID": "44444444-4444-4444-8444-444444444444"},
    )
    assert denied.status_code == 403


def test_generate_rejects_dashboard_apps(client, code_app):
    dashboard_app = client.post(
        "/api/apps",
        headers={"Idempotency-Key": "codegen-app-2"},
        json={
            "name": "Classic",
            "slug": "classic-dashboards",
            "visibility": "private",
            "app_type": "dashboard",
            "dashboard_ids": [],
        },
    )
    # Dashboard apps still require at least one dashboard.
    assert dashboard_app.status_code == 422


def test_lint_blocks_external_references_and_size():
    with pytest.raises(AppCodegenError):
        lint_generated_html('<script src="https://evil.example/x.js"></script>')
    with pytest.raises(AppCodegenError):
        lint_generated_html('<link href="//cdn.example/style.css">')
    with pytest.raises(AppCodegenError):
        lint_generated_html('<meta http-equiv="refresh" content="0;url=x">')
    with pytest.raises(AppCodegenError):
        lint_generated_html("x" * (256 * 1024 + 1))
    lint_generated_html("<style>body{}</style><script>var a = 1;</script>")
