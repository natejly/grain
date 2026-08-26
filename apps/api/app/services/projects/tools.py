"""Agent tools for the project filesystem.

The write tools mirror the document tools: each carries a `preview` that renders
a unified diff, so the approval card shows the code change rather than a blob of
JSON. Nothing here executes anything — the files are bundled and run in the
browser, behind an iframe with no network.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from ..llm_tools import MAX_RESULT_CHARS, ToolContext, ToolResult, ToolSpec
from . import bibliography, latex, store

MAX_FILE_TOOL_CHARS = 12_000


def _text(args: Dict[str, Any], key: str, default: str = "") -> str:
    value = args.get(key, default)
    return value if isinstance(value, str) else default


def _seed_files(args: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Read the optional `files` argument, refusing anything that is not a list
    of `{path, content}` objects.

    Models routinely send a bare list of path strings here. Reaching into that
    with `.get` raises, which surfaces to the user as an opaque "tool failed"
    and renders the approval card blank, so the shape is checked instead.
    """
    files = args.get("files")
    if not isinstance(files, list) or not files:
        return None
    seeded: Dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            raise store.ProjectError(
                "Each entry in `files` must be an object with `path` and `content`, "
                f"not {type(item).__name__}"
            )
        seeded[str(item.get("path", ""))] = str(item.get("content", ""))
    return seeded


def _target(db: Session, context: ToolContext, args: Dict[str, Any]):
    """Which project a call acts on: what it named, else the one on screen.

    `context.project_id` is the same fallback the document tools take from
    `context.document_id`, and it exists for the same sentence: a turn started in
    the panel beside a project means *that* project when it says "this file",
    and a model that had to guess between forty of them would guess wrong. An
    explicit id or name still wins — the model asking for something else is the
    model being specific, not the model being confused.
    """
    project_id = _text(args, "project_id")
    name = _text(args, "project")
    # Only when the call named neither. `store.resolve` prefers an id over a
    # name, so folding the context into `project_id` unconditionally would make
    # every by-name call silently act on the open project instead.
    if not project_id and not name:
        project_id = context.project_id
    return store.resolve(
        db, workspace_id=context.workspace_id, project_id=project_id, name=name
    )


def _known_projects(db: Session, context: ToolContext) -> str:
    names = [
        project.name for project in store.list_projects(db, workspace_id=context.workspace_id)
    ]
    return f" Available projects: {', '.join(names)}." if names else ""


