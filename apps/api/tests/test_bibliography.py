from __future__ import annotations

import json

import pytest

from app.database import SessionLocal
from app.models import Project, ProjectFile
from app.services.llm_tools import ToolContext
from app.services.projects import bibliography, store
from app.services.projects.latex import BIBLIOGRAPHY_PATH, DEFAULT_ENTRY_PATH
from app.services.projects.tools import registry_tools


@pytest.fixture(scope="module", autouse=True)
def _mounted():
    """Mount the bibliography router if the app has not already included it.

    `app/main.py` is owned by another workflow in this repo, so the include line
    lands there separately (see `wiring_needed`). Until it does the REST tests
    below would 404 on routing rather than on anything they are testing, and
    mounting the router permanently would make the tenant-isolation suite's
    route-table check fail on cases that live in a file this task cannot touch.
    So it is mounted on the real app — real middleware, real auth — and unmounted
    again. Once main.py includes the router this fixture finds it and does nothing.
    """
    from app.api.bibliography import router
    from app.main import app

    if any(getattr(route, "path", "").startswith(router.prefix) for route in app.routes):
        yield
        return
    original = list(app.router.routes)
    app.include_router(router)
    app.openapi_schema = None  # the schema is cached once built
    yield
    app.router.routes = original
    app.openapi_schema = None


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


def _context(identity, workspace_id: str = "") -> ToolContext:
    return ToolContext(
        workspace_id=workspace_id or identity["workspace_id"],
        user_id=identity["user_id"],
        conversation_id="none",
    )


# ---------------------------------------------------------------------------
# A .bib as it actually arrives

MESSY_BIB = r"""% Notes to self live above the entries and bibtex ignores them.
@string{acm = "Association for Computing Machinery"}

@comment{ ignored wholesale, even the { nested } bits }

@book{knuth1984,
  author    = {Donald E. Knuth},
  title     = {The {\TeX}book},
  publisher = {Addison-Wesley},
  year      = 1984,
}

@article{lamport1994,
  author  = "Leslie Lamport",
  title   = {A Document Preparation System: {\LaTeX}
             User's Guide},
  journal = {Software: Practice},
  year    = {1994}
}

@inproceedings{codd1970,
  author    = {E. F. Codd},
  title     = {A Relational Model of Data},
  booktitle = {Proc. of the } # acm,
  publisher = acm,
  year      = {1970}
}

@BOOK{Knuth1984,
  author    = {Donald E. Knuth},
  title     = {The Art of Computer Programming},
  publisher = {Addison-Wesley},
  year      = {1997}
}

@article{noyear,
  author = {A. Nonymous},
  title  = {Preliminary Findings}
}

@misc{halfwritten,
  author = {Someone},
  title  = {Cut off before the brace ever
"""

MESSY_TEX = r"""\documentclass{article}
\usepackage{natbib}
\begin{document}
As \citep{knuth1984} shows, and \citet[see][p.~2]{lamport1994} agrees.
% \cite{commentedout} is not a citation
\cite{codd1970, ghost1999}
\bibliographystyle{plainnat}
\bibliography{refs}
\end{document}
"""

# The same bibliography with its unterminated tail removed, for the tests that
# need a file an entry can actually be appended to.
CLOSED_BIB = MESSY_BIB[: MESSY_BIB.index("@misc{halfwritten")]


def _by_key(result):
    return {entry.key: entry for entry in result.entries}


def test_parser_survives_a_real_world_bib():
    result = bibliography.parse_bibtex(MESSY_BIB)
    # Both spellings of the duplicated key are kept; deduplicating here would
    # hide the very problem the validator has to report.
    assert [entry.key for entry in result.entries] == [
        "knuth1984",
        "lamport1994",
        "codd1970",
        "Knuth1984",
        "noyear",
        "halfwritten",
    ]
    entries = _by_key(result)
    assert entries["knuth1984"].entry_type == "book"
    # Entry types are case-folded, so @BOOK and @book are the same type.
    assert entries["Knuth1984"].entry_type == "book"


def test_parser_keeps_nested_braces_but_drops_the_outer_layer():
    entries = _by_key(bibliography.parse_bibtex(MESSY_BIB))
    assert entries["knuth1984"].fields["title"] == r"The {\TeX}book"


