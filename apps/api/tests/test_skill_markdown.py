"""The SKILL.md bridge, exercised as pure text transforms.

No DB, no HTTP client — `services.skill_markdown` is deliberately free of both,
so these tests are plain function calls. Three families:

1. parse: every tolerance (quotes, unknown keys, CRLF, BOM, clipping) and every
   refusal, with the exact human-readable message the route will surface.
2. render: the quoting rule, the omitted-default-title rule, and the bound
   checks that keep an export from producing a file the importer refuses.
3. the round-trip law `parse(render(x)) == x`, on the adversarial content the
   law exists for — colons, quotes, backslashes, `---` lines inside the body.
"""
from __future__ import annotations

import re

import pytest

from app.services.skill_markdown import (
    ParsedSkill,
    derived_title,
    parse_skill_markdown,
    render_skill_markdown,
)

# ---------------------------------------------------------------------------
# parse: the happy paths and tolerances
# ---------------------------------------------------------------------------


def test_parse_minimal_two_key_file() -> None:
    parsed = parse_skill_markdown(
        "---\nname: make-pdf\ndescription: Turn a doc into a PDF\n---\nDo the thing.\n"
    )
    assert parsed == ParsedSkill(
        name="make-pdf",
        title="Make Pdf",
        description="Turn a doc into a PDF",
        body="Do the thing.\n",
    )


def test_parse_explicit_title_wins_over_derived() -> None:
    parsed = parse_skill_markdown(
        "---\nname: make-pdf\ntitle: PDF Maker\ndescription: d\n---\nbody\n"
    )
    assert parsed.title == "PDF Maker"


def test_parse_missing_description_defaults_to_empty() -> None:
    parsed = parse_skill_markdown("---\nname: solo\n---\nbody\n")
    assert parsed.description == ""
    assert parsed.title == "Solo"


def test_parse_empty_description_value() -> None:
    parsed = parse_skill_markdown("---\nname: solo\ndescription:\n---\nbody\n")
    assert parsed.description == ""


def test_derived_title_capitalizes_each_segment() -> None:
    assert derived_title("make-pdf") == "Make Pdf"
    assert derived_title("a2b") == "A2b"
    assert derived_title("x") == "X"


def test_parse_double_quoted_values_unescape() -> None:
    parsed = parse_skill_markdown(
        '---\nname: q\ndescription: "colon: here and a \\" quote"\n---\nbody\n'
    )
    assert parsed.description == 'colon: here and a " quote'


def test_parse_double_quoted_backslash_runs() -> None:
    # \\\" inside quotes is an escaped backslash followed by an escaped quote —
    # the pairing a naive chained str.replace gets wrong.
    parsed = parse_skill_markdown(
        '---\nname: q\ndescription: "a\\\\\\"b"\n---\nbody\n'
    )
    assert parsed.description == 'a\\"b'


def test_parse_single_quoted_values_undouble() -> None:
    parsed = parse_skill_markdown(
        "---\nname: q\ndescription: 'it''s quoted'\n---\nbody\n"
    )
    assert parsed.description == "it's quoted"


def test_parse_value_containing_but_not_wrapped_in_quotes_is_verbatim() -> None:
    parsed = parse_skill_markdown(
        '---\nname: q\ndescription: he said "hi" loudly\n---\nbody\n'
    )
    assert parsed.description == 'he said "hi" loudly'


def test_parse_ignores_unknown_keys_and_their_nested_blocks() -> None:
    text = (
        "---\n"
        "name: rich\n"
        "license: MIT\n"
        "metadata:\n"
        "  author: someone\n"
        "  tags:\n"
        "    - a\n"
        "    - b\n"
        "allowed-tools:\n"
        "- Bash\n"
        "description: kept\n"
        "---\n"
        "body\n"
    )
    parsed = parse_skill_markdown(text)
    assert parsed.name == "rich"
    assert parsed.description == "kept"


def test_parse_ignores_blank_lines_comments_and_bare_words() -> None:
    text = "---\n\n# a comment\njunk-without-colon\nname: ok\n---\nbody\n"
    assert parse_skill_markdown(text).name == "ok"


def test_parse_last_duplicate_key_wins() -> None:
    text = "---\nname: first\nname: second\n---\nbody\n"
    assert parse_skill_markdown(text).name == "second"


def test_parse_accepts_crlf_line_endings() -> None:
    text = "---\r\nname: crlf\r\ndescription: d\r\n---\r\nline one\r\nline two\r\n"
    parsed = parse_skill_markdown(text)
    assert parsed.name == "crlf"
    assert parsed.body == "line one\nline two\n"


def test_parse_accepts_utf8_bom() -> None:
    text = "\ufeff---\nname: bom\n---\nbody\n"
    assert parse_skill_markdown(text).name == "bom"


