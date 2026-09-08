"""Scratch harness: the API on port 8011 backed by the REAL model provider.

`serve_e2e.py` pins MODEL_PROVIDER=scripted, which is right for the browser
suite — it makes tool proposal and approval deterministic — but it means those
specs never see a sentence the model actually wrote. Response *quality* cannot
be measured against canned answers, so this variant boots the same seeded
workspace against the provider configured in the repo-root .env.

Deliberate differences from serve_e2e.py:
  - its own port (8011) and its own database/objects dirs, so it can run
    beside the scripted harness without either clobbering the other;
  - safe_mode OFF on the seeded memberships, so a turn that calls a tool runs
    to completion instead of parking on an approval nobody is watching;
  - DEV_UNRESTRICTED_AGENT still pinned off, so the approval *policy* is the
    product's own rather than a developer escape hatch.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import uvicorn
from sqlalchemy import select

ROOT_ENV = Path("/Users/natejly/Desktop/Dashbored/.env")
DATABASE = Path("./data/live_workspace.db")
OBJECTS = Path("./data/live_objects")
SANDBOXES = Path("./data/live_sandboxes")
API_DIR = Path(__file__).resolve().parent / "apps" / "api"

LIVE_EMAIL = "demo@example.com"
LIVE_PASSWORD = "e2e-demo-password"

for path, remover in ((DATABASE, "unlink"), (OBJECTS, "tree"), (SANDBOXES, "tree")):
    if path.exists():
        path.unlink() if remover == "unlink" else shutil.rmtree(path)

# The provider credentials live in the repo-root .env, which this worktree does
# not have a copy of. Read them across rather than duplicating a secret onto
# disk here; everything else is set explicitly below.
if ROOT_ENV.exists():
    for line in ROOT_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip().startswith(("OPENAI_", "ANTHROPIC_")):
            os.environ[name.strip()] = value.strip()

os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///./data/live_workspace.db"
os.environ["OBJECTS_DIR"] = "./data/live_objects"
os.environ["MODEL_PROVIDER"] = os.environ.get("MODEL_PROVIDER_OVERRIDE", "openai")
os.environ["TOOL_HOST_ALLOWLIST"] = "api.github.com"
os.environ["SANDBOX_ENABLED"] = "1"
os.environ["SANDBOX_PROVIDER"] = "subprocess"
os.environ["SANDBOX_WORKDIR"] = "./data/live_sandboxes"
os.environ["WEB_ORIGIN"] = "http://127.0.0.1:3011"
os.environ["DEV_AUTO_LOGIN"] = "false"
os.environ["DEV_UNRESTRICTED_AGENT"] = "false"

sys.path.insert(0, str(API_DIR))

from app.auth import DEV_SEED_USER_ID, seed_dev_workspace  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Membership, User  # noqa: E402
from app.services.auth.passwords import hash_password  # noqa: E402

Base.metadata.create_all(bind=engine)
_db = SessionLocal()
try:
    seed_dev_workspace(_db)
    _user = _db.get(User, DEV_SEED_USER_ID)
    if _user is not None:
        _user.password_hash = hash_password(LIVE_PASSWORD)
    # Agentic, not Safe: a quality probe wants the whole answer, and a turn
    # parked on an approval card produces no prose to judge.
    for _membership in _db.scalars(select(Membership)).all():
        _membership.safe_mode = False
    _db.commit()
finally:
    _db.close()

uvicorn.run("app.main:app", app_dir=str(API_DIR), host="127.0.0.1", port=8011)