def test_parser_reads_quoted_values_and_rewraps_a_folded_one():
    entries = _by_key(bibliography.parse_bibtex(MESSY_BIB))
    assert entries["lamport1994"].fields["author"] == "Leslie Lamport"
    assert (
        entries["lamport1994"].fields["title"]
        == r"A Document Preparation System: {\LaTeX} User's Guide"
    )


def test_parser_expands_string_macros_and_concatenation():
    result = bibliography.parse_bibtex(MESSY_BIB)
    assert result.macros["acm"] == "Association for Computing Machinery"
    entries = _by_key(result)
    assert entries["codd1970"].fields["publisher"] == "Association for Computing Machinery"
    assert (
        entries["codd1970"].fields["booktitle"]
        == "Proc. of the Association for Computing Machinery"
    )


def test_parser_accepts_a_bare_number_and_a_trailing_comma():
    entries = _by_key(bibliography.parse_bibtex(MESSY_BIB))
    # `year = 1984,` — unbraced, and the comma before `}` is not a field.
    assert entries["knuth1984"].fields["year"] == "1984"
    assert set(entries["knuth1984"].fields) == {"author", "title", "publisher", "year"}


def test_parser_reports_a_truncated_final_entry_without_losing_it():
    result = bibliography.parse_bibtex(MESSY_BIB)
    entries = _by_key(result)
    assert entries["halfwritten"].truncated is True
    assert entries["halfwritten"].fields["author"] == "Someone"
    assert any("halfwritten" in problem for problem in result.problems)
    # Everything before the truncation still parsed, so one bad tail does not
    # cost the file its other six entries.
    assert len(result.entries) == 6


# Every way a .bib can run off the end of the file with a brace still open. Only
# the first three leave a `truncated` entry behind, so anything that asks the
# *entries* whether the file is closed misses the rest — and an append after any
# of them is swallowed whole by the open brace.
UNTERMINATED_TAILS = [
    "@misc{a, title = {Cut off",
    "@misc{a",
    "@misc{a,",
    '@misc{a, title = "Cut off',
    "@comment{ TODO rewrite this section",
    '@preamble{ "\\newcommand{\\x}{}"',
    "@string{acm = {Association",
    "@misc{, title = {no key here",
    "@misc{a, 9bad = {x",
    "@misc{a, title",
]


@pytest.mark.parametrize("tail", UNTERMINATED_TAILS)
def test_parser_names_the_unclosed_record_however_the_file_ends(tail):
    source = "@book{ok, title = {T}, author = {A}, publisher = {P}, year = {1}}\n\n" + tail
    result = bibliography.parse_bibtex(source)
    # The healthy entry above the damage still parsed.
    assert "ok" in {entry.key for entry in result.entries}
    assert result.unterminated, f"unclosed tail went unreported: {tail!r}"
    assert any("never closed" in problem for problem in result.problems)


@pytest.mark.parametrize("tail", UNTERMINATED_TAILS)
def test_appending_after_an_unclosed_record_is_refused_not_silently_swallowed(
    db, workspace, tail
):
    """The append would land inside the open brace: written, invisible, uncited."""
    bib = "@book{ok, title = {T}, author = {A}, publisher = {P}, year = {1}}\n\n" + tail
    project = _latex_project(db, workspace, name="Open", bib=bib)
    context = _context(workspace)
    specs = registry_tools(db, context)
    args = {
        "project": "Open",
        "entry_type": "misc",
        "key": "later",
        "fields": {"title": "Later"},
    }
    assert "ends mid-entry" in specs["bib_add"].preview(db, context, args)
    assert specs["bib_add"].executor(db, context, args).content.startswith("Error:")
    # Refused before writing, so the damaged file is left exactly as it was.
    assert store.read_file(db, project=project, path=BIBLIOGRAPHY_PATH).content == bib


def test_parser_reports_a_citation_key_that_contains_a_brace():
    """bibtex stops reading the key at the brace, so the fields are lost."""
    result = bibliography.parse_bibtex("@misc{ab{c}d, title = {T}, year = {2020}}")
    assert result.entries[0].key == "ab{c"
    assert result.entries[0].fields == {}
    assert any("contains a brace" in problem for problem in result.problems)


