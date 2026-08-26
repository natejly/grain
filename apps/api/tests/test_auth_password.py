"""Password auth: signup, login, lockout, reset, verification, hashing."""
from __future__ import annotations

import uuid

import pytest
from conftest import TEST_BASE_URL
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import EmailToken, Membership, User, UserSession, Workspace
from app.services.auth import email as email_service
from app.services.auth.passwords import (
    EQUALIZER_PLAINTEXT,
    PasswordPolicyError,
    hash_password,
    validate_password,
    verify_password,
)

PASSWORD = "correct-horse-battery-staple"


def unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


def fresh_client() -> TestClient:
    return TestClient(app, base_url=TEST_BASE_URL)


def signup(client: TestClient, email: str, password: str = PASSWORD, name: str = "Sam"):
    return client.post(
        "/api/auth/signup", json={"email": email, "password": password, "name": name}
    )


def login(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


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


def test_signup_creates_user_workspace_membership_and_agent(sent_emails):
    client = fresh_client()
    email = unique_email()
    response = signup(client, email)
    assert response.status_code == 202
    assert response.json()["status"] == "ok"

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        assert user.password_hash and user.password_hash.startswith("$argon2id$")
        assert user.email_verified_at is None
        membership = db.scalar(select(Membership).where(Membership.user_id == user.id))
        assert membership is not None
        assert membership.role == "owner"
        assert db.get(Workspace, membership.workspace_id) is not None
    finally:
        db.close()
    assert len(sent_emails) == 1
    assert sent_emails[0].to == email


def test_signup_answer_is_identical_for_a_registered_address(sent_emails):
    client = fresh_client()
    email = unique_email()
    first = signup(client, email)
    second = signup(client, email, password="a-completely-different-password")
    # Byte-identical: body and status. Anything else enumerates accounts.
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    # No second account, and the truth went to the mailbox instead.
    db = SessionLocal()
    try:
        assert len(list(db.scalars(select(User).where(User.email == email)))) == 1
    finally:
        db.close()
    assert sent_emails[-1].subject == "You already have an account"


def test_signup_rejects_a_short_password():
    response = signup(fresh_client(), unique_email(), password="short")
    assert response.status_code == 400
    assert "at least" in response.json()["detail"]


def test_login_sets_a_secure_cross_site_cookie_and_returns_a_csrf_token(sent_emails):
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    response = login(client, email)
    assert response.status_code == 200
    body = response.json()
    assert body["user_email"] == email
    assert body["role"] == "owner"
    assert body["csrf_token"]
    assert body["email_verified"] is False

    settings = get_settings()
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{settings.session_cookie_name}=")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=none" in cookie.replace("SameSite=None", "SameSite=none")
    assert "Path=/" in cookie
    # The raw token is never stored; only its digest is.
    raw = client.cookies[settings.session_cookie_name]
    db = SessionLocal()
    try:
        assert db.scalar(select(UserSession).where(UserSession.token_hash == raw)) is None
    finally:
        db.close()


def test_login_rejects_wrong_password_unknown_email_identically(sent_emails):
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    wrong = login(client, email, password="not-the-right-password")
    unknown = login(fresh_client(), unique_email(), password=PASSWORD)
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_a_null_password_hash_never_authenticates():
    """Federated-only accounts have no password and must stay unreachable."""
    email = unique_email()
    db = SessionLocal()
    try:
        user = User(email=email, name="Federated", password_hash=None)
        db.add(user)
        db.flush()
        workspace = Workspace(name="Federated workspace")
        db.add(workspace)
        db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        db.commit()
    finally:
        db.close()

    client = fresh_client()
    # An empty password is refused by the schema before it reaches the hash
    # comparison; everything else is refused by the comparison itself.
    assert login(client, email, password="").status_code == 422
    for attempt in (" ", "password", "None", "null", PASSWORD):
        assert login(client, email, password=attempt).status_code == 401
    assert verify_password(None, "anything").ok is False


def test_an_empty_password_hash_never_authenticates():
    """Regression: "no password" means falsy, not just NULL.

    ``verify_password`` fell back to the timing-equalizer hash whenever the
    stored hash was falsy but only re-checked ``stored_hash is None`` afterwards,
    so an empty-string hash — what a legacy import or a NOT NULL backfill leaves
    behind — reported ok=True to anyone who sent the equalizer plaintext. The
    account was one public constant away from being open to the world.
    """
    email = unique_email()
    db = SessionLocal()
    try:
        user = User(email=email, name="Blank", password_hash="")
        db.add(user)
        db.flush()
        workspace = Workspace(name="Blank workspace")
        db.add(workspace)
        db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        db.commit()
    finally:
        db.close()

    # The decision itself, at the unit boundary...
    assert verify_password("", EQUALIZER_PLAINTEXT).ok is False
    assert verify_password("", "anything else").ok is False
    assert verify_password(None, EQUALIZER_PLAINTEXT).ok is False
    # ...and over HTTP, where a 500 would have been an oracle of its own.
    for attempt in (EQUALIZER_PLAINTEXT, "None", "null", PASSWORD):
        response = login(fresh_client(), email, password=attempt)
        assert response.status_code == 401, (attempt, response.status_code)


def test_reset_request_does_the_same_work_for_a_known_and_unknown_address(monkeypatch):
    """Regression: the reset endpoint's *timing* used to enumerate accounts.

    The body and status were already identical, but only the registered branch
    sent mail — so with SMTP configured the response time differed by a network
    round trip (measured 59ms vs 2ms, a 29x tell). Both branches now send exactly
    one message, which is the same thing signup does for the same reason.
    """
    import time

    delivered: list[email_service.OutboundEmail] = []

    class SlowSender:
        """Stands in for SMTP: delivery costs real, measurable time."""

        def send(self, message: email_service.OutboundEmail) -> None:
            time.sleep(0.05)
            delivered.append(message)

    monkeypatch.setattr(email_service, "get_email_sender", lambda settings: SlowSender())

    known = unique_email()
    signup(fresh_client(), known)
    delivered.clear()

    def timed(address: str) -> float:
        get_settings()  # cached; keeps the call shapes identical
        start = time.perf_counter()
        response = fresh_client().post(
            "/api/auth/password/reset/request", json={"email": address}
        )
        assert response.status_code == 202
        return time.perf_counter() - start

    def best_of(address: str, attempts: int = 3) -> float:
        """The fastest of several runs, not one sample.

        Scheduler noise on a shared runner only ever ADDS time, so the minimum
        is the closest estimate of the work a branch actually performs — and an
        oracle, a branch that genuinely skips the 50ms send, is slower in every
        sample including its best. One sample each made this assertion fail on
        CI at 0.206s vs 0.054s while passing five times out of five locally on
        the same commit: it was measuring the runner, not the endpoint.
        """
        return min(timed(address) for _ in range(attempts))

    unknown = unique_email()
    attempts = 3
    known_elapsed = best_of(known, attempts)
    unknown_elapsed = best_of(unknown, attempts)

    # One message per request, whichever branch ran — the deterministic half of
    # this test, and the one that would catch the skipped send outright.
    assert len(delivered) == attempts * 2
    assert [m.to for m in delivered] == [known] * attempts + [unknown] * attempts
    assert delivered[0].subject == "Reset your password"
    assert delivered[attempts].subject == "Password reset requested"
    # And no reset token was minted for an address with no account.
    assert "token=" in delivered[0].body
    assert "token=" not in delivered[attempts].body
    # The wall clock must not separate them. Before the fix the unknown branch
    # skipped the 50ms send entirely; the bound is deliberately far looser than
    # that gap so the assertion is about the oracle, not about scheduler noise.
    #
    # Compare the FASTEST of several samples rather than one pair. Noise only
    # ever ADDS time -- a commit's fsync, a scheduler stall, a neighbour on a
    # shared runner -- so the minimum is the closest estimate of what the
    # endpoint really costs, and a systematic gap survives every sample while a
    # one-off stall survives none. Measuring one pair made this test fail in CI
    # at 206ms vs 54ms on a run where both branches provably sent their mail
    # (the assertions above passed), i.e. it failed on the noise it says here it
    # does not want to be about. The known branch legitimately does a little
    # more work -- issue_email_token's UPDATE and INSERT, and the commit that
    # follows -- and on a loaded runner that alone can clear 40ms.
    known_best = min([known_elapsed, *(timed(known) for _ in range(4))])
    unknown_best = min([unknown_elapsed, *(timed(unknown) for _ in range(4))])
    assert abs(known_best - unknown_best) < 0.04, (known_best, unknown_best)


def test_lockout_engages_and_does_not_announce_itself(sent_emails):
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    settings = get_settings()

    for _ in range(settings.login_max_failures):
        assert login(client, email, password="wrong-password-here").status_code == 401

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None and user.locked_until is not None
    finally:
        db.close()

    # The right password is now refused, and the refusal is indistinguishable
    # from the one an unknown address gets — a lockout that identifies itself is
    # an account-existence oracle.
    locked = login(client, email)
    unknown = login(client, unique_email())
    assert locked.status_code == unknown.status_code == 401
    assert locked.json() == unknown.json()


def test_login_is_rate_limited(monkeypatch, sent_emails):
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_rate_limit_attempts", 3)
    for _ in range(3):
        assert login(client, email, password="nope-nope-nope").status_code == 401
    # The fourth attempt never reaches the password check at all.
    assert login(client, email, password="nope-nope-nope").status_code == 429


def test_password_reset_is_single_use_and_revokes_every_session(sent_emails):
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    assert login(client, email).status_code == 200
    # Authenticated before the reset.
    assert client.get("/api/auth/me").status_code == 200

    request = client.post("/api/auth/password/reset/request", json={"email": email})
    assert request.status_code == 202
    token = token_from(sent_emails[-1])

    new_password = "an-entirely-new-passphrase"
    confirm = client.post(
        "/api/auth/password/reset/confirm",
        json={"token": token, "password": new_password},
    )
    assert confirm.status_code == 200
    # Every device was logged out, including this one.
    assert client.get("/api/auth/me").status_code == 401
    # The link is spent.
    assert (
        client.post(
            "/api/auth/password/reset/confirm",
            json={"token": token, "password": new_password},
        ).status_code
        == 400
    )
    fresh = fresh_client()
    assert login(fresh, email, password=PASSWORD).status_code == 401
    assert login(fresh, email, password=new_password).status_code == 200


def test_password_reset_request_answers_the_same_for_an_unknown_address(sent_emails):
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    known = client.post("/api/auth/password/reset/request", json={"email": email})
    unknown = client.post(
        "/api/auth/password/reset/request", json={"email": unique_email()}
    )
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()


def test_password_reset_clears_a_lockout(sent_emails):
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    for _ in range(get_settings().login_max_failures):
        login(client, email, password="wrong-password-here")

    client.post("/api/auth/password/reset/request", json={"email": email})
    token = token_from(sent_emails[-1])
    new_password = "another-fresh-passphrase"
    assert (
        client.post(
            "/api/auth/password/reset/confirm",
            json={"token": token, "password": new_password},
        ).status_code
        == 200
    )
    # Proving mailbox control ends the lockout, so a guessing spray cannot keep
    # the real owner out of their account indefinitely.
    assert login(fresh_client(), email, password=new_password).status_code == 200


def test_email_verification_consumes_the_token_once(sent_emails):
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    token = token_from(sent_emails[-1])
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 400
    assert login(client, email).json()["email_verified"] is True


def test_email_tokens_are_stored_only_as_hashes(sent_emails):
    client = fresh_client()
    email = unique_email()
    signup(client, email)
    token = token_from(sent_emails[-1])
    db = SessionLocal()
    try:
        assert db.scalar(select(EmailToken).where(EmailToken.token_hash == token)) is None
        assert (
            db.scalar(
                select(EmailToken).where(
                    EmailToken.token_hash == email_service.hash_token(token)
                )
            )
            is not None
        )
    finally:
        db.close()


def test_argon2_rehashes_when_the_cost_parameters_change(sent_emails, monkeypatch):
    from argon2 import PasswordHasher

    from app.services.auth import passwords

    client = fresh_client()
    email = unique_email()
    signup(client, email)

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        stale_hash = weak.hash(PASSWORD)
        user.password_hash = stale_hash
        db.commit()
    finally:
        db.close()

    assert passwords.verify_password(stale_hash, PASSWORD).needs_rehash is True
    assert login(client, email).status_code == 200

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        assert user.password_hash != stale_hash
        assert passwords.verify_password(user.password_hash, PASSWORD).needs_rehash is False
    finally:
        db.close()


def test_password_policy_bounds():
    validate_password("x" * 12, min_length=12, max_length=1024)
    with pytest.raises(PasswordPolicyError):
        validate_password("x" * 11, min_length=12, max_length=1024)
    with pytest.raises(PasswordPolicyError):
        validate_password("x" * 1025, min_length=12, max_length=1024)


def test_hashes_are_salted_per_password():
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)
    assert first != second
    assert verify_password(first, PASSWORD).ok
    assert verify_password(second, PASSWORD).ok
    assert not verify_password(first, PASSWORD + "!").ok