def test_parse_clips_overlong_description_and_title() -> None:
    text = (
        "---\nname: clip\ntitle: " + "T" * 200 + "\ndescription: " + "d" * 600
        + "\n---\nbody\n"
    )
    parsed = parse_skill_markdown(text)
    assert parsed.title == "T" * 160
    assert parsed.description == "d" * 500


def test_parse_body_keeps_dashes_and_fences_verbatim() -> None:
    body = "intro\n---\nmiddle\n```sh\necho hi\n```\n---\n"
    parsed = parse_skill_markdown("---\nname: fenced\n---\n" + body)
    assert parsed.body == body


def test_parse_closing_fence_tolerates_trailing_spaces() -> None:
    parsed = parse_skill_markdown("---\nname: ok\n---   \nbody\n")
    assert parsed.body == "body\n"


# ---------------------------------------------------------------------------
# parse: every refusal, message verbatim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "name: no-fence\n---\nbody\n",
        "body only, no frontmatter at all",
        "",
        " ---\nname: indented-fence\n---\nbody\n",
        "---",  # fence with no newline: nothing after it to parse
    ],
)
def test_parse_requires_leading_fence(text: str) -> None:
    with pytest.raises(ValueError, match=r"^SKILL\.md must start with a --- frontmatter block$"):
        parse_skill_markdown(text)


def test_parse_requires_closing_fence() -> None:
    with pytest.raises(ValueError, match=r"^frontmatter block is never closed by a --- line$"):
        parse_skill_markdown("---\nname: open\ndescription: d\nbody without close\n")


def test_parse_requires_name() -> None:
    with pytest.raises(ValueError, match=r"^frontmatter is missing 'name'$"):
        parse_skill_markdown("---\ndescription: only\n---\nbody\n")


@pytest.mark.parametrize("bad", ["Bad-Name", "has_underscore", "-leading", "trailing-", "a b", ""])
def test_parse_rejects_non_slug_names(bad: str) -> None:
    with pytest.raises(
        ValueError, match=rf"^name '{re.escape(bad)}' must be a lowercase kebab-case slug$"
    ):
        parse_skill_markdown(f"---\nname: {quoted(bad)}\n---\nbody\n")


def test_parse_rejects_overlong_name() -> None:
    long = "a" * 81
    with pytest.raises(ValueError, match=rf"^name '{long}' is longer than 80 characters$"):
        parse_skill_markdown(f"---\nname: {long}\n---\nbody\n")


@pytest.mark.parametrize(
    "tail",
    [
        "---\nname: empty-body\n---\n",
        "---\nname: empty-body\n---\n   \n\n",
        "---\nname: empty-body\n---",  # close fence at EOF, no body at all
    ],
)
def test_parse_rejects_empty_body(tail: str) -> None:
    with pytest.raises(ValueError, match=r"^body is empty$"):
        parse_skill_markdown(tail)


def test_parse_rejects_overlong_body() -> None:
    with pytest.raises(ValueError, match=r"^body is longer than 20000 characters$"):
        parse_skill_markdown("---\nname: big\n---\n" + "x" * 20001)


@pytest.mark.parametrize("key", ["name", "description"])
def test_parse_rejects_multiline_values_for_required_keys(key: str) -> None:
    text = f"---\n{key}:\n  first\n  second\nname: ok\n---\nbody\n"
    with pytest.raises(
        ValueError, match=rf"^frontmatter key '{key}' must be a single-line value$"
    ):
        parse_skill_markdown(text)


def test_parse_rejects_list_continuation_under_description() -> None:
    text = "---\nname: ok\ndescription:\n- one\n- two\n---\nbody\n"
    with pytest.raises(
        ValueError, match=r"^frontmatter key 'description' must be a single-line value$"
    ):
        parse_skill_markdown(text)


def test_parse_rejects_multiline_title_too() -> None:
    text = "---\nname: ok\ntitle: |\n  Block Title\n---\nbody\n"
    with pytest.raises(
        ValueError, match=r"^frontmatter key 'title' must be a single-line value$"
    ):
        parse_skill_markdown(text)


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def test_render_minimal_frontmatter_omits_default_title() -> None:
    text = render_skill_markdown(
        name="make-pdf", title="Make Pdf", description="Turn docs into PDFs", body="Do it.\n"
    )
    assert text == "---\nname: make-pdf\ndescription: Turn docs into PDFs\n---\nDo it.\n"


def test_render_includes_custom_title() -> None:
    text = render_skill_markdown(
        name="make-pdf", title="PDF Maker", description="", body="b\n"
    )
    assert "title: PDF Maker\n" in text


def test_render_empty_description_has_no_trailing_space() -> None:
    text = render_skill_markdown(name="x", title="X", description="", body="b\n")
    assert "description:\n" in text
    assert "description: \n" not in text