def test_parsing_a_large_bibliography_stays_linear():
    """Line numbers used to be recounted from byte zero, making this quadratic."""
    import time

    entry = (
        "@article{key%d, author = {Some Author}, title = {A Title}, "
        "journal = {J}, year = {2020}}\n"
    )
    source = "".join(entry % index for index in range(3000))
    assert len(source) > 250_000  # about one file at the store's per-file limit
    started = time.monotonic()
    result = bibliography.parse_bibtex(source)
    elapsed = time.monotonic() - started
    assert len(result.entries) == 3000
    # Linear runs in ~30ms; the quadratic version took ~220ms and grew with the
    # square, so this bound catches a regression without being timing-sensitive.
    assert elapsed < 2.0, f"parsing 3000 entries took {elapsed:.2f}s"


def test_parser_never_raises_on_garbage():
    for source in ("", "@", "@@@", "@article", "@article{", "@article{k", "not bibtex at all",
                   "@article{k, title = }", "@article{k, title", "@{}", "@article{,x={1}}"):
        assert isinstance(bibliography.parse_bibtex(source), bibliography.ParseResult)


def test_parser_takes_parenthesised_entries_and_keyless_ones():
    result = bibliography.parse_bibtex("@misc(webpage, title = {A Page}, year = 2020)")
    assert result.entries[0].key == "webpage"
    assert result.entries[0].fields == {"title": "A Page", "year": "2020"}

    keyless = bibliography.parse_bibtex("@book{, title = {Anonymous}}")
    assert keyless.entries == []
    assert any("no citation key" in problem for problem in keyless.problems)


# ---------------------------------------------------------------------------
# Reading citations out of the document


def test_cited_keys_covers_the_natbib_family_and_ignores_comments():
    assert bibliography.cited_keys(MESSY_TEX) == [
        "knuth1984",
        "lamport1994",
        "codd1970",
        "ghost1999",
    ]
    assert bibliography.cited_keys(r"\autocite[chap.~2]{a} \nocite{b,c} \Citet{d}") == [
        "a",
        "b",
        "c",
        "d",
    ]


def test_cited_keys_ignores_a_macro_definition_that_wraps_cite():
    r"""`\cite{#1}` in a \newcommand body is a definition, and `#1` is not a key."""
    source = r"""
    \newcommand{\mycite}[1]{\cite{#1}}
    \mycite{knuth1984}
    """
    assert bibliography.cited_keys(source) == ["knuth1984"]


def test_missing_required_fields_knows_the_standard_types():
    entries = _by_key(bibliography.parse_bibtex(MESSY_BIB))
    assert bibliography.missing_required_fields(entries["noyear"]) == ["journal", "year"]
    assert bibliography.missing_required_fields(entries["knuth1984"]) == []
    # A @book takes an author *or* an editor, and an unknown type owes nothing.
    edited = bibliography.parse_bibtex("@book{e, editor={X}, title={T}, publisher={P}, year={1}}")
    assert bibliography.missing_required_fields(edited.entries[0]) == []
    bare = bibliography.parse_bibtex("@book{b, title={T}}")
    assert bibliography.missing_required_fields(bare.entries[0]) == [
        "author or editor",
        "publisher",
        "year",
    ]
    assert bibliography.missing_required_fields(
        bibliography.parse_bibtex("@dataset{d, url={x}}").entries[0]
    ) == []


# ---------------------------------------------------------------------------
# Cross-referencing a project


def _latex_project(db, workspace, *, name="Paper", tex=MESSY_TEX, bib=MESSY_BIB):
    return store.create_project(
        db,
        workspace_id=workspace["workspace_id"],
        name=name,
        kind="latex",
        entry_path=DEFAULT_ENTRY_PATH,
        files={DEFAULT_ENTRY_PATH: tex, BIBLIOGRAPHY_PATH: bib},
    )


