"""The bridge between Grain skills and the open agent-skills SKILL.md format.

A SKILL.md file is YAML frontmatter (``---`` fences, at least ``name`` and
``description``) followed by a markdown body. Grain's Skill rows carry the same
facts under stricter bounds, so import/export is a pure text transform — no DB,
no HTTP, and deliberately **no YAML dependency**: the frontmatter grammar we
accept is the tiny "key: value" subset every published skill actually uses, and
hand-parsing it keeps the attack/complexity surface of a full YAML loader out
of a path that ingests third-party files. Anything richer (nested maps, lists,
block scalars) is ignored when it hangs off a key we don't care about, and is a
loud error when it tries to carry ``name``/``description``/``title`` — silently
flattening a multiline value into garbage would be worse than refusing.

The two directions are written as inverses and tested as a law:
``parse_skill_markdown(render_skill_markdown(**x)) == x`` for any content that
fits the Skill bounds. That law is what lets a skill leave Grain, live in a git
repo, and come back without drift.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Same slug rule as `schemas.SkillCreate.name` — restated here rather than
#: imported so this module stays importable without the pydantic schema tree
#: (and so a schema refactor cannot silently change what files we accept).
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: The Skill content bounds (`schemas.SkillCreate`), restated for the same
#: reason as `_NAME_RE`.
MAX_NAME_CHARS = 80
MAX_TITLE_CHARS = 160
MAX_DESCRIPTION_CHARS = 500
MAX_BODY_CHARS = 20000

#: Frontmatter keys this bridge understands. Everything else — `metadata`,
#: `license`, `allowed-tools`, vendor extensions — is ignored, continuation
#: lines included, so files from other ecosystems import cleanly.
_KNOWN_KEYS = frozenset({"name", "title", "description"})


@dataclass(frozen=True)
class ParsedSkill:
    """What a SKILL.md file says, already clipped/validated to Skill bounds.

    Frozen because it is a value, not a builder: the route hands it straight to
    `create_skill` and nothing should mutate it in between.
    """

    name: str
    title: str
    description: str
    body: str


def derived_title(name: str) -> str:
    """The title a skill gets when its file declares none: the slug's segments,
    capitalized. Shared by both directions so `render` can *omit* the `title`
    key exactly when `parse` would reconstruct it — that omission is what keeps
    exported files minimal without breaking the round-trip law."""
    return " ".join(seg.capitalize() for seg in name.split("-"))


def parse_skill_markdown(text: str) -> ParsedSkill:
    """SKILL.md text → a validated `ParsedSkill`.

    Every failure is a `ValueError` with a message meant for the person who
    uploaded the file, not for a stack trace — the route can pass it through as
    a 422 detail verbatim. Tolerances, in the spirit of "accept liberally":
    a UTF-8 BOM, CRLF/CR line endings, quoted or bare values, unknown keys
    (with their nested/multiline continuations), and a missing `title` or
    `description`. Non-negotiables: the file must *start* with the frontmatter
    fence, `name` must be a valid slug, and the body must exist and fit.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with a --- frontmatter block")
    rest = text[4:]

    front_lines, body = _split_frontmatter(rest)
    fields = _parse_frontmatter(front_lines)

    name = fields.get("name")
    if name is None:
        raise ValueError("frontmatter is missing 'name'")
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"name '{name}' is longer than {MAX_NAME_CHARS} characters")
    if not _NAME_RE.match(name):
        raise ValueError(f"name '{name}' must be a lowercase kebab-case slug")

    # Optional keys degrade instead of erroring: a missing description is the
    # empty string and a missing title is derived from the slug, because the
    # open standard only promises `name` + `description` and plenty of files in
    # the wild carry even less. Overlong values are clipped, not refused — the
    # skill is still usable and the clip is visible in the editor afterwards.
    description = fields.get("description", "")[:MAX_DESCRIPTION_CHARS]
    title = fields.get("title") or derived_title(name)
    title = title[:MAX_TITLE_CHARS]

    if not body.strip():
        raise ValueError("body is empty")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError(f"body is longer than {MAX_BODY_CHARS} characters")

    return ParsedSkill(name=name, title=title, description=description, body=body)


