from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.config import get_settings
from app.database import SessionLocal
from app.models import IntegrationAccount
from app.services.connectors import base as connector_base
from app.services.crypto import decrypt_secret


@pytest.fixture()
def integration_settings():
    settings = get_settings()
    original = (
        settings.integrations_encryption_key,
        settings.google_client_id,
        settings.google_client_secret,
        settings.strava_client_id,
        settings.strava_client_secret,
    )
    settings.integrations_encryption_key = SecretStr(Fernet.generate_key().decode())
    settings.google_client_id = "google-client"
    settings.google_client_secret = SecretStr("google-secret")
    settings.strava_client_id = "strava-client"
    settings.strava_client_secret = SecretStr("strava-secret")
    yield settings
    (
        settings.integrations_encryption_key,
        settings.google_client_id,
        settings.google_client_secret,
        settings.strava_client_id,
        settings.strava_client_secret,
    ) = original


def _gmail_transport() -> httpx.MockTransport:
    body = base64.urlsafe_b64encode(b"Quarterly Atlas budget update").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "google-access",
                    "refresh_token": "google-refresh",
                    "expires_in": 3600,
                    "scope": "gmail.readonly",
                },
            )
        if "/profile" in url:
            return httpx.Response(200, json={"emailAddress": "nate@example.com"})
        if url.endswith("/messages") or "/messages?" in url:
            return httpx.Response(
                200, json={"messages": [{"id": "m1"}, {"id": "m2"}]}
            )
        if "/messages/" in url:
            message_id = urlparse(url).path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "id": message_id,
                    "threadId": "t1",
                    "snippet": "Budget attached",
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {"name": "Subject", "value": f"Atlas budget {message_id}"},
                            {"name": "From", "value": "cfo@example.com"},
                            {"name": "To", "value": "nate@example.com"},
                            {"name": "Date", "value": "Mon, 1 Jun 2026 09:00:00 +0000"},
                        ],
                        "body": {"data": body},
                    },
                },
            )
        raise AssertionError(f"Unexpected URL in test: {url}")

    return httpx.MockTransport(handler)