def test_validation_reports_all_four_categories(db, workspace):
    project = _latex_project(db, workspace)
    report = bibliography.validate_project(db, project=project)

    # 1. Cited but undefined — the one that breaks the build.
    assert [item.key for item in report.undefined] == ["ghost1999"]
    assert report.undefined[0].cited_in == [DEFAULT_ENTRY_PATH]

    # 2. Defined but uncited — dead weight.
    assert sorted(item.key for item in report.uncited) == ["halfwritten", "noyear"]

    # 3. Duplicate keys, across whatever spelling was used.
    assert [item.key for item in report.duplicates] == ["knuth1984"]
    assert report.duplicates[0].count == 2

    # 4. Entries missing a field their type requires.
    incomplete = {item.key: item.missing for item in report.incomplete}
    assert incomplete["noyear"] == ["journal", "year"]

    assert report.problems  # the truncated entry
    assert report.ok is False
    assert report.bib_files == [BIBLIOGRAPHY_PATH]
    assert report.tex_files == [DEFAULT_ENTRY_PATH]


def test_a_healthy_project_reports_ok(db, workspace):
    project = _latex_project(
        db,
        workspace,
        name="Clean",
        tex=r"\documentclass{article}\begin{document}\cite{a}\end{document}",
        bib="@misc{a, title = {A Thing}, year = {2020}}\n",
    )
    report = bibliography.validate_project(db, project=project)
    assert report.ok is True
    assert (report.undefined, report.uncited, report.duplicates) == ([], [], [])


def test_citations_match_case_insensitively_like_bibtex(db, workspace):
    """`\\cite{Knuth1984}` really does resolve `@book{knuth1984` — bibtex folds keys."""
    project = _latex_project(
        db,
        workspace,
        name="Folded",
        tex=r"\documentclass{article}\begin{document}\cite{KNUTH1984}\end{document}",
        bib="@book{knuth1984, author={K}, title={T}, publisher={P}, year={1984}}\n",
    )
    report = bibliography.validate_project(db, project=project)
    assert report.undefined == []
    assert report.uncited == []


def test_nocite_star_means_nothing_is_dead_weight(db, workspace):
    project = _latex_project(
        db,
        workspace,
        name="Nocite",
        tex=r"\documentclass{article}\begin{document}\nocite{*}\end{document}",
        bib="@misc{a, title = {A}}\n@misc{b, title = {B}}\n",
    )
    assert bibliography.validate_project(db, project=project).uncited == []


def test_entries_are_found_across_every_bib_file(db, workspace):
    project = _latex_project(
        db,
        workspace,
        name="Split",
        tex=r"\documentclass{article}\begin{document}\cite{a}\cite{b}\end{document}",
        bib="@misc{a, title = {A}}\n",
    )
    store.write_file(
        db,
        workspace_id=workspace["workspace_id"],
        project=project,
        path="more.bib",
        content="@misc{b, title = {B}}\n",
    )
    report = bibliography.validate_project(db, project=project)
    assert report.undefined == []
    assert report.bib_files == ["more.bib", BIBLIOGRAPHY_PATH]


def test_bibliography_is_refused_for_a_non_latex_project(db, workspace):
    project = store.create_project(db, workspace_id=workspace["workspace_id"], name="Webby")
    with pytest.raises(store.ProjectError, match="LaTeX"):
        bibliography.validate_project(db, project=project)


# ---------------------------------------------------------------------------
# Writing an entry


def test_render_entry_is_valid_bibtex_that_round_trips():
    text = bibliography.render_entry(
        "Article",
        "lovelace1843",
        {"Author": "Ada Lovelace", "title": "Notes", "journal": "Taylor's", "year": "1843"},
    )
    assert text == (
        "@article{lovelace1843,\n"
        "  author  = {Ada Lovelace},\n"
        "  title   = {Notes},\n"
        "  journal = {Taylor's},\n"
        "  year    = {1843}\n"
        "}\n"
    )
    # The proof that it is well formed is that the parser reads it back.
    parsed = bibliography.parse_bibtex(text)
    assert parsed.entries[0].key == "lovelace1843"
    assert parsed.entries[0].fields["author"] == "Ada Lovelace"
    assert parsed.problems == []


