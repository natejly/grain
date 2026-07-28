from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..schemas import DatasetQuery
from .analytics import AnalyticsValidationError, current_dataset_version, execute_dataset_query
from .model import generate_code

MAX_HTML_BYTES = 256 * 1024
SNAPSHOT_ROW_LIMIT = 200
SAMPLE_ROWS = 5

# Injected into every generated app; the only channel in or out is postMessage.
FIELDNOTE_RUNTIME = """<script>
(function () {
  var pending = {};
  var counter = 0;
  window.fieldnote = {
    snapshots: {},
    onData: null,
    query: function (dataset, query) {
      return new Promise(function (resolve, reject) {
        var requestId = "q" + (counter += 1);
        pending[requestId] = { resolve: resolve, reject: reject };
        parent.postMessage(
          { type: "fieldnote:query", requestId: requestId, dataset: dataset, query: query || {} },
          "*"
        );
      });
    }
  };
  window.addEventListener("message", function (event) {
    var msg = event.data || {};
    if (msg.type === "fieldnote:init") {
      window.fieldnote.snapshots = msg.snapshots || {};
      if (typeof window.fieldnote.onData === "function") {
        window.fieldnote.onData(window.fieldnote.snapshots);
      }
    }
    if (msg.type === "fieldnote:result" && pending[msg.requestId]) {
      var entry = pending[msg.requestId];
      delete pending[msg.requestId];
      if (msg.error) entry.reject(new Error(msg.error));
      else entry.resolve(msg.result);
    }
  });
  parent.postMessage({ type: "fieldnote:ready" }, "*");
})();
</script>"""

CODEGEN_INSTRUCTIONS = """You generate a single self-contained HTML fragment for a data mini-app
that runs inside a locked-down sandboxed iframe.

Hard constraints — the sandbox enforces them, so violations just break the app:
- One HTML document body. Inline <style> and <script> only.
- NO external URLs of any kind: no CDN scripts, stylesheets, fonts, images, or fetch/XHR —
  the frame's CSP is default-src 'none' with connect-src 'none'.
- Data arrives ONLY through the provided runtime (already injected before your code):
  * `window.fieldnote.snapshots` — object mapping dataset name to {columns, rows, row_count}.
  * `window.fieldnote.onData = (snapshots) => …` — called when snapshots arrive; render there.
  * `await window.fieldnote.query(name, {filters, group_by, metrics, order_by, limit})` —
    live typed queries (preview only; may reject when offline — fall back to snapshots).
- Do not emit <html>, <head>, <body>, <meta>, or another copy of the runtime.
- Style for a dark background (#0b0d10); keep everything readable and self-contained.

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


def _deterministic_html(app_name: str, binding_names: List[str]) -> str:
    names = json.dumps(binding_names)
    return f"""<style>
  body {{ margin: 0; padding: 24px; background: #0b0d10; color: #e7eaf0;
         font-family: ui-sans-serif, system-ui, sans-serif; }}
  h1 {{ font-size: 18px; margin: 0 0 16px; }}
  h2 {{ font-size: 13px; color: #929ba8; margin: 20px 0 8px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
  th, td {{ border: 1px solid #282f39; padding: 6px 9px; text-align: left; }}
  th {{ background: #12161c; }}
  p {{ color: #69727e; font-size: 12px; }}
</style>
<h1>{app_name}</h1>
<div id="root"><p>Waiting for data…</p></div>
<script>
  var BOUND = {names};
  window.fieldnote.onData = function (snapshots) {{
    var root = document.getElementById("root");
    root.innerHTML = "";
    BOUND.forEach(function (name) {{
      var snap = snapshots[name];
      var title = document.createElement("h2");
      title.textContent = name;
      root.appendChild(title);
      if (!snap || !snap.rows || !snap.rows.length) {{
        var empty = document.createElement("p");
        empty.textContent = "No rows.";
        root.appendChild(empty);
        return;
      }}
      var table = document.createElement("table");
      var head = table.insertRow();
      snap.columns.forEach(function (column) {{
        var cell = document.createElement("th");
        cell.textContent = column;
        head.appendChild(cell);
      }});
      snap.rows.slice(0, 50).forEach(function (row) {{
        var tr = table.insertRow();
        snap.columns.forEach(function (column) {{
          var td = tr.insertCell();
          td.textContent = row[column] == null ? "—" : String(row[column]);
        }});
      }});
      root.appendChild(table);
    }});
  }};
</script>"""


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

    if settings.active_model_provider == "openai":
        parts = [f"App name: {app_name}", f"Request: {prompt}"]
        if schema_notes:
            parts.append("Available datasets (via window.fieldnote):\n" + "\n".join(schema_notes))
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
    else:
        body = _deterministic_html(app_name, [binding["name"] for binding in bindings])

    lint_generated_html(body)
    html = FIELDNOTE_RUNTIME + "\n" + body
    manifest = {
        "schema_version": 2,
        "kind": "code",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "prompt": prompt[:4000],
        "html": html,
        "data_bindings": bindings,
        "snapshots": snapshots,
    }
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return manifest, hashlib.sha256(canonical.encode()).hexdigest()