def test_render_quotes_only_when_needed() -> None:
    quoted_desc = render_skill_markdown(
        name="x", title="X", description="note: important", body="b\n"
    )
    assert 'description: "note: important"\n' in quoted_desc

    bare = render_skill_markdown(name="x", title="X", description="plain words", body="b\n")
    assert "description: plain words\n" in bare

    edge_ws = render_skill_markdown(name="x", title="X", description=" padded ", body="b\n")
    assert 'description: " padded "\n' in edge_ws

    leading_quote = render_skill_markdown(name="x", title="X", description='"hi"', body="b\n")
    assert 'description: "\\"hi\\""\n' in leading_quote


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            dict(name="Bad Name", title="T", description="", body="b"),
            "name 'Bad Name' must be a lowercase kebab-case slug",
        ),
        (
            dict(name="a" * 81, title="T", description="", body="b"),
            f"name '{'a' * 81}' must be a lowercase kebab-case slug",
        ),
        (dict(name="ok", title="a\nb", description="", body="b"), "title must be a single line"),
        (
            dict(name="ok", title="T", description="a\nb", body="b"),
            "description must be a single line",
        ),
        (dict(name="ok", title="", description="", body="b"), "title is empty"),
        (
            dict(name="ok", title="T" * 161, description="", body="b"),
            "title is longer than 160 characters",
        ),
        (
            dict(name="ok", title="T", description="d" * 501, body="b"),
            "description is longer than 500 characters",
        ),
        (dict(name="ok", title="T", description="", body="   \n"), "body is empty"),
        (
            dict(name="ok", title="T", description="", body="x" * 20001),
            "body is longer than 20000 characters",
        ),
    ],
)
def test_render_refuses_unrenderable_content(kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=rf"^{re.escape(message)}$"):
        render_skill_markdown(**kwargs)


# ---------------------------------------------------------------------------
# the round-trip law
# ---------------------------------------------------------------------------

_ROUND_TRIP_CASES = [
    # the plain case
    ("make-pdf", "Make Pdf", "Turn a doc into a PDF", "Do the thing.\n"),
    # empty description, default title
    ("solo", "Solo", "", "body\n"),
    # custom title with a colon (forces quoting)
    ("kebab-case-name", "Title: The Sequel", "desc", "b\n"),
    # colon+space in description
    ("c", "C", "usage: run it like this", "b\n"),
    # embedded double quotes, single quotes, both
    ("q", "Q", 'he said "hi" and left', "b\n"),
    ("q", "Q", "it's got 'singles'", "b\n"),
    ("q", "Q", "\"mixed\" and 'both': yes", "b\n"),
    # quote-wrapped-looking values that must survive literally
    ("q", "Q", '"fully wrapped"', "b\n"),
    ("q", "Q", "'fully wrapped'", "b\n"),
    # backslashes, alone and against quotes
    ("bs", "Bs", 'C:\\path\\to\\thing', "b\n"),
    ("bs", "Bs", ' ends with backslash \\', "b\n"),
    ("bs", "Bs", ' \\" backslash-quote run \\\\" ', "b\n"),
    # leading/trailing whitespace in values
    ("ws", "Ws", "  padded  ", "b\n"),
    ("ws", "Ws", "\ttab-led", "b\n"),
    # body containing --- lines, code fences, fake frontmatter, blank edges
    (
        "tricky-body",
        "Tricky Body",
        "d",
        "\n---\nname: not-frontmatter\n---\n\n```md\n---\n```\ntrailing text",
    ),
    # body without trailing newline
    ("no-newline", "No Newline", "d", "no trailing newline"),
    # body with trailing blank lines
    ("blanks", "Blanks", "d", "kept\n\n\n"),
    # bounds: longest legal everything
    ("n" * 80, "T" * 160, "d" * 500, "x" * 20000),
    # description that looks like another frontmatter key
    ("sneaky", "Sneaky", "title: not-a-key", "b\n"),
    # unicode
    ("unicode", "Unicode", "émojis 🎉 and — dashes", "café ☕\n"),
]


@pytest.mark.parametrize(("name", "title", "description", "body"), _ROUND_TRIP_CASES)
def test_round_trip_law(name: str, title: str, description: str, body: str) -> None:
    rendered = render_skill_markdown(
        name=name, title=title, description=description, body=body
    )
    assert parse_skill_markdown(rendered) == ParsedSkill(
        name=name, title=title, description=description, body=body
    )


def test_round_trip_survives_a_second_cycle() -> None:
    """render∘parse must also be stable: exporting an imported file changes
    nothing, so syncing a repo back and forth never produces spurious diffs."""
    original = render_skill_markdown(
        name="stable", title="Stable: Truly", description='has "both": quirks ', body="b\n---\n"
    )
    parsed = parse_skill_markdown(original)
    again = render_skill_markdown(
        name=parsed.name,
        title=parsed.title,
        description=parsed.description,
        body=parsed.body,
    )
    assert again == original


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def quoted(value: str) -> str:
    """Frontmatter-quote a test value so names with spaces/edge cases survive
    the trip through the parser's unquoting and hit validation verbatim."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