def _list_projects(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    rows = store.list_projects(db, workspace_id=context.workspace_id)
    if not rows:
        return ToolResult(content="No projects yet. Use create_project to start one.")
    return ToolResult(
        content=json.dumps(
            {
                "projects": [
                    {
                        "id": row.id,
                        "name": row.name,
                        "description": row.description,
                        "entry_path": row.entry_path,
                        "files": len(store.list_files(db, project_id=row.id)),
                    }
                    for row in rows
                ]
            }
        )
    )


def _fs_list(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        project = _target(db, context, args)
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}.{_known_projects(db, context)}")
    files = store.list_files(db, project_id=project.id)
    return ToolResult(
        content=json.dumps(
            {
                "project": project.name,
                "entry_path": project.entry_path,
                "files": [
                    {"path": file.path, "bytes": store.byte_size(file.content)}
                    for file in files
                ],
                "total_bytes": sum(store.byte_size(file.content) for file in files),
            }
        )
    )


def _fs_read(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        project = _target(db, context, args)
        file = store.read_file(db, project=project, path=_text(args, "path"))
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}")
    body = file.content
    truncated = len(body) > MAX_FILE_TOOL_CHARS
    if truncated:
        body = body[:MAX_FILE_TOOL_CHARS] + "\n…(truncated)"
    return ToolResult(
        content=json.dumps(
            {
                "project": project.name,
                "path": file.path,
                "content": body,
                "truncated": truncated,
            }
        )
    )


def _create_project(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        seeded = _seed_files(args)
        project = store.create_project(
            db,
            workspace_id=context.workspace_id,
            name=_text(args, "name"),
            description=_text(args, "description"),
            kind=_text(args, "kind", "web") or "web",
            # Empty means "use this kind's default" — index.tsx or main.tex.
            entry_path=_text(args, "entry_path"),
            created_by=context.user_id,
            files=seeded,
        )
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}")
    paths = [file.path for file in store.list_files(db, project_id=project.id)]
    return ToolResult(
        content=(
            f"Created project “{project.name}” (id {project.id}) with "
            f"{', '.join(paths)}. Entry point: {project.entry_path}."
        ),
        created_ids=[project.id],
    )


def _preview_create_project(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    try:
        seeded = _seed_files(args)
    except store.ProjectError as exc:
        return f"This will fail: {exc}"
    try:
        kind, entry, resolved = latex.resolve_seed(
            kind=_text(args, "kind", "web") or "web",
            name=_text(args, "name"),
            entry_path=_text(args, "entry_path"),
            files=seeded,
        )
    except store.ProjectError as exc:
        return f"This will fail: {exc}"
    paths = list(resolved)
    return (
        f"Create a {kind} project “{_text(args, 'name')}” with {len(paths)} file(s): "
        f"{', '.join(paths)} — entry point {entry}"
    )


def _fs_write(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        project = _target(db, context, args)
        file, created = store.write_file(
            db,
            workspace_id=context.workspace_id,
            project=project,
            path=_text(args, "path"),
            content=_text(args, "content"),
        )
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}")
    verb = "Created" if created else "Overwrote"
    return ToolResult(content=f"{verb} {project.name}/{file.path}.")


def _preview_fs_write(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    try:
        project = _target(db, context, args)
        return store.preview_write(
            db,
            project=project,
            path=_text(args, "path"),
            content=_text(args, "content"),
        )
    except store.ProjectError as exc:
        return f"This write will fail: {exc}"


def _fs_edit(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        project = _target(db, context, args)
        file, diff, count = store.edit_file(
            db,
            project=project,
            path=_text(args, "path"),
            find=_text(args, "find"),
            replace=_text(args, "replace"),
            replace_all=bool(args.get("replace_all")),
        )
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}")
    plural = "occurrence" if count == 1 else "occurrences"
    return ToolResult(
        content=f"Edited {project.name}/{file.path} ({count} {plural} replaced).\n\n{diff}"
    )


def _preview_fs_edit(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    try:
        project = _target(db, context, args)
        return store.preview_edit(
            db,
            project=project,
            path=_text(args, "path"),
            find=_text(args, "find"),
            replace=_text(args, "replace"),
            replace_all=bool(args.get("replace_all")),
        )
    except store.ProjectError as exc:
        return f"This edit will fail: {exc}"


def _fs_delete(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        project = _target(db, context, args)
        path = store.delete_file(db, project=project, path=_text(args, "path"))
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}")
    return ToolResult(content=f"Deleted {project.name}/{path}.")


def _preview_fs_delete(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    try:
        project = _target(db, context, args)
        file = store.read_file(db, project=project, path=_text(args, "path"))
    except store.ProjectError as exc:
        return f"This deletion will fail: {exc}"
    lines = len(file.content.splitlines())
    return f"Delete {project.name}/{file.path} ({lines} lines) — this cannot be undone"


# --------------------------------------------------------------------------
# Bibliographies


def _entry_fields(args: Dict[str, Any]) -> Dict[str, str]:
    """Read the `fields` argument as a name -> value mapping.

    An object is the documented shape, but models reliably also send a list of
    ``{name, value}`` pairs. Both are unambiguous, so both are accepted; anything
    else raises a ProjectError the caller turns into a message rather than an
    opaque tool failure with a blank approval card.
    """
    raw = args.get("fields")
    if isinstance(raw, dict):
        return {str(name): str(value) for name, value in raw.items()}
    if isinstance(raw, list):
        pairs: Dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise store.ProjectError(
                    "Each entry in `fields` must be an object with `name` and `value`, "
                    f"not {type(item).__name__}"
                )
            name = item.get("name") or item.get("field") or item.get("key") or ""
            pairs[str(name)] = str(item.get("value", ""))
        return pairs
    raise store.ProjectError(
        '`fields` must be an object of BibTeX fields, e.g. {"author": "Ada Lovelace", '
        '"title": "Notes", "year": "1843"}'
    )


def _bib_list(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        project = _target(db, context, args)
        report = bibliography.validate_project(db, project=project)
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}.{_known_projects(db, context)}")
    # Trimmed to the result budget here rather than clipped by the caller: half a
    # JSON document is not something the model can read at all.
    return ToolResult(content=json.dumps(report.as_dict(max_chars=MAX_RESULT_CHARS)))


def _bib_add(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        project = _target(db, context, args)
        _file, plan = bibliography.add_entry(
            db,
            workspace_id=context.workspace_id,
            project=project,
            entry_type=_text(args, "entry_type"),
            key=_text(args, "key"),
            fields=_entry_fields(args),
            path=_text(args, "path"),
        )
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}")
    verb = "Created" if plan.created else "Appended to"
    return ToolResult(
        content=(
            f"{verb} {project.name}/{plan.path}. Cite it with "
            f"\\cite{{{plan.key}}}.\n\n{plan.entry}"
        )
    )


def _preview_bib_add(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    try:
        project = _target(db, context, args)
        plan = bibliography.plan_add_entry(
            db,
            project=project,
            entry_type=_text(args, "entry_type"),
            key=_text(args, "key"),
            fields=_entry_fields(args),
            path=_text(args, "path"),
        )
    except store.ProjectError as exc:
        return f"This will fail: {exc}"
    return bibliography.describe_plan(project, plan)


_PROJECT_TARGET = {
    "project_id": {"type": "string", "description": "Project id (or use project)."},
    "project": {
        "type": "string",
        "description": "Project name, if no id. Omit both when there is only one project.",
    },
}


def registry_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    return {
        "list_projects": ToolSpec(
            name="list_projects",
            description="List the workspace's code projects with their entry points and files.",
            parameters={"type": "object", "properties": {}},
            executor=_list_projects,
        ),
        "fs_list": ToolSpec(
            name="fs_list",
            description="List the files in a project with their sizes.",
            parameters={"type": "object", "properties": dict(_PROJECT_TARGET)},
            executor=_fs_list,
        ),
        "fs_read": ToolSpec(
            name="fs_read",
            description="Read one file from a project. Paths are relative to the project root.",
            parameters={
                "type": "object",
                "properties": {**_PROJECT_TARGET, "path": {"type": "string"}},
                "required": ["path"],
            },
            executor=_fs_read,
        ),
        "create_project": ToolSpec(
            name="create_project",
            description=(
                "Create a code project. Omit `files` to get a runnable React starter "
                "that previews immediately. The project is bundled in the browser: "
                "TypeScript/TSX is supported, but the only importable packages are "
                "'react' and 'react-dom' — everything else must be a relative import "
                "of a file in the project. A 'latex' project compiles server-side "
                "with full TeX Live, so packages like tikz, beamer, biblatex, "
                "booktabs, and siunitx all work. Shell-escape is disabled, so "
                "minted is not available."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["web", "latex"],
                        "description": (
                            "'web' for a React/TypeScript app previewed in the "
                            "browser; 'latex' for a LaTeX document compiled to a "
                            "PDF. Default 'web'."
                        ),
                    },
                    "entry_path": {
                        "type": "string",
                        "description": (
                            "Entry file. Defaults to index.tsx for web and "
                            "main.tex for latex; a latex entry must end in .tex."
                        ),
                    },
                    "files": {
                        "type": "array",
                        "description": "Optional seed files; must include the entry file.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                    },
                },
                "required": ["name"],
            },
            executor=_create_project,
            read_only=False,
            preview=_preview_create_project,
        ),
        "fs_write": ToolSpec(
            name="fs_write",
            description=(
                "Create a file or replace its entire contents. Prefer fs_edit for "
                "small changes to an existing file. The user approves a diff first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **_PROJECT_TARGET,
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            executor=_fs_write,
            read_only=False,
            preview=_preview_fs_write,
        ),
        "fs_edit": ToolSpec(
            name="fs_edit",
            description=(
                "Replace an exact string in a project file. `find` must appear exactly "
                "once unless replace_all is true — include surrounding text to make it "
                "unique. The user sees a diff and approves before it applies."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **_PROJECT_TARGET,
                    "path": {"type": "string"},
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "find", "replace"],
            },
            executor=_fs_edit,
            read_only=False,
            preview=_preview_fs_edit,
        ),
        "fs_delete": ToolSpec(
            name="fs_delete",
            description="Delete a file from a project. The entry file cannot be deleted.",
            parameters={
                "type": "object",
                "properties": {**_PROJECT_TARGET, "path": {"type": "string"}},
                "required": ["path"],
            },
            executor=_fs_delete,
            read_only=False,
            preview=_preview_fs_delete,
        ),
        "bib_list": ToolSpec(
            name="bib_list",
            description=(
                "List a LaTeX project's bibliography and check it against the "
                "document. Reports citations with no entry (these break the "
                "compile), entries nothing cites, duplicate keys, and entries "
                "missing the fields their type requires. Read this before editing "
                "a .bib by hand."
            ),
            parameters={"type": "object", "properties": dict(_PROJECT_TARGET)},
            executor=_bib_list,
        ),
        "bib_add": ToolSpec(
            name="bib_add",
            description=(
                "Add one entry to a LaTeX project's bibliography. Give the BibTeX "
                "entry type (article, book, inproceedings, …), the citation key the "
                "document will \\cite, and the fields. A key already in the project "
                "is refused rather than duplicated. The user approves the exact "
                "BibTeX first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **_PROJECT_TARGET,
                    "entry_type": {
                        "type": "string",
                        "description": "BibTeX entry type, e.g. article, book, inproceedings.",
                    },
                    "key": {
                        "type": "string",
                        "description": "Citation key, e.g. knuth1984. No spaces or braces.",
                    },
                    "fields": {
                        "type": "object",
                        "description": (
                            "BibTeX fields as name/value pairs, e.g. "
                            '{"author": "Donald E. Knuth", "title": "The TeXbook", '
                            '"publisher": "Addison-Wesley", "year": "1984"}.'
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Which .bib to append to. Optional when the project has "
                            "exactly one."
                        ),
                    },
                },
                "required": ["entry_type", "key", "fields"],
            },
            executor=_bib_add,
            read_only=False,
            preview=_preview_bib_add,
        ),
    }
