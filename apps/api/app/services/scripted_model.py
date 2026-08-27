"""A model that replays a JSON script instead of calling a provider.

Selected with MODEL_PROVIDER=scripted, which Settings refuses outside a
development/test app_env. It is the app's only test double: with no no-key
product mode left, this is what lets both suites drive the *real* agent loop —
proposal, policy resolution, parking on approval, resume, execution — with no
network model and no API key. Nothing downstream of the model changes: the
approval endpoint, its guards, and the tools all run as they do in production.

Script shape (a JSON list):

    [
      {
        "match": "draft the launch runbook",
        "steps": [
          {"call": {"name": "create_document", "arguments": {"title": "…"}}},
          {"text": "Drafted it.", "denied_text": "I left it alone."}
        ],
        "memories": [{"kind": "fact", "content": "…", "entities": ["…"]}]
      }
    ]

`match` is a case-insensitive substring of the user's prompt; the first entry
carrying the facet being asked for wins, so order overlapping entries
most-specific first. An entry needs at least one facet: `steps` scripts the chat
turn, `memories` scripts what the model extracts from the exchange afterwards.

Each step is one model turn: a `call` proposes a single tool call, anything else
speaks. `denied_text` replaces `text` when the preceding tool call came back
denied, which is what a real model would react to.

Prompts no entry covers still have to produce *something*, because the double
also backs turns a test never scripted. Those fall through to the built-ins
below: an answer that quotes the retrieved passages, and a table renderer for
generated apps. They stand in for a model, they are not a product mode — nothing
here is reachable outside a development/test app_env.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ..config import Settings
from .errors import UserFacingError
from .model import stream_words
from .retrieval import Evidence


@dataclass(frozen=True)
class ScriptedCall:
    name: str
    arguments: str


@dataclass(frozen=True)
class ScriptedStep:
    text: str = ""
    denied_text: str = ""
    call: Optional[ScriptedCall] = None


@dataclass(frozen=True)
class ScriptedEntry:
    match: str
    steps: List[ScriptedStep] = field(default_factory=list)
    memories: List[Dict[str, object]] = field(default_factory=list)


@dataclass
class _FunctionCallItem:
    """Shaped like the Responses API's function_call output item."""

    call_id: str
    name: str
    arguments: str
    type: str = "function_call"


@dataclass
class _ScriptedResponse:
    output: List[Any] = field(default_factory=list)
    output_text: str = ""


class ScriptError(UserFacingError, RuntimeError):
    pass


_CACHE: Dict[Tuple[str, float], List[ScriptedEntry]] = {}


def load_script(path: Path) -> List[ScriptedEntry]:
    """Parse the script, memoised on (path, mtime) so edits are picked up."""
    key = (str(path), path.stat().st_mtime)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ScriptError("A model script must be a JSON list of entries")
    entries = [_entry(item, index) for index, item in enumerate(raw)]
    _CACHE.clear()
    _CACHE[key] = entries
    return entries


def _entry(item: Any, index: int) -> ScriptedEntry:
    if not isinstance(item, dict):
        raise ScriptError(f"Script entry {index} is not an object")
    match = str(item.get("match") or "").strip().lower()
    if not match:
        raise ScriptError(f"Script entry {index} has no match")
    raw_steps = item.get("steps")
    if raw_steps is not None and (not isinstance(raw_steps, list) or not raw_steps):
        raise ScriptError(f"Script entry “{match}” has no steps")
    raw_memories = item.get("memories")
    if raw_memories is not None and not isinstance(raw_memories, list):
        raise ScriptError(f"Script entry “{match}” has a non-list memories")
    if raw_steps is None and raw_memories is None:
        raise ScriptError(f"Script entry “{match}” scripts nothing")
    return ScriptedEntry(
        match=match,
        steps=[_step(row, match) for row in raw_steps or []],
        memories=[row for row in raw_memories or [] if isinstance(row, dict)],
    )


def _step(row: Any, match: str) -> ScriptedStep:
    if not isinstance(row, dict):
        raise ScriptError(f"Script entry “{match}” has a step that is not an object")
    call = row.get("call")
    if call is None:
        return ScriptedStep(
            text=str(row.get("text") or ""),
            denied_text=str(row.get("denied_text") or ""),
        )
    if not isinstance(call, dict) or not call.get("name"):
        raise ScriptError(f"Script entry “{match}” has a call without a name")
    return ScriptedStep(
        call=ScriptedCall(
            name=str(call["name"]),
            arguments=json.dumps(call.get("arguments") or {}),
        )
    )


def _item_type(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("type") or "")
    return str(getattr(item, "type", "") or "")


def _last_call_was_denied(input_items: List[Any]) -> bool:
    # Deferred: agent_loop imports this module, so the constant can only be
    # reached once both modules are loaded.
    from .agent_loop import DENIAL_OUTPUT

    for item in reversed(input_items):
        if _item_type(item) != "function_call_output":
            continue
        output = item.get("output") if isinstance(item, dict) else None
        return str(output or "") == DENIAL_OUTPUT
    return False


