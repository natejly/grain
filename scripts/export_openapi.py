from __future__ import annotations

import json
from pathlib import Path

from app.main import app

target = Path("packages/api-client/openapi.json")
target.write_text(
    json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(target)

