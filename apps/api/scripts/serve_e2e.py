from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import uvicorn

DATABASE = Path("./data/e2e_workspace.db")
OBJECTS = Path("./data/e2e_objects")
API_DIR = Path(__file__).resolve().parents[1]

# The password the browser suite signs in with. It belongs to the seeded demo
# workspace and exists only in this development-only script; see below.
E2E_DEMO_EMAIL = "demo@example.com"
E2E_DEMO_PASSWORD = "e2e-demo-password"

if DATABASE.exists():
    DATABASE.unlink()
if OBJECTS.exists():
    shutil.rmtree(OBJECTS)

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
os.environ["WEB_ORIGIN"] = "http://127.0.0.1:3010"
# Off, deliberately. The web app has a sign-in screen now, so the browser suite
# authenticates the way a user does — cookie, CSRF header and all — and a signed
# out browser really is signed out, which is what makes the logout test mean
# something. Leaving this on would have handed every cookie-less request the
# seeded identity and quietly hidden the whole login flow from the suite.
os.environ["DEV_AUTO_LOGIN"] = "false"

sys.path.insert(0, str(API_DIR))

from app.auth import DEV_SEED_USER_ID, seed_dev_workspace  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import User  # noqa: E402
from app.services.auth.passwords import hash_password  # noqa: E402

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
finally:
    _db.close()

uvicorn.run("app.main:app", app_dir="apps/api", host="127.0.0.1", port=8010)