def _speak(text: str) -> Iterable[Tuple[str, Any]]:
    for piece in stream_words(text):
        yield "delta", piece
    yield "completed", _ScriptedResponse(output_text=text)


def _entries(settings: Settings) -> List[ScriptedEntry]:
    if settings.scripted_model_script is None:
        raise ScriptError("MODEL_PROVIDER=scripted requires SCRIPTED_MODEL_SCRIPT")
    return load_script(settings.scripted_model_script)


def _matching(
    settings: Settings, text: str, facet: Callable[[ScriptedEntry], bool]
) -> Optional[ScriptedEntry]:
    """First entry matching `text` that scripts the facet being asked for.

    Facet-aware rather than first-match-wins: one prompt can be scripted for its
    chat turn, for the memories the exchange yields, or for both, and an entry
    that only carries the other facet must not shadow the one that carries this.
    """
    needle = text.lower()
    return next(
        (
            candidate
            for candidate in _entries(settings)
            if candidate.match in needle and facet(candidate)
        ),
        None,
    )


def unscripted_answer(evidence: List[Evidence]) -> str:
    """What the double says about a prompt no entry covers.

    Quoting the retrieved passages keeps an unscripted turn useful to the tests
    and the browser suite that assert on citations, without any entry having to
    anticipate every prompt a test types.
    """
    findings = []
    for index, item in enumerate(evidence[:3], start=1):
        excerpt = item.excerpt.strip()
        if len(excerpt) > 420:
            excerpt = excerpt[:417].rsplit(" ", 1)[0] + "…"
        findings.append("- " + excerpt + " [" + str(index) + "]")
    if not findings:
        return "No script matches this prompt and no indexed passage answers it."
    return "\n".join(findings)


def scripted_memories(settings: Settings, prompt: str) -> List[Dict[str, object]]:
    """Stand in for model.extract_memories. Nothing scripted means nothing durable."""
    entry = _matching(settings, prompt, lambda candidate: bool(candidate.memories))
    return list(entry.memories) if entry is not None else []


