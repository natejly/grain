from __future__ import annotations

import json
import os
from pathlib import Path

# Importing `app.main` constructs Settings, and Settings refuses to boot the
# product provider without a key — so exporting a *schema* required an
# OPENAI_API_KEY it never calls. That is why the contract-drift check has been
# failing in CI: no key there, and no repo-root `.env` to supply one, so the
# script died on "MODEL_PROVIDER=openai requires OPENAI_API_KEY" before reaching
# the one line that matters.
#
# The route table is a static fact about the code. Point the settings at the test
# double so the app is importable, exactly as evaluate_retrieval.py does, and set
# the APP_ENV that makes `scripted` legal — `scripted` is gated on dev/test and
# `app_env` defaults to production, so the provider alone is not enough.
# `setdefault` throughout: a real configuration still wins, and the exported
# schema is identical either way.
os.environ.setdefault("MODEL_PROVIDER", "scripted")
os.environ.setdefault(
    "SCRIPTED_MODEL_SCRIPT",
    str(Path(__file__).resolve().parents[1] / "apps" / "api" / "tests" / "scripts" / "agent.json"),
)
os.environ.setdefault("APP_ENV", "development")

from app.main import app

target = Path("packages/api-client/openapi.json")
target.write_text(
    json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(target)