def render_skill_markdown(*, name: str, title: str, description: str, body: str) -> str:
    """A Grain skill's content → SKILL.md text.

    Validates its inputs against the same bounds `parse_skill_markdown`
    enforces, because the round-trip law is only worth stating for output that
    parses back: rendering an empty body or a newline-bearing title would
    produce a file this very module refuses (or worse, misreads), and that
    corruption should fail at export time, not at the next import.

    `title` is omitted when it equals the slug-derived default, so a skill
    authored without a custom title exports to the minimal two-key frontmatter
    the open standard shows in its examples.
    """
    if len(name) > MAX_NAME_CHARS or not _NAME_RE.match(name):
        raise ValueError(f"name '{name}' must be a lowercase kebab-case slug")
    if "\n" in title:
        raise ValueError("title must be a single line")
    if "\n" in description:
        raise ValueError("description must be a single line")
    if not title:
        raise ValueError("title is empty")
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"title is longer than {MAX_TITLE_CHARS} characters")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError(f"description is longer than {MAX_DESCRIPTION_CHARS} characters")
    if not body.strip():
        raise ValueError("body is empty")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError(f"body is longer than {MAX_BODY_CHARS} characters")

    lines = ["---", f"name: {name}", f"description: {_quote(description)}".rstrip()]
    if title != derived_title(name):
        lines.append(f"title: {_quote(title)}")
    lines.append("---")
    # The body is appended verbatim after the closing fence's newline — no
    # added blank line, no trailing-newline normalization. Any massaging here
    # would be a byte the parse side could not give back.
    return "\n".join(lines) + "\n" + body


def _split_frontmatter(rest: str) -> tuple[list[str], str]:
    """Split text-after-the-opening-fence into frontmatter lines and the body.

    Scans by character offset instead of joining split lines back together so
    the body is a verbatim slice of the input — ``"\\n".join(lines)`` would eat
    a trailing newline and quietly break the round-trip law.
    """
    front_lines: list[str] = []
    pos = 0
    while pos <= len(rest):
        nl = rest.find("\n", pos)
        line = rest[pos:] if nl == -1 else rest[pos:nl]
        if line.rstrip() == "---":
            return front_lines, "" if nl == -1 else rest[nl + 1 :]
        front_lines.append(line)
        if nl == -1:
            break
        pos = nl + 1
    raise ValueError("frontmatter block is never closed by a --- line")


def _parse_frontmatter(lines: list[str]) -> dict[str, str]:
    """The hand-rolled "key: value" subset of YAML this bridge accepts.

    A line starting with whitespace or ``-`` is a *continuation* — a nested
    map, a list item, or a block-scalar line. Continuations of unknown keys are
    skipped wholesale (that is how a `metadata:` block imports harmlessly);
    a continuation of a known key is an error, because the only honest readings
    of a multiline `name` are all wrong.
    """
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in (" ", "\t", "-"):
            if current_key in _KNOWN_KEYS:
                raise ValueError(
                    f"frontmatter key '{current_key}' must be a single-line value"
                )
            continue
        key, sep, raw_value = line.partition(":")
        if not sep:
            # A bare word at the top level is not a key we could ever act on;
            # treated like an unknown key rather than a fatal typo.
            current_key = None
            continue
        current_key = key.strip()
        if current_key in _KNOWN_KEYS:
            fields[current_key] = _unquote(raw_value.strip())
    return fields


def _unquote(value: str) -> str:
    """Undo the two YAML scalar quoting styles for single-line values.

    Double quotes unescape ``\\\\`` and ``\\"`` (the only escapes `_quote`
    emits) with a left-to-right scan — chained `str.replace` calls can pair a
    backslash with the wrong neighbor in runs like ``\\\\\\"``. Single quotes
    unescape the doubled ``''``. A value that merely *contains* a quote is left
    alone — only a matched wrap counts.
    """
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        inner = value[1:-1]
        out: list[str] = []
        i = 0
        while i < len(inner):
            if inner[i] == "\\" and i + 1 < len(inner) and inner[i + 1] in ('"', "\\"):
                out.append(inner[i + 1])
                i += 2
            else:
                out.append(inner[i])
                i += 1
        return "".join(out)
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _quote(value: str) -> str:
    """Wrap a frontmatter value in double quotes only when a bare rendering
    would parse back differently: a ``: `` inside it would read as a nested
    key, edge whitespace would be stripped, and a leading quote would be
    mistaken for a quoting style. Everything else stays bare, which is what
    keeps exported files looking hand-written."""
    needs_quotes = (
        ": " in value
        or value != value.strip()
        or value.startswith(('"', "'"))
    )
    if not needs_quotes:
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
