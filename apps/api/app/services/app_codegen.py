from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..clock import utcnow
from ..config import Settings, get_settings
from ..schemas import DatasetQuery
from .analytics import AnalyticsValidationError, current_dataset_version, execute_dataset_query
from .model import generate_code
from .scripted_model import scripted_app_html
from .usage import usage_scope

MAX_HTML_BYTES = 256 * 1024
SNAPSHOT_ROW_LIMIT = 200
SAMPLE_ROWS = 5

# Injected into every generated app; the only channel in or out is postMessage.
#
# Naming: the runtime object is `window.grain` and the wire messages are
# `grain:*`. `window.jasmine` and `window.fieldnote` are kept as aliases to the
# *same object*, not copies — the model was told the older names for every app
# generated before each rename, and a body that still says
# `window.jasmine.onData = ...` (or `window.fieldnote....`) has to keep
# working. The same goes for the `--jasmine-*` CSS variables, which older
# bodies reference and which now resolve through the `--grain-*` tokens. The
# host speaks all three message namespaces for the same reason: a published
# release stores its own frozen copy of this runtime, so every snapshot cut
# before a rename posts `jasmine:ready` or `fieldnote:ready` and listens for
# the matching `:init` forever. See SandboxFrame for the host half of that
# compatibility.
GRAIN_RUNTIME = """<style>
:root {
  color-scheme: light;
  --grain-bg: #fffdf8;
  --grain-surface: #faf7f0;
  --grain-border: #e3dcce;
  --grain-text: #2e2a24;
  --grain-muted: #6b6357;
  --grain-accent: #2c7454;
  --grain-accent-soft: #6fbf9b;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --grain-bg: #0b0d10;
  --grain-surface: #12161c;
  --grain-border: #282f39;
  --grain-text: #e7eaf0;
  --grain-muted: #929ba8;
  --grain-accent: #7c9cff;
  --grain-accent-soft: #a8bcff;
}
:root {
  --jasmine-bg: var(--grain-bg);
  --jasmine-surface: var(--grain-surface);
  --jasmine-border: var(--grain-border);
  --jasmine-text: var(--grain-text);
  --jasmine-muted: var(--grain-muted);
  --jasmine-accent: var(--grain-accent);
  --jasmine-accent-soft: var(--grain-accent-soft);
}
html, body { background: var(--grain-bg); color: var(--grain-text); }
</style>
<script>
(function () {
  var pending = {};
  var counter = 0;
  var handler = null;
  var delivered = false;
  window.grain = {
    snapshots: {},
    theme: "light",
    query: function (dataset, query) {
      return new Promise(function (resolve, reject) {
        var requestId = "q" + (counter += 1);
        pending[requestId] = { resolve: resolve, reject: reject };
        parent.postMessage(
          { type: "grain:query", requestId: requestId, dataset: dataset, query: query || {} },
          "*"
        );
      });
    }
  };
  // onData may be assigned before or after the host delivers data, so the
  // setter replays whatever already arrived instead of dropping the render.
  Object.defineProperty(window.grain, "onData", {
    get: function () { return handler; },
    set: function (fn) {
      handler = fn;
      if (delivered && typeof fn === "function") fn(window.grain.snapshots);
    }
  });
  // Same object under the old names: assignments through any reference are
  // visible through the others, onData included.
  window.jasmine = window.grain;
  window.fieldnote = window.grain;
  window.addEventListener("message", function (event) {
    var msg = event.data || {};
    if (msg.type === "grain:init") {
      window.grain.snapshots = msg.snapshots || {};
      if (msg.theme === "dark" || msg.theme === "light") {
        window.grain.theme = msg.theme;
        document.documentElement.setAttribute("data-theme", msg.theme);
      }
      delivered = true;
      if (typeof handler === "function") handler(window.grain.snapshots);
    }
    if (msg.type === "grain:result" && pending[msg.requestId]) {
      var entry = pending[msg.requestId];
      delete pending[msg.requestId];
      if (msg.error) entry.reject(new Error(msg.error));
      else entry.resolve(msg.result);
    }
  });
  parent.postMessage({ type: "grain:ready" }, "*");
})();
</script>"""

# The old module-level names, for any in-tree importer that has not moved yet.
JASMINE_RUNTIME = GRAIN_RUNTIME
FIELDNOTE_RUNTIME = GRAIN_RUNTIME

