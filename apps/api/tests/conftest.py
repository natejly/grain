from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import pytest
from fastapi.testclient import TestClient

from app.config import project_root  # noqa: E402

# Settings anchor relative sqlite/object paths to the repo root, so these must be
# anchored the same way. Resolving them against the cwd instead deletes a phantom
# apps/api/data copy while the app keeps using a stale repo-root database — the
# failure mode recorded in tasks/lessons.md.
TEST_DB = project_root() / "data" / "test_workspace.db"
TEST_OBJECTS = project_root() / "data" / "test_objects"
if TEST_DB.exists():
    TEST_DB.unlink()
if TEST_OBJECTS.exists():
    shutil.rmtree(TEST_OBJECTS)

os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///" + str(TEST_DB)
os.environ["OBJECTS_DIR"] = str(TEST_OBJECTS)
os.environ["TOOL_HOST_ALLOWLIST"] = "api.github.com"
# The suite runs on the only test double the app has. There is no no-key product
# mode, so a keyless run has to select `scripted` explicitly — Settings refuse to
# boot `openai` without OPENAI_API_KEY, and refuse `scripted` without a script.
os.environ["MODEL_PROVIDER"] = "scripted"
os.environ["SCRIPTED_MODEL_SCRIPT"] = str(
    Path(__file__).parent / "scripts" / "agent.json"
)
# DEV_AUTO_LOGIN stays off here on purpose. The suite authenticates the way a
# browser does — a real session row and its cookie — so these tests prove the
# production path is reachable *and* that a request without a cookie is refused.
os.environ["DEV_AUTO_LOGIN"] = "false"
# Same for DEV_USER: the override is covered by explicit monkeypatch tests, and
# leaving it on would make every anonymous client already signed in as Lyn.
os.environ["DEV_USER"] = ""
# And the third of the same family, pinned for the same reason the other two are
# — but this one was missing, and it is the expensive one to leave loose. A
# developer's own repo-root `.env` with `DEV_UNRESTRICTED_AGENT=1` is read by
# Settings, and it switches off both the approval parking and the per-subject
# tool narrowing. Forty-two tests then fail on this machine and pass in CI, all
# of them saying something plausible about scoping or the agent loop, and none
# of them about the one line that actually did it.
os.environ["DEV_UNRESTRICTED_AGENT"] = "false"

from app.auth import (  # noqa: E402
    DEV_SEED_USER_ID,
    DEV_SEED_WORKSPACE_ID,
    seed_dev_workspace,
)
from app.config import get_settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Agent, Membership, User, Workspace  # noqa: E402
from app.services.auth.ratelimit import auth_rate_limiter  # noqa: E402
from app.services.auth.sessions import create_session  # noqa: E402
from app.services.embeddings import query_embedding_cache  # noqa: E402

# https, not http: the session cookie is Secure (as it must be, since
# SameSite=None is only honoured with Secure), and http.cookiejar refuses to
# send a Secure cookie over a plaintext request. Speaking https to the ASGI app
# lets the suite exercise the real cookie attributes instead of a weakened set.
TEST_BASE_URL = "https://testserver"


@dataclass(frozen=True)
class Identity:
    """A logged-in user the tests can act as."""

    user_id: str
    workspace_id: str
    token: str
    csrf_token: str


@pytest.fixture(scope="session", autouse=True)
def _database_schema():
    """Create the tables once for the whole run.

    The app's lifespan also does this, but only tests that touch the `client`
    fixture start the app; auth tests that drive their own client would
    otherwise find an empty database.
    """
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_dev_workspace(db, get_settings())
    finally:
        db.close()
    yield


@pytest.fixture(autouse=True)
def _isolate_query_embedding_cache():
    """Query vectors are cached for the life of the process.

    That is safe in production — (text, model) -> vector does not change — but
    tests swap the embedder per test, so one test's fake vector would otherwise
    be served to the next test that happens to use the same query string.
    """
    query_embedding_cache.clear()
    yield
    query_embedding_cache.clear()


