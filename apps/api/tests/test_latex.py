from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import Project, ProjectFile
from app.services.projects import store
from app.services.projects.latex import (
    BIBLIOGRAPHY_PATH,
    DEFAULT_ENTRY_PATH,
    KIND,
    WEB_ENTRY_PATH,
    WEB_KIND,
    has_documentclass,
    is_latex_kind,
    normalize_kind,
    packages_used,
    resolve_seed,
    seed_warnings,
    starter_files,
    strip_comments,
    unavailable_packages,
    validate_entry_path,
)


@pytest.fixture
def workspace(client):
    identity = client.get("/api/bootstrap").json()["identity"]
    created: list[str] = []
    yield identity, created
    db = SessionLocal()
    try:
        for project_id in created:
            db.query(ProjectFile).filter(ProjectFile.project_id == project_id).delete()
            db.query(Project).filter(Project.id == project_id).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The starter


def test_starter_is_non_empty_and_self_contained():
    files = starter_files("Gaussian Integrals")
    assert set(files) == {DEFAULT_ENTRY_PATH, BIBLIOGRAPHY_PATH}
    tex = files[DEFAULT_ENTRY_PATH]
    assert tex.strip()
    assert files[BIBLIOGRAPHY_PATH].strip()
    # The pieces that make it a real document rather than a stub.
    assert r"\documentclass" in tex
    assert r"\begin{document}" in tex and r"\end{document}" in tex
    assert "Gaussian Integrals" in tex
    assert r"\begin{align}" in tex
    assert r"\section" in tex
    # The bibliography has to point at the file that was actually seeded, and
    # cite a key that file defines, or the first compile fails on pass two.
    assert r"\bibliography{refs}" in tex
    assert r"\citep{knuth1984}" in tex
    assert "knuth1984" in files[BIBLIOGRAPHY_PATH]


def test_starter_loads_nothing_outside_the_offline_bundle():
    files = starter_files("Paper")
    assert unavailable_packages(files[DEFAULT_ENTRY_PATH]) == []


def test_starter_escapes_a_name_that_is_tex_syntax():
    tex = starter_files("Costs & Margins_2 100%")[DEFAULT_ENTRY_PATH]
    assert r"Costs \& Margins\_2 100\%" in tex
    # The escaped percent must not have turned into a comment that eats the line.
    assert has_documentclass(tex)


def test_starter_escapes_a_backslash_without_escaping_its_own_escape():
    # Escaping character by character re-reads what the previous pass wrote:
    # `\` becomes `\textbackslash{}` and the brace pass then mangles it into
    # `\textbackslash\{\}`, which typesets as `\{}` in the title.
    tex = starter_files(r"Q1\Q2")["main.tex"]
    assert r"\title{Q1\textbackslash{}Q2}" in tex
    assert r"\textbackslash\{" not in tex


def test_starter_title_survives_a_name_that_looks_like_a_template_token():
    # Substituting the tokens in sequence lets a later one match text an earlier
    # one inserted, so this name used to come out titled after the bibliography.
    tex = starter_files("@BIB@")["main.tex"]
    assert r"\title{@BIB@}" in tex
    assert r"\bibliography{refs}" in tex


def test_starter_in_a_subdirectory_still_finds_its_bibliography():
    files = starter_files("Paper", "paper/main.tex")
    assert set(files) == {"paper/main.tex", "paper/refs.bib"}
    # This engine runs bibtex from the entry file's own directory, and the .bib
    # sits beside the entry — so the reference is the bare stem. Emitting the
    # root-relative "paper/refs" makes bibtex exit 2, which was verified by
    # compiling both forms: the bare name produced a PDF, this one did not.
    assert r"\bibliography{refs}" in files["paper/main.tex"]
    assert r"\bibliography{paper/refs}" not in files["paper/main.tex"]


# ---------------------------------------------------------------------------
# The entry rule


def test_entry_path_must_be_a_tex_file():
    assert validate_entry_path("main.tex") == "main.tex"
    assert validate_entry_path("paper/Main.TEX") == "paper/Main.TEX"
    assert validate_entry_path("./chapters//intro.tex") == "chapters/intro.tex"
    assert validate_entry_path("") == DEFAULT_ENTRY_PATH


@pytest.mark.parametrize("path", ["refs.bib", "index.tsx", "main", "main.tex.bak"])
def test_entry_path_rejects_anything_that_is_not_tex(path):
    with pytest.raises(store.ProjectError, match=".tex"):
        validate_entry_path(path)