CODEGEN_INSTRUCTIONS = """You generate a single self-contained HTML fragment for a data mini-app
that runs inside a locked-down sandboxed iframe.

Hard constraints — the sandbox enforces them, so violations just break the app:
- One HTML document body. Inline <style> and <script> only.
- NO external URLs of any kind: no CDN scripts, stylesheets, fonts, images, or fetch/XHR —
  the frame's CSP is default-src 'none' with connect-src 'none'.
- Data arrives ONLY through the provided runtime (already injected before your code):
  * `window.grain.snapshots` — object mapping dataset name to {columns, rows, row_count}.
  * `window.grain.onData = (snapshots) => …` — called when snapshots arrive; render there.
  * `await window.grain.query(name, {filters, group_by, metrics, order_by, limit})` —
    live typed queries (preview only; may reject when offline — fall back to snapshots).
  * `window.grain.theme` — "light" or "dark", set before onData fires.
- Do not emit <html>, <head>, <body>, <meta>, or another copy of the runtime.
- The app must work in BOTH themes, so do not hardcode colours. The runtime already
  sets the page background and text colour and exposes these variables — use them:
  --grain-bg, --grain-surface, --grain-border, --grain-text, --grain-muted,
  --grain-accent, --grain-accent-soft. For chart series and anything the variables
  do not cover, read window.grain.theme inside onData and pick a palette.

Return ONLY the HTML fragment, no markdown fences, no commentary."""


class AppCodegenError(ValueError):
    pass


def lint_generated_html(html: str) -> None:
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise AppCodegenError("Generated app exceeds the 256 KB limit")
    lowered = html.lower()
    if "<meta http-equiv" in lowered:
        raise AppCodegenError("Generated app must not override document policies")
    if re.search(r"(?:src|href)\s*=\s*[\"']\s*(?:https?:)?//", lowered):
        raise AppCodegenError("Generated app must not reference external URLs")


def build_code_manifest(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    app_name: str,
    prompt: str,
    dataset_ids: List[str],
    previous_html: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> Tuple[Dict[str, Any], str]:
    settings = settings or get_settings()

    bindings: List[Dict[str, str]] = []
    snapshots: Dict[str, Any] = {}
    schema_notes: List[str] = []
    for dataset_id in dict.fromkeys(dataset_ids):
        try:
            dataset, version = current_dataset_version(
                db, workspace_id=workspace_id, dataset_id=dataset_id
            )
            result = execute_dataset_query(
                db,
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                query=DatasetQuery(limit=SNAPSHOT_ROW_LIMIT),
            )
        except AnalyticsValidationError as exc:
            raise AppCodegenError(str(exc)) from exc
        bindings.append({"dataset_id": dataset.id, "name": dataset.name})
        snapshots[dataset.name] = result.model_dump(mode="json")
        columns = json.loads(version.schema_json)
        samples = result.rows[:SAMPLE_ROWS]
        schema_notes.append(
            f"Dataset “{dataset.name}” ({version.row_count} rows). Columns: "
            + json.dumps(columns)
            + ". Sample rows: "
            + json.dumps(samples, default=str)
        )

    if settings.active_model_provider == "scripted":
        # The test double is handed the bindings rather than the prompt: what a
        # generated app is judged on is the frame it renders, so it stands in
        # with a page that reads the same runtime real generated code must use.
        body = scripted_app_html(app_name, [binding["name"] for binding in bindings])
    else:
        parts = [f"App name: {app_name}", f"Request: {prompt}"]
        if schema_notes:
            parts.append("Available datasets (via window.grain):\n" + "\n".join(schema_notes))
        else:
            parts.append("No datasets are bound; build a static informational page.")
        if previous_html:
            parts.append(
                "Existing app to modify (return the full updated fragment):\n" + previous_html
            )
        with usage_scope(workspace_id=workspace_id, user_id=user_id):
            body = generate_code(
                CODEGEN_INSTRUCTIONS,
                "\n\n".join(parts),
                user_id=user_id,
                settings=settings,
            )

    lint_generated_html(body)
    html = GRAIN_RUNTIME + "\n" + body
    manifest = {
        "schema_version": 2,
        "kind": "code",
        "generated_at": utcnow().isoformat() + "Z",
        "prompt": prompt[:4000],
        "html": html,
        "data_bindings": bindings,
        "snapshots": snapshots,
    }
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return manifest, hashlib.sha256(canonical.encode()).hexdigest()
