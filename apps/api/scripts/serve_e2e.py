from __future__ import annotations

import os
import shutil
from pathlib import Path

import uvicorn

DATABASE = Path("./data/e2e_workspace.db")
OBJECTS = Path("./data/e2e_objects")

if DATABASE.exists():
    DATABASE.unlink()
if OBJECTS.exists():
    shutil.rmtree(OBJECTS)

os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///./data/e2e_workspace.db"
os.environ["OBJECTS_DIR"] = "./data/e2e_objects"
os.environ["MODEL_PROVIDER"] = "deterministic"
os.environ["TOOL_HOST_ALLOWLIST"] = "api.github.com"
os.environ["WEB_ORIGIN"] = "http://127.0.0.1:3010"

uvicorn.run("app.main:app", app_dir="apps/api", host="127.0.0.1", port=8010)
