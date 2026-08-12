"""Passwordless magic-link sign-in: request is not an enumeration oracle, and
the link is a single-use, expiring, hash-at-rest credential that mints a real
session — exactly the reset flow's posture, reused verbatim for login.
"""
from __future__ import annotations

import uuid

import pytest
from conftest import TEST_BASE_URL
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.clock import utcnow
from app.database import SessionLocal
from app.main import app
from app.models import EmailToken, User
from app.services.auth import email as email_service

PASSWORD = "correct-horse-battery-staple"


def unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


def fresh_client() -> TestClient:
    return TestClient(app, base_url=TEST_BASE_URL)


def signup(client: TestClient, email: str, password: str = PASSWORD, name: str = "Sam"):
    return client.post(
        "/api/auth/signup", json={"email": email, "password": password, "name": name}
    )


def request_link(client: TestClient, email: str):
    return client.post("/api/auth/login-link/request", json={"email": email})


def consume_link(client: TestClient, token: str):
    return client.post("/api/auth/login-link/consume", json={"token": token})


@pytest.fixture
def sent_emails(monkeypatch):
    """Capture outbound mail instead of printing it."""
    captured: list[email_service.OutboundEmail] = []

    class Capturing:
        def send(self, message: email_service.OutboundEmail) -> None:
            captured.append(message)

    monkeypatch.setattr(email_service, "get_email_sender", lambda settings: Capturing())
    return captured


def token_from(message: email_service.OutboundEmail) -> str:
    return message.body.split("token=")[1].split()[0]


def test_login_link_request_answers_identically_for_known_and_unknown(sent_emails):
    """The whole point of the request half: it must not say which addresses exist.

    Body and status are byte-identical for a registered and an unregistered
    address, and — mirroring the reset request — exactly one message is sent on
    both branches so response *time* is not the oracle the identical body hides.
    Only the registered branch carries a real token in it.
    """
    client = fresh_client()
    known = unique_email()
    signup(client, known)
    sent_emails.clear()

    known_resp = request_link(fresh_client(), known)
    unknown_resp = request_link(fresh_client(), unique_email())

    assert known_resp.status_code == unknown_resp.status_code == 202
    assert known_resp.json() == unknown_resp.json()
    # One message per request, whichever branch ran.
    assert len(sent_emails) == 2
    assert sent_emails[0].subject == "Your sign-in link"
    assert "token=" in sent_emails[0].body
    # The unknown address gets mail too, but no live credential is minted for it.
    assert "token=" not in sent_emails[1].body


def test_login_link_mints_a_working_session_without_email_verification(sent_emails):
    """Redeeming a link lands you in a real session, verified mailbox or not.

    Signup leaves ``email_verified_at`` NULL, yet clicking a link emailed to that
    address is itself the mailbox proof login accepts — so consume mints a
    session (a usable /me and /bootstrap) exactly like ``_issue_login``, and
    never gates on verification the way an address-as-identity flow would.
    """
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    sent_emails.clear()

    assert request_link(client, email).status_code == 202
    token = token_from(sent_emails[-1])

    guest = fresh_client()
    consumed = consume_link(guest, token)
    assert consumed.status_code == 200
    body = consumed.json()
    assert body["user_email"] == email
    assert body["role"] == "owner"
    assert body["csrf_token"]
    # The mailbox was never confirmed, yet the session is fully usable.
    assert body["email_verified"] is False
    assert guest.get("/api/auth/me").status_code == 200
    assert guest.get("/api/bootstrap").status_code == 200


def test_login_link_is_single_use(sent_emails):
    """A redeemed link is a one-way door: the second consume gets nothing."""
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    sent_emails.clear()
    request_link(client, email)
    token = token_from(sent_emails[-1])

    first = consume_link(fresh_client(), token)
    assert first.status_code == 200
    # Spent. A replay is rejected with the same generic 400 an unknown token gets.
    second = consume_link(fresh_client(), token)
    assert second.status_code == 400
    assert second.json()["detail"] == "Invalid or expired token"


def test_login_link_expires(sent_emails):
    """Past its short TTL the link is dead, even though it was never redeemed."""
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    sent_emails.clear()
    request_link(client, email)
    token = token_from(sent_emails[-1])

    db = SessionLocal()
    try:
        stored = db.scalar(
            select(EmailToken).where(
                EmailToken.token_hash == email_service.hash_token(token),
                EmailToken.purpose == email_service.PURPOSE_LOGIN,
            )
        )
        assert stored is not None
        # Backdate expiry so the consume UPDATE's `expires_at > now` filter misses.
        stored.expires_at = utcnow() - stored.expires_at.resolution
        db.commit()
    finally:
        db.close()

    expired = consume_link(fresh_client(), token)
    assert expired.status_code == 400
    assert expired.json()["detail"] == "Invalid or expired token"


def test_login_link_tokens_are_stored_only_as_hashes(sent_emails):
    """The raw link is never at rest; only its SHA-256 digest is."""
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    sent_emails.clear()
    request_link(client, email)
    token = token_from(sent_emails[-1])

    db = SessionLocal()
    try:
        assert (
            db.scalar(select(EmailToken).where(EmailToken.token_hash == token)) is None
        )
        assert (
            db.scalar(
                select(EmailToken).where(
                    EmailToken.token_hash == email_service.hash_token(token),
                    EmailToken.purpose == email_service.PURPOSE_LOGIN,
                )
            )
            is not None
        )
    finally:
        db.close()


def test_a_tampered_or_foreign_token_is_rejected(sent_emails):
    """Only the exact bytes emailed for *this purpose* redeem — nothing else.

    A mangled token, pure garbage, and a validly-issued token for a *different*
    purpose (a real reset link) all fail with the same generic 400: the consume
    UPDATE keys on both the hash and ``purpose=login``, so a reset token can no
    more sign you in than a random string can.
    """
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    sent_emails.clear()
    request_link(client, email)
    good = token_from(sent_emails[-1])

    # A single flipped character no longer hashes to the stored digest.
    tampered = good[:-1] + ("a" if good[-1] != "a" else "b")
    assert consume_link(fresh_client(), tampered).status_code == 400
    assert consume_link(fresh_client(), "not-a-real-token").status_code == 400

    # A genuine reset token for the same user is the wrong *purpose* here.
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        reset_raw = email_service.issue_email_token(
            db,
            user_id=user.id,
            purpose=email_service.PURPOSE_PASSWORD_RESET,
            ttl=email_service.RESET_TOKEN_TTL,
        )
        db.commit()
    finally:
        db.close()
    foreign = consume_link(fresh_client(), reset_raw)
    assert foreign.status_code == 400
    assert foreign.json()["detail"] == "Invalid or expired token"
    # And the good link still works after all the rejected attempts.
    assert consume_link(fresh_client(), good).status_code == 200


def test_login_link_consume_rejects_a_disabled_account(sent_emails):
    """A non-active account gets the same generic 400 — status is not an oracle.

    ``login`` refuses a disabled user; the magic link must not be a side door
    around that, and it must refuse with the same uninformative message so the
    account's state never leaks.
    """
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    sent_emails.clear()
    request_link(client, email)
    token = token_from(sent_emails[-1])

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.status = "disabled"
        db.commit()
    finally:
        db.close()

    rejected = consume_link(fresh_client(), token)
    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "Invalid or expired token"