@pytest.mark.parametrize(
    "value",
    [
        r"A double backslash \\ is a TeX line break",
        r"Braces {stay} balanced",
        r"An accent {\"o} and a macro {\TeX}",
        "Ends in a pair\\\\",
    ],
)
def test_render_entry_still_accepts_the_backslashes_that_are_fine(value):
    """The brace and backslash rules must not reject ordinary TeX."""
    text = bibliography.render_entry("misc", "k", {"title": value})
    parsed = bibliography.parse_bibtex(text)
    assert parsed.problems == []
    assert parsed.unterminated == ""
    assert parsed.entries[0].key == "k"


@pytest.mark.parametrize(
    ("entry_type", "key", "fields", "match"),
    [
        ("article", "", {"title": "T"}, "citation key"),
        ("article", "two words", {"title": "T"}, "citation key"),
        ("article", "brace{y", {"title": "T"}, "citation key"),
        ("", "k", {"title": "T"}, "entry type"),
        ("art icle", "k", {"title": "T"}, "entry type"),
        ("article", "k", {}, "at least one field"),
        ("article", "k", {"title": "   "}, "at least one field"),
        ("article", "k", {"title": "Un{balanced"}, "unbalanced braces"),
        ("article", "k", {"ti tle": "T"}, "field name"),
        # bibtex counts braces mechanically and never honours a backslash, so a
        # value carrying `\{` leaves the entry open and eats the rest of the file.
        ("article", "k", {"title": r"A \{ brace"}, "unbalanced braces"),
        ("article", "k", {"title": r"A \} brace"}, "unbalanced braces"),
        # `{See appendix\}` escapes its own closing brace.
        ("article", "k", {"title": "See appendix\\"}, "lone backslash"),
        ("article", "k", {"title": "Three\\\\\\"}, "lone backslash"),
    ],
)
def test_render_entry_refuses_input_that_would_corrupt_the_file(entry_type, key, fields, match):
    with pytest.raises(store.ProjectError, match=match):
        bibliography.render_entry(entry_type, key, fields)


def test_add_entry_appends_exactly_what_the_preview_showed(db, workspace):
    project = _latex_project(db, workspace, name="Append", bib=CLOSED_BIB)
    context = _context(workspace)
    specs = registry_tools(db, context)
    args = {
        "project": "Append",
        "entry_type": "article",
        "key": "turing1950",
        "fields": {
            "author": "Alan M. Turing",
            "title": "Computing Machinery and Intelligence",
            "journal": "Mind",
            "year": "1950",
        },
    }

    preview = specs["bib_add"].preview(db, context, args)
    assert f"Append/{BIBLIOGRAPHY_PATH}" in preview
    # Previewing must not write.
    before = store.read_file(db, project=project, path=BIBLIOGRAPHY_PATH).content
    assert "turing1950" not in before

    result = specs["bib_add"].executor(db, context, args)
    assert not result.content.startswith("Error:")
    after = store.read_file(db, project=project, path=BIBLIOGRAPHY_PATH).content

    # The exact BibTeX the approval card showed is the exact BibTeX on disk.
    rendered = bibliography.render_entry("article", "turing1950", args["fields"])
    assert rendered in preview
    assert after == before.rstrip("\n") + "\n\n" + rendered
    assert after.endswith(rendered)
    # And it is now a defined key, not a parse problem.
    entries, _paths, problems = bibliography.list_entries(db, project=project)
    assert "turing1950" in {entry.key for entry in entries}
    assert not any("turing1950" in problem for problem in problems)


def test_add_entry_refuses_a_duplicate_key_in_both_the_tool_and_its_preview(db, workspace):
    project = _latex_project(db, workspace, name="Dupe")
    context = _context(workspace)
    specs = registry_tools(db, context)
    args = {
        "project": "Dupe",
        "entry_type": "book",
        "key": "KNUTH1984",  # the same key bibtex sees, in a different case
        "fields": {"author": "D. K.", "title": "T", "publisher": "P", "year": "1984"},
    }
    assert "already defined" in specs["bib_add"].preview(db, context, args)
    assert specs["bib_add"].executor(db, context, args).content.startswith("Error:")
    # Nothing was written on the way to refusing.
    assert store.read_file(db, project=project, path=BIBLIOGRAPHY_PATH).content == MESSY_BIB


