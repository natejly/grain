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

MAX_HTML_BYTES = 256 * 1024
SNAPSHOT_ROW_LIMIT = 200
SAMPLE_ROWS = 5

# Injected into every generated app; the only channel in or out is postMessage.
#
# Naming: the runtime object is `window.jasmine` and the wire messages are
# `jasmine:*`. `window.fieldnote` is kept as an alias to the *same object*, not
# a copy — the model was told the old name for every app generated so far, and
# a body that still says `window.fieldnote.onData = ...` has to keep working.
# The host speaks both message namespaces for the same reason: a published
# release stores its own frozen copy of this runtime, so every snapshot cut
# before today posts `fieldnote:ready` and listens for `fieldnote:init`
# forever. See SandboxFrame for the host half of that compatibility.
JASMINE_RUNTIME = """<style>
:root {
  color-scheme: light;
  --jasmine-bg: #fffdf8;
  --jasmine-surface: #faf7f0;
  --jasmine-border: #e3dcce;
  --jasmine-text: #2e2a24;
  --jasmine-muted: #6b6357;
  --jasmine-accent: #2c7454;
  --jasmine-accent-soft: #6fbf9b;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --jasmine-bg: #0b0d10;
  --jasmine-surface: #12161c;
  --jasmine-border: #282f39;
  --jasmine-text: #e7eaf0;
  --jasmine-muted: #929ba8;
  --jasmine-accent: #7c9cff;
  --jasmine-accent-soft: #a8bcff;
}
html, body { background: var(--jasmine-bg); color: var(--jasmine-text); }
</style>
<script>
(function () {
  var pending = {};
  var counter = 0;
  var handler = null;
  var delivered = false;
  window.jasmine = {
    snapshots: {},
    theme: "light",
    query: function (dataset, query) {
      return new Promise(function (resolve, reject) {
        var requestId = "q" + (counter += 1);
        pending[requestId] = { resolve: resolve, reject: reject };
        parent.postMessage(
          { type: "jasmine:query", requestId: requestId, dataset: dataset, query: query || {} },
          "*"
        );
      });
    }
  };
  // onData may be assigned before or after the host delivers data, so the
  // setter replays whatever already arrived instead of dropping the render.
  Object.defineProperty(window.jasmine, "onData", {
    get: function () { return handler; },
    set: function (fn) {
      handler = fn;
      if (delivered && typeof fn === "function") fn(window.jasmine.snapshots);
    }
  });
  // Same object under the old name: assignments through either reference are
  // visible through the other, onData included.
  window.fieldnote = window.jasmine;
  window.addEventListener("message", function (event) {
    var msg = event.data || {};
    if (msg.type === "jasmine:init") {
      window.jasmine.snapshots = msg.snapshots || {};
      if (msg.theme === "dark" || msg.theme === "light") {
        window.jasmine.theme = msg.theme;
        document.documentElement.setAttribute("data-theme", msg.theme);
      }
      delivered = true;
      if (typeof handler === "function") handler(window.jasmine.snapshots);
    }
    if (msg.type === "jasmine:result" && pending[msg.requestId]) {
      var entry = pending[msg.requestId];
      delete pending[msg.requestId];
      if (msg.error) entry.reject(new Error(msg.error));
      else entry.resolve(msg.result);
    }
  });
  parent.postMessage({ type: "jasmine:ready" }, "*");
})();
</script>"""

# The old module-level name, for any in-tree importer that has not moved yet.
FIELDNOTE_RUNTIME = JASMINE_RUNTIME

CODEGEN_INSTRUCTIONS = """You generate a single self-contained HTML fragment for a data mini-app
that runs inside a locked-down sandboxed iframe.

Hard constraints — the sandbox enforces them, so violations just break the app:
- One HTML document body. Inline <style> and <script> only.
- NO external URLs of any kind: no CDN scripts, stylesheets, fonts, images, or fetch/XHR —
  the frame's CSP is default-src 'none' with connect-src 'none'.
- Data arrives ONLY through the provided runtime (already injected before your code):
  * `window.jasmine.snapshots` — object mapping dataset name to {columns, rows, row_count}.
  * `window.jasmine.onData = (snapshots) => …` — called when snapshots arrive; render there.
  * `await window.jasmine.query(name, {filters, group_by, metrics, order_by, limit})` —
    live typed queries (preview only; may reject when offline — fall back to snapshots).
  * `window.jasmine.theme` — "light" or "dark", set before onData fires.
- Do not emit <html>, <head>, <body>, <meta>, or another copy of the runtime.
- The app must work in BOTH themes, so do not hardcode colours. The runtime already
  sets the page background and text colour and exposes these variables — use them:
  --jasmine-bg, --jasmine-surface, --jasmine-border, --jasmine-text, --jasmine-muted,
  --jasmine-accent, --jasmine-accent-soft. For chart series and anything the variables
  do not cover, read window.jasmine.theme inside onData and pick a palette.

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
            parts.append("Available datasets (via window.jasmine):\n" + "\n".join(schema_notes))
        else:
            parts.append("No datasets are bound; build a static informational page.")
        if previous_html:
            parts.append(
                "Existing app to modify (return the full updated fragment):\n" + previous_html
            )
        body = generate_code(
            CODEGEN_INSTRUCTIONS,
            "\n\n".join(parts),
            user_id=user_id,
            settings=settings,
        )

    lint_generated_html(body)
    html = JASMINE_RUNTIME + "\n" + body
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