def scripted_app_html(app_name: str, binding_names: List[str]) -> str:
    """Stand in for model.generate_code: a plain table over the bound datasets.

    Not scripted per prompt. Generated apps are judged by what renders in the
    frame, so a fixed page that actually reads `window.grain` is a more
    useful double than a canned string — it exercises the runtime the real
    generated code has to use, including its theme variables.
    """
    names = json.dumps(binding_names)
    return f"""<style>
  body {{ margin: 0; padding: 24px; background: var(--grain-bg);
         color: var(--grain-text);
         font-family: ui-sans-serif, system-ui, sans-serif; }}
  h1 {{ font-size: 18px; margin: 0 0 16px; }}
  h2 {{ font-size: 13px; color: var(--grain-muted); margin: 20px 0 8px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
  th, td {{ border: 1px solid var(--grain-border); padding: 6px 9px; text-align: left; }}
  th {{ background: var(--grain-surface); }}
  p {{ color: var(--grain-muted); font-size: 12px; }}
</style>
<h1>{app_name}</h1>
<div id="root"><p>Waiting for data…</p></div>
<script>
  var BOUND = {names};
  window.grain.onData = function (snapshots) {{
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


def scripted_workflow_graph(input_text: str) -> str:
    """Stand in for the workflow compiler's model call.

    Emits a two-node DAG — search the workspace's sources, then summarise them —
    that is deliberately *valid*: `search_sources` is in every workspace's
    registry, the reference points upstream, and the trigger is manual. So a test
    that exercises the compile path end to end without injecting a step gets a
    real pass through the validator rather than a stub that skips it.

    Not scripted per prompt. A script entry can express a bad graph when a test
    wants one; the default here is the good one.
    """
    query = input_text.strip().splitlines()[1] if "\n" in input_text else "workspace"
    return json.dumps(
        {
            "name": "Scripted workflow",
            "description": "Search the workspace, then summarise what came back.",
            "trigger": {"kind": "manual", "cron": "", "timezone": "UTC"},
            "nodes": [
                {
                    "id": "gather",
                    "kind": "tool",
                    "tool": "search_sources",
                    "arguments": {"query": query[:200]},
                    "description": "Find the relevant passages.",
                },
                {
                    "id": "summarise",
                    "kind": "agent",
                    "prompt": "Summarise these passages: {{ gather.output }}",
                    "description": "Write the summary.",
                },
            ],
            "edges": [{"from": "gather", "to": "summarise"}],
        }
    )


_SCHEDULE_TIME_RE = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b")
_SCHEDULE_INTERVAL_RE = re.compile(r"\bevery\s+(\d+)\s+(minute|hour)s?\b")
_SCHEDULE_ORDINAL_RE = re.compile(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b")
_SCHEDULE_CUE_RE = re.compile(
    r"\b(every|each|daily|hourly|nightly|weekly|monthly|weekdays?|weekends?)\b"
)
#: Cron's Sunday=0 convention, same as `validate._CRON_NAMES`.
_SCHEDULE_WEEKDAYS = {
    "sunday": 0,
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
}


def scripted_schedule_json(input_text: str) -> str:
    """Stand in for the schedule compiler's model call.

    Not a canned answer, unlike `scripted_workflow_graph`: the composer's whole
    surface is "type any sentence", so a single fixed reply would make every
    offline test assert the same string. Instead: a small deterministic parser
    for the shapes people actually type — intervals, dailies, weekday lists,
    month ordinals. A sentence outside those shapes answers `{"cron": ""}`,
    which the route turns into an honest 422 rather than a fake success.

    Times are translated, never range-checked — "at 99:00" becomes an hour of
    99 on purpose, so the offline path still proves the *validator* is what
    holds a model's answer to the hand-typed standard.
    """
    body, _, tail = input_text.partition("Default timezone:")
    text = body.replace("Schedule to express:", " ").strip().lower()
    tail = tail.strip()
    zone = tail.splitlines()[0].strip() if tail else "UTC"

    interval = _SCHEDULE_INTERVAL_RE.search(text)
    if interval:
        value = int(interval.group(1))
        if interval.group(2) == "minute":
            return json.dumps({"cron": f"*/{value} * * * *", "timezone": zone})
        return json.dumps({"cron": f"0 */{value} * * *", "timezone": zone})
    if re.search(r"\bevery minute\b", text):
        return json.dumps({"cron": "* * * * *", "timezone": zone})
    if re.search(r"\bhourly\b|\bevery hour\b", text):
        return json.dumps({"cron": "0 * * * *", "timezone": zone})

    hour: Optional[int] = None
    minute = 0
    clock = _SCHEDULE_TIME_RE.search(text)
    if clock:
        hour = int(clock.group(1))
        minute = int(clock.group(2) or 0)
        meridiem = clock.group(3)
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    elif "noon" in text:
        hour = 12
    elif "midnight" in text:
        hour = 0
    elif "morning" in text:
        hour = 9
    elif "afternoon" in text:
        hour = 15
    elif "evening" in text:
        hour = 18
    elif "night" in text:
        hour = 22

    dow = "*"
    named = sorted(
        {
            value
            for name, value in _SCHEDULE_WEEKDAYS.items()
            if re.search(rf"\b{name}s?\b", text)
        }
    )
    if re.search(r"\bweek\s?days?\b", text):
        dow = "1-5"
    elif re.search(r"\bweekends?\b", text):
        dow = "0,6"
    elif named:
        dow = ",".join(str(value) for value in named)
    elif re.search(r"\bweekly\b|\bevery week\b", text):
        dow = "1"

    dom = "*"
    ordinal = _SCHEDULE_ORDINAL_RE.search(text)
    if ordinal:
        dom = ordinal.group(1)
    elif re.search(r"\bfirst of (?:the|every) month\b", text):
        dom = "1"
    elif re.search(r"\bmonthly\b|\bevery month\b", text):
        dom = "1"

    if dow == "*" and dom == "*" and not _SCHEDULE_CUE_RE.search(text):
        # No recurrence in the sentence at all. "" is the parser saying so; the
        # compiler turns it into the 422 the real model would earn for prose.
        return json.dumps({"cron": "", "timezone": ""})
    if hour is None:
        # "every monday", "monthly on the 1st": mornings are the default reading.
        hour = 9
    return json.dumps({"cron": f"{minute} {hour} {dom} * {dow}", "timezone": zone})


def scripted_model_step(
    settings: Settings, *, prompt: str, evidence: List[Evidence]
) -> Callable[[List[Any], List[Dict[str, Any]], str], Iterable[Tuple[str, Any]]]:
    """Build the agent loop's ModelStep for one turn of `prompt`.

    Spelled out rather than annotated `ModelStep`: agent_loop imports this
    module, so it cannot be imported back for the alias.
    """
    entry = _matching(settings, prompt, lambda candidate: bool(candidate.steps))

    def step(
        input_items: List[Any], tools: List[Dict[str, Any]], instructions: str
    ) -> Iterable[Tuple[str, Any]]:
        if entry is None:
            return _speak(unscripted_answer(evidence))
        # Each scripted step emits at most one call, so the calls already in the
        # transcript say how far the script has run — which survives the process
        # hop between parking on an approval and resuming.
        position = sum(
            1 for item in input_items if _item_type(item) == "function_call"
        )
        if position >= len(entry.steps):
            raise ScriptError(f"Script entry “{entry.match}” ran out of steps")
        current = entry.steps[position]
        if current.call is not None:
            return [
                (
                    "completed",
                    _ScriptedResponse(
                        output=[
                            _FunctionCallItem(
                                call_id=f"scripted-{position}",
                                name=current.call.name,
                                arguments=current.call.arguments,
                            )
                        ]
                    ),
                )
            ]
        denied = bool(current.denied_text) and _last_call_was_denied(input_items)
        return _speak(current.denied_text if denied else current.text)

    return step