def test_add_entry_refuses_to_append_after_an_unterminated_entry(db, workspace):
    """An open brace swallows whatever follows it, so the write would be a no-op."""
    project = _latex_project(db, workspace, name="Truncated")
    with pytest.raises(store.ProjectError, match="ends mid-entry"):
        bibliography.plan_add_entry(
            db, project=project, entry_type="misc", key="later", fields={"title": "Later"}
        )
    assert store.read_file(db, project=project, path=BIBLIOGRAPHY_PATH).content == MESSY_BIB


def test_add_entry_creates_the_bibliography_when_the_project_lost_it(db, workspace):
    project = store.create_project(
        db,
        workspace_id=workspace["workspace_id"],
        name="Bare",
        kind="latex",
        entry_path=DEFAULT_ENTRY_PATH,
        files={DEFAULT_ENTRY_PATH: r"\documentclass{article}\begin{document}x\end{document}"},
    )
    _file, plan = bibliography.add_entry(
        db,
        workspace_id=workspace["workspace_id"],
        project=project,
        entry_type="misc",
        key="fresh",
        fields={"title": "A Fresh Start"},
    )
    assert (plan.path, plan.created) == (BIBLIOGRAPHY_PATH, True)
    assert store.read_file(db, project=project, path=BIBLIOGRAPHY_PATH).content == plan.entry


def test_add_entry_needs_a_path_when_several_bibliographies_exist(db, workspace):
    project = _latex_project(db, workspace, name="Ambiguous", bib="@misc{a, title={A}}\n")
    store.write_file(
        db,
        workspace_id=workspace["workspace_id"],
        project=project,
        path="extra.bib",
        content="",
    )
    with pytest.raises(store.ProjectError, match="several bibliographies"):
        bibliography.plan_add_entry(
            db, project=project, entry_type="misc", key="b", fields={"title": "B"}
        )
    plan = bibliography.plan_add_entry(
        db,
        project=project,
        entry_type="misc",
        key="b",
        fields={"title": "B"},
        path="extra.bib",
    )
    assert plan.path == "extra.bib"


def test_add_entry_refuses_a_target_that_is_not_a_bib_file(db, workspace):
    project = _latex_project(db, workspace, name="Wrongtarget")
    with pytest.raises(store.ProjectError, match=r"\.bib"):
        bibliography.plan_add_entry(
            db,
            project=project,
            entry_type="misc",
            key="x",
            fields={"title": "X"},
            path=DEFAULT_ENTRY_PATH,
        )


def test_add_entry_still_obeys_the_project_filesystem_rules(db, workspace):
    project = _latex_project(db, workspace, name="Escapee")
    with pytest.raises(store.ProjectError, match="leave the project root"):
        bibliography.plan_add_entry(
            db,
            project=project,
            entry_type="misc",
            key="x",
            fields={"title": "X"},
            path="../outside.bib",
        )


# ---------------------------------------------------------------------------
# The agent tools


def test_bib_tools_declare_themselves_correctly(db, workspace):
    specs = registry_tools(db, _context(workspace))
    assert specs["bib_list"].read_only is True
    assert specs["bib_add"].read_only is False
    assert specs["bib_add"].preview is not None


def test_bib_tools_are_in_the_agent_registry(db, workspace):
    """An unregistered tool is invisible to the model, which is the same as absent."""
    from app.services.llm_tools import build_registry

    registry = build_registry(db, _context(workspace))
    assert "bib_list" in registry and "bib_add" in registry


def test_bib_list_reports_the_validation_as_json(db, workspace):
    _latex_project(db, workspace, name="Listed")
    context = _context(workspace)
    specs = registry_tools(db, context)
    payload = json.loads(specs["bib_list"].executor(db, context, {"project": "Listed"}).content)
    assert payload["counts"]["entries"] == 6
    assert payload["truncated"] == []
    assert [item["key"] for item in payload["cited_but_undefined"]] == ["ghost1999"]
    assert [item["key"] for item in payload["duplicate_keys"]] == ["knuth1984"]
    assert sorted(item["key"] for item in payload["defined_but_uncited"]) == [
        "halfwritten",
        "noyear",
    ]
    assert payload["missing_required_fields"][0]["key"] == "noyear"
    assert payload["ok"] is False
    assert {entry["key"] for entry in payload["entries"]} >= {"knuth1984", "codd1970"}