def test_connect_returns_authorize_url(client, integration_settings):
    response = client.post(
        "/api/integrations/google/connect",
        headers={"Idempotency-Key": "integration-connect-1"},
    )
    assert response.status_code == 200
    url = response.json()["authorize_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    params = parse_qs(urlparse(url).query)
    assert params["client_id"] == ["google-client"]
    assert params["state"][0]


def test_connect_requires_configuration(client):
    response = client.post(
        "/api/integrations/google/connect",
        headers={"Idempotency-Key": "integration-connect-2"},
    )
    assert response.status_code == 400


def test_callback_rejects_bad_state(client, integration_settings):
    response = client.get(
        "/api/integrations/google/callback?code=abc&state=not-a-real-state",
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_google_oauth_flow_stores_encrypted_tokens_and_syncs(
    client, integration_settings
):
    connect = client.post(
        "/api/integrations/google/connect",
        headers={"Idempotency-Key": "integration-connect-3"},
    ).json()
    state = parse_qs(urlparse(connect["authorize_url"]).query)["state"][0]

    connector_base.HTTP_TRANSPORT = _gmail_transport()
    try:
        callback = client.get(
            f"/api/integrations/google/callback?code=auth-code&state={state}",
            follow_redirects=False,
        )
        assert callback.status_code == 307
        assert "connected=google" in callback.headers["location"]

        listing = client.get("/api/integrations").json()
        google = next(item for item in listing if item["provider"] == "google")
        assert google["configured"] is True
        assert google["account"]["external_account"] == "nate@example.com"
        account_id = google["account"]["id"]

        db = SessionLocal()
        try:
            account = db.get(IntegrationAccount, account_id)
            assert account.access_token_enc != "google-access"
            assert (
                decrypt_secret(account.access_token_enc, integration_settings)
                == "google-access"
            )
        finally:
            db.close()

        synced = client.post(
            f"/api/integrations/{account_id}/sync",
            headers={"Idempotency-Key": "integration-sync-1"},
        )
        assert synced.status_code == 202
        jobs = client.get(f"/api/integrations/{account_id}/jobs").json()
        assert jobs[0]["status"] == "succeeded"
        assert jobs[0]["stats"]["messages"] == 2

        sources = client.get("/api/sources").json()
        gmail_sources = [s for s in sources if s["filename"].startswith("gmail-")]
        assert gmail_sources, "gmail sync should land sources"
        assert any(s["status"] == "ready" for s in gmail_sources)

        datasets = client.get("/api/datasets").json()
        gmail_dataset = next(d for d in datasets if d["name"] == "gmail-messages")
        assert gmail_dataset["row_count"] == 2

        # Scoping: another workspace sees nothing.
        denied = client.get(
            "/api/integrations",
            headers={"X-Workspace-ID": "33333333-3333-4333-8333-333333333333"},
        )
        assert denied.status_code == 403
    finally:
        connector_base.HTTP_TRANSPORT = None


def test_disconnect_removes_account(client, integration_settings):
    listing = client.get("/api/integrations").json()
    google = next(item for item in listing if item["provider"] == "google")
    if google["account"] is None:
        pytest.skip("depends on the oauth flow test creating an account")
    response = client.delete(
        f"/api/integrations/{google['account']['id']}",
        headers={"Idempotency-Key": "integration-disconnect-1"},
    )
    assert response.status_code == 204
    listing = client.get("/api/integrations").json()
    google = next(item for item in listing if item["provider"] == "google")
    assert google["account"] is None


class _FakeGarth:
    profile = {"displayName": "nate-garmin", "fullName": "Nate J"}

    def __init__(self):
        self.token = None

    def login(self, email, password, prompt_mfa=None):
        if password != "correct-horse":
            raise RuntimeError("bad credentials")

    def dumps(self):
        return "fake-garmin-session"

    def loads(self, value):
        assert value == "fake-garmin-session"


class _FakeGarmin:
    def __init__(self, email=None, password=None, is_cn=False):
        self.username = email
        self.password = password
        self.garth = _FakeGarth()
        self.display_name = ""
        self.full_name = ""

    def get_activities(self, start, limit):
        return [
            {
                "activityId": 101,
                "activityName": "Morning Run",
                "activityType": {"typeKey": "running"},
                "startTimeLocal": "2026-07-26 07:00:00",
                "distance": 5000.0,
                "duration": 1500.0,
                "elevationGain": 40.0,
                "averageHR": 150.0,
                "maxHR": 172.0,
                "calories": 380.0,
            }
        ]


def test_garmin_credentials_flow_and_sync(client, integration_settings, monkeypatch):
    from app.services.connectors import garmin as garmin_module

    monkeypatch.setattr(garmin_module, "Garmin", _FakeGarmin)

    rejected = client.post(
        "/api/integrations/garmin/credentials",
        headers={"Idempotency-Key": "garmin-connect-bad"},
        json={"email": "nate@example.com", "password": "wrong"},
    )
    assert rejected.status_code == 400

    connected = client.post(
        "/api/integrations/garmin/credentials",
        headers={"Idempotency-Key": "garmin-connect-1"},
        json={"email": "nate@example.com", "password": "correct-horse"},
    )
    assert connected.status_code == 201
    account = connected.json()
    assert account["provider"] == "garmin"

    from app.database import SessionLocal as _SessionLocal
    from app.models import IntegrationAccount as _IntegrationAccount

    db = _SessionLocal()
    try:
        row = db.get(_IntegrationAccount, account["id"])
        assert row.refresh_token_enc != "fake-garmin-session"
        assert (
            decrypt_secret(row.refresh_token_enc, integration_settings)
            == "fake-garmin-session"
        )
    finally:
        db.close()

    synced = client.post(
        f"/api/integrations/{account['id']}/sync",
        headers={"Idempotency-Key": "garmin-sync-1"},
    )
    assert synced.status_code == 202
    jobs = client.get(f"/api/integrations/{account['id']}/jobs").json()
    assert jobs[0]["status"] == "succeeded"
    assert jobs[0]["stats"]["activities"] == 1

    datasets = client.get("/api/datasets").json()
    garmin_dataset = next(d for d in datasets if d["name"] == "garmin-activities")
    assert garmin_dataset["row_count"] == 1
