"""The virtual filesystem behind multi-file code projects.

Files live in rows, not on disk — the browser bundles them with esbuild-wasm and
renders the result in a locked-down iframe, so nothing here ever executes. Paths
are therefore virtual, but they are still normalized and validated like real
ones: the bundler resolves relative imports against them, and a project's files
are the only thing it is allowed to reach.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Project, ProjectFile, utcnow
from ..artifacts.documents import DocumentError, apply_replacement, render_diff

MAX_FILE_BYTES = 256 * 1024
MAX_PROJECT_BYTES = 5 * 1024 * 1024
MAX_FILES_PER_PROJECT = 200
MAX_PATH_CHARS = 400
MAX_NAME_CHARS = 120


class ProjectError(ValueError):
    """A user- and model-facing problem with a project or file operation."""


def normalize_path(path: str) -> str:
    """Canonicalize a virtual path, or refuse it.

    `./a//b.ts` collapses to `a/b.ts`. Everything that could address something
    outside the project — absolute paths, `..`, backslashes (a Windows separator
    the bundler would treat as a literal character), control characters — is
    rejected rather than sanitized, because silently rewriting a path the model
    asked for produces a file it will never find again.
    """
    raw = (path or "").strip()
    if not raw:
        raise ProjectError("A file path is required")
    if len(raw) > MAX_PATH_CHARS:
        raise ProjectError(f"Path is longer than the {MAX_PATH_CHARS}-character limit")
    if "\\" in raw:
        raise ProjectError("Use forward slashes in paths, not backslashes")
    if any(character < " " or character == "\x7f" for character in raw):
        raise ProjectError("Path contains control characters")
    if raw.startswith("/"):
        raise ProjectError("Paths must be relative to the project root, not absolute")
    if raw.endswith("/"):
        raise ProjectError("Path ends in a separator, so it names a directory, not a file")

    segments: List[str] = []
    for segment in raw.split("/"):
        if segment == "..":
            raise ProjectError("Paths may not contain “..”; they cannot leave the project root")
        # Empty segments come from `//`, and `.` from `./` — both are noise the
        # bundler would resolve away anyway.
        if segment in ("", "."):
            continue
        if segment.strip() != segment:
            raise ProjectError(f"Path segment “{segment}” has leading or trailing whitespace")
        segments.append(segment)

    if not segments:
        raise ProjectError(f"“{raw}” does not name a file")
    clean = "/".join(segments)
    if len(clean) > MAX_PATH_CHARS:
        raise ProjectError(f"Path is longer than the {MAX_PATH_CHARS}-character limit")
    return clean


def byte_size(content: str) -> int:
    return len(content.encode("utf-8"))


# --------------------------------------------------------------------------
# Projects


def get_project(db: Session, *, workspace_id: str, project_id: str) -> Project:
    project = db.scalar(
        select(Project).where(
            Project.id == project_id, Project.workspace_id == workspace_id
        )
    )
    if project is None:
        raise ProjectError("No project with that id")
    return project


def find_by_name(db: Session, *, workspace_id: str, name: str) -> Optional[Project]:
    wanted = name.strip().lower()
    for project in db.scalars(select(Project).where(Project.workspace_id == workspace_id)):
        if project.name.strip().lower() == wanted:
            return project
    return None


def list_projects(db: Session, *, workspace_id: str) -> List[Project]:
    return list(
        db.scalars(
            select(Project)
            .where(Project.workspace_id == workspace_id)
            .order_by(Project.updated_at.desc())
        )
    )


def resolve(
    db: Session, *, workspace_id: str, project_id: str = "", name: str = ""
) -> Project:
    """Accept an id or a name, since models reliably remember only one."""
    if project_id:
        return get_project(db, workspace_id=workspace_id, project_id=project_id)
    if name:
        found = find_by_name(db, workspace_id=workspace_id, name=name)
        if found is None:
            raise ProjectError(f"No project named “{name}”")
        return found
    # A single project is the common case; don't make the model name it.
    projects = list_projects(db, workspace_id=workspace_id)
    if len(projects) == 1:
        return projects[0]
    raise ProjectError("Provide project_id or project name")


# React is imported explicitly so the starter compiles under esbuild's classic
# JSX transform as well as the automatic one.
STARTER_INDEX = '''import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

const mount =
  document.getElementById("root") ??
  document.body.appendChild(document.createElement("div"));

createRoot(mount).render(<App />);
'''

STARTER_APP = '''import React, { useState } from "react";

export function App() {
  const [count, setCount] = useState(0);
  return (
    <main
      style={{
        fontFamily: "system-ui, sans-serif",
        padding: "2rem",
        lineHeight: 1.5,
      }}
    >
      <h1 style={{ fontSize: "1.25rem", margin: 0 }}>New project</h1>
      <p>Edit App.tsx and the preview rebuilds.</p>
      <button onClick={() => setCount(count + 1)}>Clicked {count} times</button>
    </main>
  );
}
'''

# Only react/react-dom are provided by the in-browser bundler — there is no npm
# install in the frame, so the starter must not import any other bare specifier.
STARTER_FILES: Dict[str, str] = {
    "index.tsx": STARTER_INDEX,
    "App.tsx": STARTER_APP,
}


def create_project(
    db: Session,
    *,
    workspace_id: str,
    name: str,
    description: str = "",
    entry_path: str = "index.tsx",
    created_by: str = "",
    files: Optional[Dict[str, str]] = None,
    kind: str = "web",
) -> Project:
    """Create a project seeded with files that already render, so preview works.

    The kind decides the seed and the entry-path rule, and `latex.resolve_seed`
    owns both so neither this function nor its callers has to branch — imported
    locally because latex.py builds on this module.
    """
    from .latex import resolve_seed

    name = name.strip()
    if not name:
        raise ProjectError("A project needs a name")
    if len(name) > MAX_NAME_CHARS:
        raise ProjectError(f"Project name is longer than {MAX_NAME_CHARS} characters")
    if find_by_name(db, workspace_id=workspace_id, name=name) is not None:
        raise ProjectError(f"A project named “{name}” already exists")

    resolved_kind, entry, seeded = resolve_seed(
        kind=kind, name=name, entry_path=entry_path, files=files
    )
    normalized: Dict[str, str] = {}
    for path, content in seeded.items():
        clean = normalize_path(path)
        if clean in normalized:
            raise ProjectError(f"Duplicate file path “{clean}”")
        if byte_size(content) > MAX_FILE_BYTES:
            raise ProjectError(f"{clean} exceeds the {MAX_FILE_BYTES:,}-byte per-file limit")
        normalized[clean] = content
    if len(normalized) > MAX_FILES_PER_PROJECT:
        raise ProjectError(f"A project holds at most {MAX_FILES_PER_PROJECT} files")
    total = sum(byte_size(content) for content in normalized.values())
    if total > MAX_PROJECT_BYTES:
        raise ProjectError(f"Project exceeds the {MAX_PROJECT_BYTES:,}-byte limit")
    if normalized and entry not in normalized:
        raise ProjectError(f"The entry file “{entry}” is not among the project's files")

    project = Project(
        workspace_id=workspace_id,
        name=name,
        description=description[:2000],
        kind=resolved_kind,
        entry_path=entry,
        created_by=created_by,
    )
    db.add(project)
    db.flush()
    for path, content in normalized.items():
        db.add(
            ProjectFile(
                workspace_id=workspace_id,
                project_id=project.id,
                path=path,
                content=content,
            )
        )
    db.commit()
    return project


def delete_project(db: Session, *, workspace_id: str, project_id: str) -> str:
    project = get_project(db, workspace_id=workspace_id, project_id=project_id)
    for file in list_files(db, project_id=project.id):
        db.delete(file)
    name = project.name
    db.delete(project)
    db.commit()
    return name


# --------------------------------------------------------------------------
# Files


def list_files(db: Session, *, project_id: str) -> List[ProjectFile]:
    return list(
        db.scalars(
            select(ProjectFile)
            .where(ProjectFile.project_id == project_id)
            .order_by(ProjectFile.path.asc())
        )
    )


def find_file(db: Session, *, project_id: str, path: str) -> Optional[ProjectFile]:
    return db.scalar(
        select(ProjectFile).where(
            ProjectFile.project_id == project_id, ProjectFile.path == path
        )
    )


def read_file(db: Session, *, project: Project, path: str) -> ProjectFile:
    clean = normalize_path(path)
    file = find_file(db, project_id=project.id, path=clean)
    if file is None:
        raise ProjectError(f"“{clean}” is not in {project.name}")
    return file


def project_bytes(db: Session, *, project_id: str, excluding: str = "") -> int:
    return sum(
        byte_size(file.content)
        for file in list_files(db, project_id=project_id)
        if file.path != excluding
    )


def _check_write(
    db: Session, *, project: Project, path: str, content: str, replacing: bool
) -> None:
    """Refuse an oversized write outright — truncating would silently corrupt code."""
    size = byte_size(content)
    if size > MAX_FILE_BYTES:
        raise ProjectError(
            f"{path} is {size:,} bytes, over the {MAX_FILE_BYTES:,}-byte per-file limit"
        )
    if not replacing:
        count = len(list_files(db, project_id=project.id))
        if count >= MAX_FILES_PER_PROJECT:
            raise ProjectError(
                f"{project.name} already holds {count} files, the limit is "
                f"{MAX_FILES_PER_PROJECT}. Delete something first."
            )
    others = project_bytes(db, project_id=project.id, excluding=path)
    if others + size > MAX_PROJECT_BYTES:
        raise ProjectError(
            f"Writing {path} would put {project.name} at {others + size:,} bytes, "
            f"over the {MAX_PROJECT_BYTES:,}-byte project limit"
        )


def write_file(
    db: Session, *, workspace_id: str, project: Project, path: str, content: str
) -> Tuple[ProjectFile, bool]:
    """Create or overwrite a file. Returns the row and whether it is new."""
    clean = normalize_path(path)
    existing = find_file(db, project_id=project.id, path=clean)
    _check_write(db, project=project, path=clean, content=content, replacing=existing is not None)
    if existing is None:
        existing = ProjectFile(
            workspace_id=workspace_id,
            project_id=project.id,
            path=clean,
            content=content,
        )
        db.add(existing)
        created = True
    else:
        existing.content = content
        created = False
    project.updated_at = utcnow()
    db.commit()
    return existing, created


def preview_write(db: Session, *, project: Project, path: str, content: str) -> str:
    clean = normalize_path(path)
    existing = find_file(db, project_id=project.id, path=clean)
    before = existing.content if existing else ""
    return render_diff(before, content, title=f"{project.name}/{clean}")


def _replace(content: str, find: str, replace: str, *, replace_all: bool) -> Tuple[str, int]:
    """Document edits and file edits are the same operation; only the noun differs."""
    try:
        return apply_replacement(content, find, replace, replace_all=replace_all)
    except DocumentError as exc:
        raise ProjectError(str(exc).replace("the document", "the file")) from exc


def edit_file(
    db: Session,
    *,
    project: Project,
    path: str,
    find: str,
    replace: str,
    replace_all: bool = False,
) -> Tuple[ProjectFile, str, int]:
    """Exact-string replacement, with the same ambiguity rules as document edits."""
    file = read_file(db, project=project, path=path)
    updated, count = _replace(file.content, find, replace, replace_all=replace_all)
    _check_write(db, project=project, path=file.path, content=updated, replacing=True)
    diff = render_diff(file.content, updated, title=f"{project.name}/{file.path}")
    file.content = updated
    project.updated_at = utcnow()
    db.commit()
    return file, diff, count


def preview_edit(
    db: Session,
    *,
    project: Project,
    path: str,
    find: str,
    replace: str,
    replace_all: bool = False,
) -> str:
    file = read_file(db, project=project, path=path)
    updated, _count = _replace(file.content, find, replace, replace_all=replace_all)
    return render_diff(file.content, updated, title=f"{project.name}/{file.path}")


def delete_file(db: Session, *, project: Project, path: str) -> str:
    file = read_file(db, project=project, path=path)
    if file.path == project.entry_path:
        raise ProjectError(
            f"“{file.path}” is the entry file; point entry_path elsewhere before deleting it"
        )
    db.delete(file)
    project.updated_at = utcnow()
    db.commit()
    return file.path


def snapshot(db: Session, project: Project) -> Dict[str, object]:
    """Everything the bundler needs for one project, in one payload."""
    files = list_files(db, project_id=project.id)
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "kind": project.kind,
        "entry_path": project.entry_path,
        "files": [
            {"path": file.path, "content": file.content, "bytes": byte_size(file.content)}
            for file in files
        ],
        "total_bytes": sum(byte_size(file.content) for file in files),
    }