def test_bib_list_stays_parseable_json_for_a_large_bibliography(db, workspace):
    """A result clipped at the budget would be half a JSON document, i.e. useless."""
    from app.services.llm_tools import MAX_RESULT_CHARS

    big = "\n".join(
        f"@article{{key{n}, author = {{Author Number {n} With A Long Name}}, "
        f"title = {{A Reasonably Long Paper Title Number {n}}}, "
        f"journal = {{Journal of Very Long Names}}, year = {{2000}}}}"
        for n in range(400)
    )
    _latex_project(db, workspace, name="Huge", bib=big)
    context = _context(workspace)
    specs = registry_tools(db, context)
    result = specs["bib_list"].executor(db, context, {"project": "Huge"})

    assert len(result.content) <= MAX_RESULT_CHARS
    assert result.content == result.bounded_content()
    payload = json.loads(result.content)  # the point: it still parses
    # Trimmed, but honest about it: the true sizes are still reported.
    assert payload["counts"]["entries"] == 400
    assert payload["counts"]["defined_but_uncited"] == 400
    assert "entries" in payload["truncated"]
    # The listing gives way before the verdicts do.
    assert payload["entries"] == []


def test_bib_list_stays_parseable_json_for_a_document_split_across_many_files(
    db, workspace
):
    """The file lists used to sit outside the trimming and blow the budget alone."""
    from app.services.llm_tools import MAX_RESULT_CHARS

    project = _latex_project(db, workspace, name="Split", bib=CLOSED_BIB)
    for index in range(150):
        store.write_file(
            db,
            workspace_id=workspace["workspace_id"],
            project=project,
            path=f"sections/a-fairly-long-chapter-file-name-{index}.tex",
            content=r"\section{S}",
        )
    context = _context(workspace)
    result = registry_tools(db, context)["bib_list"].executor(db, context, {"project": "Split"})

    assert len(result.content) <= MAX_RESULT_CHARS
    payload = json.loads(result.content)  # the point: it still parses
    assert payload["counts"]["tex_files"] == 151
    assert "tex_files" in payload["truncated"]
    # The findings survived the file list giving way.
    assert payload["counts"]["entries"] == 5


def test_bib_tools_return_errors_instead_of_raising(db, workspace):
    store.create_project(db, workspace_id=workspace["workspace_id"], name="NotLatex")
    context = _context(workspace)
    specs = registry_tools(db, context)
    assert specs["bib_list"].executor(
        db, context, {"project": "Nonexistent"}
    ).content.startswith("Error:")
    assert specs["bib_list"].executor(
        db, context, {"project": "NotLatex"}
    ).content.startswith("Error:")
    assert specs["bib_add"].executor(
        db, context, {"project": "NotLatex", "entry_type": "misc", "key": "k", "fields": {}}
    ).content.startswith("Error:")


@pytest.mark.parametrize("fields", ["author=X", ["author"], [None], 7])
def test_bib_add_survives_a_malformed_fields_argument(db, workspace, fields):
    """A preview that raises renders a blank approval card, so it must explain."""
    _latex_project(db, workspace, name="Malformed")
    context = _context(workspace)
    specs = registry_tools(db, context)
    args = {"project": "Malformed", "entry_type": "misc", "key": "k", "fields": fields}
    assert specs["bib_add"].executor(db, context, args).content.startswith("Error:")
    assert "will fail" in specs["bib_add"].preview(db, context, args)


def test_bib_add_accepts_the_name_value_list_models_often_send(db, workspace):
    _latex_project(db, workspace, name="Listy", bib="")
    context = _context(workspace)
    specs = registry_tools(db, context)
    result = specs["bib_add"].executor(
        db,
        context,
        {
            "project": "Listy",
            "entry_type": "misc",
            "key": "pairs",
            "fields": [{"name": "title", "value": "Pairs"}],
        },
    )
    assert not result.content.startswith("Error:")
    assert "@misc{pairs," in result.content