def test_entry_path_still_obeys_the_filesystem_rules():
    # Delegated to the store rather than reimplemented, so escapes stay refused.
    with pytest.raises(store.ProjectError):
        validate_entry_path("../outside.tex")
    with pytest.raises(store.ProjectError):
        validate_entry_path("/absolute.tex")


# ---------------------------------------------------------------------------
# Kinds and seeding


def test_normalize_kind():
    assert normalize_kind("") == WEB_KIND
    assert normalize_kind("  LaTeX ") == KIND
    assert is_latex_kind("latex") and not is_latex_kind("web")
    with pytest.raises(store.ProjectError, match="Unknown project kind"):
        normalize_kind("markdown")


def test_resolve_seed_defaults_per_kind():
    kind, entry, files = resolve_seed(kind="latex", name="Paper")
    assert (kind, entry) == (KIND, DEFAULT_ENTRY_PATH)
    assert entry in files

    kind, entry, files = resolve_seed()
    assert (kind, entry) == (WEB_KIND, WEB_ENTRY_PATH)
    assert files == store.STARTER_FILES


def test_resolve_seed_honours_supplied_files_and_validates_the_entry():
    kind, entry, files = resolve_seed(
        kind="latex", entry_path="doc.tex", files={"doc.tex": r"\documentclass{article}"}
    )
    assert (kind, entry, files) == (KIND, "doc.tex", {"doc.tex": r"\documentclass{article}"})
    with pytest.raises(store.ProjectError):
        resolve_seed(kind="latex", entry_path="doc.txt", files={"doc.txt": ""})


# ---------------------------------------------------------------------------
# Reading a document


def test_strip_comments_leaves_escaped_percent_alone():
    assert strip_comments(r"a % note") == "a "
    assert strip_comments(r"50\% off % note").strip() == r"50\% off"


def test_packages_used_reads_options_lists_and_groups():
    source = r"""
    \documentclass[11pt]{article}
    \usepackage[margin=1in]{geometry}
    \usepackage{amsmath, amssymb}
    \RequirePackage{xcolor}
    % \usepackage{tikz}
    """
    assert packages_used(source) == [
        "article",
        "geometry",
        "amsmath",
        "amssymb",
        "xcolor",
    ]


def test_unavailable_packages_names_what_the_bundle_lacks():
    assert unavailable_packages(r"\usepackage{tikz}") == ["tikz"]
    assert unavailable_packages(r"\documentclass{beamer}") == ["beamer"]
    assert unavailable_packages(r"\usepackage{amsmath}") == []
    # A commented-out package is not loaded, so it is not a problem.
    assert unavailable_packages("% \\usepackage{tikz}") == []


def test_has_documentclass():
    assert has_documentclass(r"\documentclass[12pt]{article}")
    assert not has_documentclass(r"% \documentclass{article}")
    assert not has_documentclass(r"\section{Intro}")


def test_seed_warnings_are_advisory_and_specific():
    assert seed_warnings(DEFAULT_ENTRY_PATH, starter_files("Paper")) == []

    warnings = seed_warnings("main.tex", {"main.tex": r"\section{Intro}"})
    assert len(warnings) == 1
    assert r"\documentclass" in warnings[0]

    warnings = seed_warnings(
        "main.tex", {"main.tex": "\\documentclass{article}\n\\usepackage{tikz}"}
    )
    assert len(warnings) == 1
    assert "tikz" in warnings[0]


# ---------------------------------------------------------------------------
# Composed with the virtual filesystem it shares with web projects


def test_a_seeded_latex_project_lands_in_the_project_filesystem(workspace):
    identity, created = workspace
    kind, entry, files = resolve_seed(kind="latex", name="Latex Seed Test")
    db = SessionLocal()
    try:
        project = store.create_project(
            db,
            workspace_id=identity["workspace_id"],
            name="Latex Seed Test",
            entry_path=entry,
            created_by=identity["user_id"],
            files=files,
        )
        created.append(project.id)
        assert project.entry_path == DEFAULT_ENTRY_PATH
        stored = {file.path: file.content for file in store.list_files(db, project_id=project.id)}
        assert set(stored) == {DEFAULT_ENTRY_PATH, BIBLIOGRAPHY_PATH}
        assert has_documentclass(stored[DEFAULT_ENTRY_PATH])
        # No parallel filesystem: the snapshot the preview reads is the same one
        # web projects use, so the compiler gets these files with no new plumbing.
        snapshot = store.snapshot(db, project)
        assert snapshot["entry_path"] == DEFAULT_ENTRY_PATH
        assert {file["path"] for file in snapshot["files"]} == set(stored)  # type: ignore[index,union-attr]
    finally:
        db.close()
    assert kind == KIND
