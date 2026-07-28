from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_DB = Path("./data/test_workspace.db")
TEST_OBJECTS = Path("./data/test_objects")
if TEST_DB.exists():
    TEST_DB.unlink()
if TEST_OBJECTS.exists():
    shutil.rmtree(TEST_OBJECTS)

os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///./data/test_workspace.db"
os.environ["OBJECTS_DIR"] = "./data/test_objects"
os.environ["TOOL_HOST_ALLOWLIST"] = "api.github.com"
os.environ["MODEL_PROVIDER"] = "deterministic"

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def headers():
    return {"Idempotency-Key": "test-key-" + os.urandom(8).hex()}