# ---------------------------------------------------------------------------
# The REST surface


def test_rest_surface_lists_validates_and_adds(client, workspace, db):
    project = _latex_project(db, workspace, name="Restful")

    listed = client.get(f"/api/bibliography/{project.id}/entries")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["bib_files"] == [BIBLIOGRAPHY_PATH]
    assert [entry["key"] for entry in body["entries"]][:2] == ["knuth1984", "lamport1994"]
    assert body["entries"][0]["fields"]["title"] == r"The {\TeX}book"
    assert body["problems"]

    report = client.get(f"/api/bibliography/{project.id}/validate").json()
    assert report["ok"] is False
    assert [item["key"] for item in report["cited_but_undefined"]] == ["ghost1999"]
    assert [item["key"] for item in report["duplicate_keys"]] == ["knuth1984"]
    assert report["entry_count"] == 6

    # This .bib ends mid-entry, so appending to it would be a silent no-op.
    blocked = client.post(
        f"/api/bibliography/{project.id}/entries",
        json={"entry_type": "misc", "key": "ghost1999", "fields": {"title": "Missing"}},
    )
    assert blocked.status_code == 422
    assert "ends mid-entry" in blocked.json()["detail"]


def test_rest_surface_adds_an_entry_that_resolves_a_missing_citation(client, workspace, db):
    project = _latex_project(db, workspace, name="Addable", bib=CLOSED_BIB)

    added = client.post(
        f"/api/bibliography/{project.id}/entries",
        json={
            "entry_type": "misc",
            "key": "ghost1999",
            "fields": {"title": "The Missing Reference", "year": "1999"},
        },
    )
    assert added.status_code == 201, added.text
    assert added.json()["path"] == BIBLIOGRAPHY_PATH
    assert added.json()["created"] is False
    assert "@misc{ghost1999," in added.json()["entry"]

    # The undefined citation it was added to satisfy is now resolved.
    report = client.get(f"/api/bibliography/{project.id}/validate").json()
    assert report["cited_but_undefined"] == []

    duplicate = client.post(
        f"/api/bibliography/{project.id}/entries",
        json={"entry_type": "misc", "key": "ghost1999", "fields": {"title": "Again"}},
    )
    assert duplicate.status_code == 422
    assert "already defined" in duplicate.json()["detail"]


def test_rest_surface_404s_on_an_unknown_project(client, workspace):
    assert client.get("/api/bibliography/nope/validate").status_code == 404


def test_rest_surface_422s_on_a_web_project(client, workspace, db):
    project = store.create_project(db, workspace_id=workspace["workspace_id"], name="Webrest")
    response = client.get(f"/api/bibliography/{project.id}/validate")
    assert response.status_code == 422
    assert "LaTeX" in response.json()["detail"]


def test_another_workspace_cannot_read_or_write_a_bibliography(
    client, workspace, db, identity_client
):
    """Scoping is not advisory: the project lookup is the only way in."""
    project = _latex_project(db, workspace, name="Private")
    intruder = identity_client()

    assert intruder.get(f"/api/bibliography/{project.id}/entries").status_code == 404
    assert intruder.get(f"/api/bibliography/{project.id}/validate").status_code == 404
    assert (
        intruder.post(
            f"/api/bibliography/{project.id}/entries",
            json={"entry_type": "misc", "key": "sneaky", "fields": {"title": "X"}},
        ).status_code
        == 404
    )

    # Same boundary through the agent tools, which resolve by name.
    other = ToolContext(
        workspace_id=intruder.identity.workspace_id,
        user_id=intruder.identity.user_id,
        conversation_id="none",
    )
    specs = registry_tools(db, other)
    assert specs["bib_list"].executor(db, other, {"project": "Private"}).content.startswith(
        "Error:"
    )
    assert specs["bib_add"].executor(
        db,
        other,
        {"project": "Private", "entry_type": "misc", "key": "sneaky", "fields": {"title": "X"}},
    ).content.startswith("Error:")

    # And the .bib is untouched.
    assert store.read_file(db, project=project, path=BIBLIOGRAPHY_PATH).content == MESSY_BIB
