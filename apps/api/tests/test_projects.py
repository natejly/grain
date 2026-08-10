from __future__ import annotations

import json

import pytest

from app.database import SessionLocal
from app.models import Project, ProjectFile
from app.services.llm_tools import ToolContext
from app.services.projects import store
from app.services.projects.tools import registry_tools


@pytest.fixture
def workspace(client):
    identity = client.get("/api/bootstrap").json()["identity"]
    yield identity
    db = SessionLocal()
    try:
        db.query(ProjectFile).delete()
        db.query(Project).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def db(workspace):
    """Ordered after `workspace` so this session closes before the cleanup runs."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _context(identity) -> ToolContext:
    return ToolContext(
        workspace_id=identity["workspace_id"],
        user_id=identity["user_id"],
        conversation_id="none",
    )


def _project(db, workspace, name="Demo", **kwargs):
    return store.create_project(
        db, workspace_id=workspace["workspace_id"], name=name, **kwargs
    )


# --------------------------------------------------------------------------
# Path safety


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/index.tsx",
        "../secrets.ts",
        "..",
        "src/../../outside.ts",
        "a/../b.ts",
        "src\\windows.ts",
        "..\\..\\escape.ts",
        "",
        "   ",
        "src/",
        ".",
        "./",
        "//",
        "a/ b.ts",
        "null\x00byte.ts",
        "new\nline.ts",
        "bell\x07.ts",
        "x" * 401,
        "deep/" * 90 + "f.ts",
    ],
)
def test_unsafe_paths_are_rejected(path):
    with pytest.raises(store.ProjectError):
        store.normalize_path(path)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("./a//b.ts", "a/b.ts"),
        ("index.tsx", "index.tsx"),
        ("  src/App.tsx  ", "src/App.tsx"),
        ("src/./components/Chart.tsx", "src/components/Chart.tsx"),
        ("a///b////c.css", "a/b/c.css"),
    ],
)
def test_safe_paths_are_normalized(raw, expected):
    assert store.normalize_path(raw) == expected


def test_traversal_is_rejected_at_the_write_boundary_not_just_the_parser(db, workspace):
    """The store must refuse a bad path even when it arrives through a write."""
    project = _project(db, workspace)
    with pytest.raises(store.ProjectError, match="leave the project root"):
        store.write_file(
            db,
            workspace_id=workspace["workspace_id"],
            project=project,
            path="../../etc/passwd",
            content="nope",
        )
    assert [f.path for f in store.list_files(db, project_id=project.id)] == [
        "App.tsx",
        "index.tsx",
    ]


# --------------------------------------------------------------------------
# Limits


def test_per_file_limit_is_an_error_not_a_truncation(db, workspace):
    project = _project(db, workspace)
    oversized = "x" * (store.MAX_FILE_BYTES + 1)
    with pytest.raises(store.ProjectError, match="per-file limit"):
        store.write_file(
            db,
            workspace_id=workspace["workspace_id"],
            project=project,
            path="big.ts",
            content=oversized,
        )
    assert store.find_file(db, project_id=project.id, path="big.ts") is None


def test_multibyte_content_is_measured_in_bytes_not_characters(db, workspace):
    project = _project(db, workspace)
    # Well under the limit in characters, over it in UTF-8 bytes.
    content = "é" * (store.MAX_FILE_BYTES // 2 + 1)
    assert len(content) < store.MAX_FILE_BYTES
    with pytest.raises(store.ProjectError, match="per-file limit"):
        store.write_file(
            db,
            workspace_id=workspace["workspace_id"],
            project=project,
            path="accents.ts",
            content=content,
        )


def test_project_byte_limit_is_enforced(db, workspace):
    project = _project(db, workspace, name="Heavy", files={"index.tsx": "x"})
    filler = "y" * store.MAX_FILE_BYTES
    remaining = store.MAX_PROJECT_BYTES - store.project_bytes(db, project_id=project.id)
    for index in range(remaining // store.MAX_FILE_BYTES):
        db.add(
            ProjectFile(
                workspace_id=workspace["workspace_id"],
                project_id=project.id,
                path=f"filler{index}.ts",
                content=filler,
            )
        )
    db.commit()
    tail = store.MAX_PROJECT_BYTES - store.project_bytes(db, project_id=project.id)
    # Fill it to exactly the cap: that is allowed.
    store.write_file(
        db,
        workspace_id=workspace["workspace_id"],
        project=project,
        path="tail.ts",
        content="z" * tail,
    )
    assert store.project_bytes(db, project_id=project.id) == store.MAX_PROJECT_BYTES
    with pytest.raises(store.ProjectError, match="project limit"):
        store.write_file(
            db,
            workspace_id=workspace["workspace_id"],
            project=project,
            path="one-more.ts",
            content="!",
        )


def test_file_count_limit_is_enforced_but_overwrites_still_work(db, workspace):
    project = _project(db, workspace, name="Crowded", files={"index.tsx": "x"})
    for index in range(store.MAX_FILES_PER_PROJECT - 1):
        db.add(
            ProjectFile(
                workspace_id=workspace["workspace_id"],
                project_id=project.id,
                path=f"f{index}.ts",
                content="//",
            )
        )
    db.commit()
    with pytest.raises(store.ProjectError, match="the limit is"):
        store.write_file(
            db,
            workspace_id=workspace["workspace_id"],
            project=project,
            path="overflow.ts",
            content="//",
        )
    # A full project can still be edited — the cap is on new files.
    file, created = store.write_file(
        db,
        workspace_id=workspace["workspace_id"],
        project=project,
        path="f0.ts",
        content="// changed",
    )
    assert created is False and file.content == "// changed"


# --------------------------------------------------------------------------
# Projects and the starter


def test_new_project_seeds_a_runnable_starter(db, workspace):
    project = _project(db, workspace, name="Starter")
    files = {file.path: file.content for file in store.list_files(db, project_id=project.id)}
    assert project.entry_path == "index.tsx"
    assert "index.tsx" in files
    # Everything index.tsx imports must be react/react-dom or a file in the project;
    # there is no npm install inside the sandbox frame.
    assert 'from "./App"' in files["index.tsx"]
    assert "App.tsx" in files
    for content in files.values():
        for line in content.splitlines():
            if line.startswith("import ") and " from " in line:
                specifier = line.split(" from ")[1].strip().strip('";')
                assert specifier.startswith(".") or specifier in {
                    "react",
                    "react-dom",
                    "react-dom/client",
                }, specifier


def test_duplicate_project_names_are_refused(db, workspace):
    _project(db, workspace, name="Unique")
    with pytest.raises(store.ProjectError, match="already exists"):
        _project(db, workspace, name="unique")


def test_entry_file_must_exist_and_cannot_be_deleted(db, workspace):
    with pytest.raises(store.ProjectError, match="entry file"):
        _project(db, workspace, name="Headless", files={"other.ts": "x"})
    project = _project(db, workspace, name="Guarded")
    with pytest.raises(store.ProjectError, match="entry file"):
        store.delete_file(db, project=project, path="index.tsx")


def test_projects_are_addressable_by_name_and_a_lone_project_needs_none(db, workspace):
    project = _project(db, workspace, name="By Name")
    assert (
        store.resolve(db, workspace_id=workspace["workspace_id"], name="by name").id
        == project.id
    )
    # A single project is the common case; the model should not have to name it.
    assert store.resolve(db, workspace_id=workspace["workspace_id"]).id == project.id
    _project(db, workspace, name="Second")
    with pytest.raises(store.ProjectError, match="Provide project_id or project name"):
        store.resolve(db, workspace_id=workspace["workspace_id"])


# --------------------------------------------------------------------------
# Editing


def test_edit_replaces_exactly_once(db, workspace):
    project = _project(db, workspace, name="Edits")
    store.write_file(
        db,
        workspace_id=workspace["workspace_id"],
        project=project,
        path="src/util.ts",
        content="const a = 1;\nconst b = 2;\n",
    )
    file, diff, count = store.edit_file(
        db, project=project, path="src/util.ts", find="const b = 2;", replace="const b = 3;"
    )
    assert count == 1
    assert "-const b = 2;" in diff and "+const b = 3;" in diff
    assert file.content == "const a = 1;\nconst b = 3;\n"


def test_ambiguous_edit_is_refused_unless_replace_all(db, workspace):
    project = _project(db, workspace, name="Ambiguous")
    store.write_file(
        db,
        workspace_id=workspace["workspace_id"],
        project=project,
        path="dup.ts",
        content="x\nx\nx\n",
    )
    with pytest.raises(store.ProjectError, match="appears 3 times"):
        store.edit_file(db, project=project, path="dup.ts", find="x", replace="y")
    _file, _diff, count = store.edit_file(
        db, project=project, path="dup.ts", find="x", replace="y", replace_all=True
    )
    assert count == 3


def test_edit_with_no_match_is_refused(db, workspace):
    project = _project(db, workspace, name="NoMatch")
    with pytest.raises(store.ProjectError, match="does not appear"):
        store.edit_file(db, project=project, path="index.tsx", find="zzz", replace="y")


def test_edit_of_a_missing_file_names_the_file(db, workspace):
    project = _project(db, workspace, name="Missing")
    with pytest.raises(store.ProjectError, match="is not in Missing"):
        store.edit_file(db, project=project, path="nope.ts", find="a", replace="b")


# --------------------------------------------------------------------------
# The agent tools


def test_write_tools_ask_and_reads_do_not(db, workspace):
    specs = registry_tools(db, _context(workspace))
    for name in ("list_projects", "fs_list", "fs_read"):
        assert specs[name].read_only is True, name
    for name in ("create_project", "fs_write", "fs_edit", "fs_delete"):
        assert specs[name].read_only is False, name
        assert specs[name].preview is not None, name


def test_fs_edit_preview_is_a_diff_and_does_not_mutate(db, workspace):
    project = _project(db, workspace, name="Preview")
    store.write_file(
        db,
        workspace_id=workspace["workspace_id"],
        project=project,
        path="app/main.ts",
        content="keep\nold line\nkeep\n",
    )
    context = _context(workspace)
    specs = registry_tools(db, context)
    preview = specs["fs_edit"].preview(
        db,
        context,
        {"project": "Preview", "path": "app/main.ts", "find": "old line", "replace": "new line"},
    )
    assert "-old line" in preview and "+new line" in preview
    assert "Preview/app/main.ts" in preview
    # The whole point: previewing must not apply the edit.
    file = store.read_file(db, project=project, path="app/main.ts")
    assert file.content == "keep\nold line\nkeep\n"


def test_preview_of_a_doomed_edit_explains_rather_than_raising(db, workspace):
    _project(db, workspace, name="Doomed")
    context = _context(workspace)
    specs = registry_tools(db, context)
    preview = specs["fs_edit"].preview(
        db, context, {"path": "index.tsx", "find": "zzz", "replace": "y"}
    )
    assert "will fail" in preview


def test_fs_write_preview_diffs_against_the_existing_file(db, workspace):
    project = _project(db, workspace, name="Rewrite")
    store.write_file(
        db,
        workspace_id=workspace["workspace_id"],
        project=project,
        path="a.ts",
        content="before\n",
    )
    context = _context(workspace)
    specs = registry_tools(db, context)
    overwrite = specs["fs_write"].preview(
        db, context, {"path": "a.ts", "content": "after\n"}
    )
    assert "-before" in overwrite and "+after" in overwrite
    creation = specs["fs_write"].preview(
        db, context, {"path": "b.ts", "content": "brand new\n"}
    )
    assert "+brand new" in creation and "-" not in creation.split("+brand new")[1]
    assert store.find_file(db, project_id=project.id, path="b.ts") is None


def test_tools_round_trip_write_read_list_and_delete(db, workspace):
    context = _context(workspace)
    specs = registry_tools(db, context)
    created = specs["create_project"].executor(db, context, {"name": "Roundtrip"})
    assert "Created project" in created.content

    assert "Created" in specs["fs_write"].executor(
        db, context, {"path": "./src//Chart.tsx", "content": "export const Chart = () => null;"}
    ).content

    read = json.loads(
        specs["fs_read"].executor(db, context, {"path": "src/Chart.tsx"}).content
    )
    assert read["content"] == "export const Chart = () => null;"
    assert read["path"] == "src/Chart.tsx"

    listing = json.loads(specs["fs_list"].executor(db, context, {}).content)
    assert [file["path"] for file in listing["files"]] == [
        "App.tsx",
        "index.tsx",
        "src/Chart.tsx",
    ]

    assert "Deleted" in specs["fs_delete"].executor(
        db, context, {"path": "src/Chart.tsx"}
    ).content
    listing = json.loads(specs["fs_list"].executor(db, context, {}).content)
    assert [file["path"] for file in listing["files"]] == ["App.tsx", "index.tsx"]


def test_tools_return_errors_instead_of_raising(db, workspace):
    context = _context(workspace)
    specs = registry_tools(db, context)
    _project(db, workspace, name="Safe")
    assert specs["fs_write"].executor(
        db, context, {"path": "../escape.ts", "content": "x"}
    ).content.startswith("Error:")
    assert specs["fs_read"].executor(
        db, context, {"path": "ghost.ts"}
    ).content.startswith("Error:")
    unknown = specs["fs_list"].executor(db, context, {"project": "Nonexistent"})
    assert unknown.content.startswith("Error:")
    # The model gets told what it could have asked for.
    assert "Safe" in unknown.content


def test_list_projects_reports_file_counts(db, workspace):
    context = _context(workspace)
    specs = registry_tools(db, context)
    assert "No projects yet" in specs["list_projects"].executor(db, context, {}).content
    _project(db, workspace, name="Counted")
    payload = json.loads(specs["list_projects"].executor(db, context, {}).content)
    assert payload["projects"][0]["name"] == "Counted"
    assert payload["projects"][0]["files"] == 2


@pytest.mark.parametrize(
    "files",
    [
        ["index.tsx"],
        [None],
        [["index.tsx", "x"]],
        [{"path": "index.tsx", "content": "x"}, "App.tsx"],
    ],
)
def test_create_project_survives_a_malformed_files_argument(db, workspace, files):
    """Models often send `files` as a list of paths. That must not raise."""
    context = _context(workspace)
    specs = registry_tools(db, context)
    result = specs["create_project"].executor(db, context, {"name": "Malformed", "files": files})
    assert result.content.startswith("Error:")
    # A preview that raises renders a blank approval card, so it must explain too.
    preview = specs["create_project"].preview(db, context, {"name": "Malformed", "files": files})
    assert "will fail" in preview
    assert store.find_by_name(db, workspace_id=workspace["workspace_id"], name="Malformed") is None


def test_project_tools_are_in_the_agent_registry(db, workspace):
    """Unregistered tools are invisible to the model, which is the same as absent."""
    from app.services.llm_tools import build_registry

    registry = build_registry(db, _context(workspace))
    for name in (
        "list_projects",
        "fs_list",
        "fs_read",
        "create_project",
        "fs_write",
        "fs_edit",
        "fs_delete",
    ):
        assert name in registry, name


def test_rest_surface_is_mounted_and_round_trips(client, workspace):
    """The router has to be included in the app, not merely defined."""
    created = client.post("/api/projects", json={"name": "REST", "description": "d"})
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    assert client.get("/api/projects").status_code == 200
    assert client.get(f"/api/projects/{project_id}").json()["entry_path"] == "index.tsx"

    written = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "./src//Chart.tsx", "content": "export const Chart = () => null;"},
    )
    assert written.status_code == 200
    assert written.json()["path"] == "src/Chart.tsx"

    escape = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "../../etc/passwd", "content": "nope"},
    )
    assert escape.status_code == 422

    removed = client.delete(
        f"/api/projects/{project_id}/files", params={"path": "src/Chart.tsx"}
    )
    assert removed.status_code == 204
    assert client.delete(f"/api/projects/{project_id}").status_code == 204
    assert client.get(f"/api/projects/{project_id}").status_code == 404


def test_snapshot_carries_everything_the_bundler_needs(db, workspace):
    project = _project(db, workspace, name="Bundle")
    payload = store.snapshot(db, project)
    assert payload["entry_path"] == "index.tsx"
    assert {file["path"] for file in payload["files"]} == {"index.tsx", "App.tsx"}
    assert payload["total_bytes"] > 0