@pytest.fixture(autouse=True)
def _reset_auth_rate_limiter():
    """The limiter counts per process, so one test's logins would otherwise
    spend the next test's budget."""
    auth_rate_limiter.reset()
    yield
    auth_rate_limiter.reset()


def issue_session(
    user_id: str, *, user_agent: str = "pytest", ip_address: str = "127.0.0.1"
) -> tuple[str, str]:
    """Create a real session row for `user_id`; return (token, csrf token).

    This is the suite's way in, and it is deliberately not a shortcut past
    authentication: it writes the same row a login writes, and every request
    then travels the same cookie -> session -> membership path a browser does.
    Nothing in `app/` knows the caller is a test.
    """
    db = SessionLocal()
    try:
        issued = create_session(
            db,
            user_id=user_id,
            settings=get_settings(),
            user_agent=user_agent,
            ip_address=ip_address,
        )
        token, csrf_token = issued.token, issued.csrf_token
        db.commit()
        return token, csrf_token
    finally:
        db.close()


def authenticate(test_client: TestClient, identity: Identity) -> TestClient:
    """Attach an identity's cookie and CSRF header to a client."""
    settings = get_settings()
    # No explicit domain: http.cookiejar rewrites a dotless host to
    # "testserver.local", so a cookie pinned to "testserver" would be stored and
    # never sent back.
    test_client.cookies.set(settings.session_cookie_name, identity.token)
    test_client.headers[settings.csrf_header_name] = identity.csrf_token
    return test_client


def create_identity(
    *, name: str = "Test user", email: Optional[str] = None, workspace_name: str = "Test workspace"
) -> Identity:
    """A brand new user, workspace, owner membership, agent and session."""
    db = SessionLocal()
    try:
        user = User(email=email or f"{os.urandom(6).hex()}@example.com", name=name)
        db.add(user)
        db.flush()
        workspace = Workspace(name=workspace_name)
        db.add(workspace)
        db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        db.add(
            Agent(
                workspace_id=workspace.id,
                name="Research partner",
                instructions="Answer from workspace evidence.",
            )
        )
        db.commit()
        user_id, workspace_id = user.id, workspace.id
    finally:
        db.close()
    token, csrf_token = issue_session(user_id)
    return Identity(
        user_id=user_id, workspace_id=workspace_id, token=token, csrf_token=csrf_token
    )


@pytest.fixture(scope="session")
def client():
    """The suite's default caller: the seeded dev owner, holding a real session.

    The seed creates the rows; this fixture creates the session. There is no
    header the server trusts and no default identity to fall back on, so
    removing these two lines makes every request in the suite a 401 — which is
    the point.
    """
    with TestClient(app, base_url=TEST_BASE_URL) as test_client:
        token, csrf_token = issue_session(DEV_SEED_USER_ID)
        authenticate(
            test_client,
            Identity(
                user_id=DEV_SEED_USER_ID,
                workspace_id=DEV_SEED_WORKSPACE_ID,
                token=token,
                csrf_token=csrf_token,
            ),
        )
        yield test_client


@pytest.fixture
def anonymous_client():
    """A client with no session at all, for the fail-closed assertions."""
    return TestClient(app, base_url=TEST_BASE_URL)


@pytest.fixture
def identity_client() -> Callable[..., TestClient]:
    """Factory for a client acting as a *different* tenant.

    Each call creates a real user, workspace and session, which is how the
    isolation tests now prove scoping — they used to assert it by sending an
    X-User-Id header, which is exactly the thing that no longer exists.
    """

    def factory(**kwargs: str) -> TestClient:
        identity = create_identity(**kwargs)
        client = TestClient(app, base_url=TEST_BASE_URL)
        authenticate(client, identity)
        # Handy for tests that need to write rows for this tenant directly.
        client.identity = identity  # type: ignore[attr-defined]
        return client

    return factory


@pytest.fixture
def headers():
    return {"Idempotency-Key": "test-key-" + os.urandom(8).hex()}
