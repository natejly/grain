from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import uvicorn

DATABASE = Path("./data/e2e_workspace.db")
OBJECTS = Path("./data/e2e_objects")
SANDBOXES = Path("./data/e2e_sandboxes")
API_DIR = Path(__file__).resolve().parents[1]

# The password the browser suite signs in with. It belongs to the seeded demo
# workspace and exists only in this development-only script; see below.
E2E_DEMO_EMAIL = "demo@example.com"
E2E_DEMO_PASSWORD = "e2e-demo-password"

if DATABASE.exists():
    DATABASE.unlink()
if OBJECTS.exists():
    shutil.rmtree(OBJECTS)
if SANDBOXES.exists():
    shutil.rmtree(SANDBOXES)

os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///./data/e2e_workspace.db"
os.environ["OBJECTS_DIR"] = "./data/e2e_objects"
# The scripted provider runs the real agent loop against a canned script, so the
# browser suite can exercise tool proposal and approval with no network model and
# no API key. Prompts the script does not match are answered from the retrieved
# passages by the double itself.
os.environ["MODEL_PROVIDER"] = "scripted"
os.environ["SCRIPTED_MODEL_SCRIPT"] = "apps/web/e2e/agent-script.json"
os.environ["TOOL_HOST_ALLOWLIST"] = "api.github.com"
# Code execution on, on the local driver. The browser suite is the only place
# that can prove a figure the agent draws actually reaches a user's screen, and
# it cannot prove it against a sandbox that refuses to start. `subprocess` runs
# as this process's own user and is not a sandbox — Settings refuse it outside
# development/test for exactly that reason — but it harvests `chart.png` from
# the session directory through the same code path the container driver uses,
# which is the part under test here.
os.environ["SANDBOX_ENABLED"] = "1"
os.environ["SANDBOX_PROVIDER"] = "subprocess"
os.environ["SANDBOX_WORKDIR"] = "./data/e2e_sandboxes"
os.environ["WEB_ORIGIN"] = "http://127.0.0.1:3010"
# Off, deliberately. The web app has a sign-in screen now, so the browser suite
# authenticates the way a user does — cookie, CSRF header and all — and a signed
# out browser really is signed out, which is what makes the logout test mean
# something. Leaving this on would have handed every cookie-less request the
# seeded identity and quietly hidden the whole login flow from the suite.
os.environ["DEV_AUTO_LOGIN"] = "false"
# Off for the same reason, and pinned rather than assumed: Settings read the
# repo-root `.env`, and a developer who has `DEV_UNRESTRICTED_AGENT=1` there for
# their own dev server switches off approval parking for this harness too. Six
# specs then fail on their machine and pass in CI, every one of them reporting a
# missing approval card rather than the setting that removed it.
os.environ["DEV_UNRESTRICTED_AGENT"] = "false"

sys.path.insert(0, str(API_DIR))

from app.auth import DEV_SEED_USER_ID, seed_dev_workspace  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Agent, Conversation, Membership, User, Workspace  # noqa: E402
from app.services.auth.passwords import hash_password  # noqa: E402

# A second workspace for the same user, so the browser suite can prove the
# switcher actually switches. Its one conversation is the tell: a title that
# exists in exactly one of the two workspaces makes "the views refetched" an
# assertion rather than a hope. Created after the seed, so the demo workspace
# still holds the oldest membership and stays the default landing.
E2E_SECOND_WORKSPACE = "Field notes"
E2E_SECOND_CONVERSATION = "Radio silence"

# Give the seeded demo user a password so the suite can log in as it. The specs
# share one workspace on purpose — the github-zen tool grant, the agent, the
# sources they upload for each other — and a freshly signed-up tenant would have
# none of that. `seed_dev_workspace` refuses to run outside development/test, and
# this file is never imported by the app, so the credential cannot follow the
# code into a deployment.
Base.metadata.create_all(bind=engine)
_db = SessionLocal()
try:
    seed_dev_workspace(_db)
    _user = _db.get(User, DEV_SEED_USER_ID)
    if _user is not None:
        _user.password_hash = hash_password(E2E_DEMO_PASSWORD)
        _db.commit()

    _second = Workspace(name=E2E_SECOND_WORKSPACE)
    _db.add(_second)
    _db.flush()
    _db.add(
        Membership(workspace_id=_second.id, user_id=DEV_SEED_USER_ID, role="owner")
    )
    _db.add(
        Agent(
            workspace_id=_second.id,
            name="Research partner",
            instructions="Answer from workspace evidence.",
        )
    )
    _db.add(
        Conversation(
            workspace_id=_second.id,
            created_by=DEV_SEED_USER_ID,
            title=E2E_SECOND_CONVERSATION,
        )
    )
    _db.commit()
finally:
    _db.close()

uvicorn.run("app.main:app", app_dir="apps/api", host="127.0.0.1", port=8010)
