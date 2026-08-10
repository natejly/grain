"""Bibliographies for LaTeX projects: read the .bib, cross-reference it, extend it.

The compile pipeline already runs bibtex8 in the browser, so a .bib in the project
filesystem works end to end. What it cannot do is tell anyone *why* the second
pass failed, because nothing on this side understands the file's contents. This
module supplies that understanding: a tolerant BibTeX reader, a cross-reference
between what the .tex files cite and what the .bib files define, and one way to
append an entry.

Two decisions are worth stating up front.

The parser is written here rather than pulled in. `bibtexparser` is the obvious
dependency, but its 1.x reader is a regex pass that gives up on the exact inputs
this has to survive (a truncated final entry, an unparseable field) and its 2.x
line is a different API again — and the whole job is ~200 lines of scanning. A
dependency whose failure mode is "raises on a file the user is mid-edit" is worse
than no dependency.

Cite keys are matched case-insensitively, because bibtex is: it downcases keys
before comparing them, so `\\cite{Knuth1984}` really does resolve `@book{knuth1984`.
Matching case-sensitively here would invent "undefined citation" reports for
documents that compile, and that category is the one people act on.
"""
from __future__ import annotations

import bisect
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Tuple

from sqlalchemy.orm import Session

from ...models import Project, ProjectFile
from . import latex, store
from .store import ProjectError

# A tool result is clipped at 4k characters, so a 300-entry bibliography would
# arrive at the model as a severed JSON document. Cap the listing instead and say
# so, which keeps the validation counts — the part worth reading — intact.
MAX_ENTRIES_LISTED = 40
MAX_FIELDS_PER_ENTRY = 40
MAX_KEY_CHARS = 200
MAX_FIELD_VALUE_CHARS = 4000


# ---------------------------------------------------------------------------
# The parser


@dataclass
class BibEntry:
    """One @type{key, ...} record. `source` is filled in by the project layer."""

    entry_type: str
    key: str
    fields: Dict[str, str] = field(default_factory=dict)
    line: int = 0
    source: str = ""
    # The file ended before this entry's closing brace. Its fields are whatever
    # survived; it is still *defined*, so citations of it are not reported as
    # undefined on top of the parse problem that already names it.
    truncated: bool = False


@dataclass
class ParseResult:
    entries: List[BibEntry] = field(default_factory=list)
    macros: Dict[str, str] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)
    # Names the record — an entry, an @comment, an @string — that ran to the end
    # of the file without its closing delimiter, or "" if the file is closed.
    # Everything after that point is *inside* the open brace, which is why
    # nothing can be appended to the file until it is closed. A truncated *entry*
    # is only one of the ways this happens: an unterminated @comment leaves no
    # entry behind at all, so `truncated` on the entries cannot answer it.
    unterminated: str = ""


_ENTRY_TYPE_PATTERN = re.compile(r"[A-Za-z]+")
_FIELD_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_+:.\-]*")
# A bare value runs until whatever could end or continue it: a field separator,
# the entry's closing delimiter, whitespace, or a concatenation operator.
_BARE_VALUE_PATTERN = re.compile(r"[^,\s#}\)]+")
_WHITESPACE_RUN = re.compile(r"\s+")
_CLOSERS = {"{": "}", "(": ")"}


def _collapse(value: str) -> str:
    """Fold the line breaks a wrapped field is written across into single spaces."""
    return _WHITESPACE_RUN.sub(" ", value).strip()


class _Scanner:
    """A single left-to-right pass over one .bib file.

    Kept as a class only because every read advances a shared cursor; there is no
    state here that outlives `run`.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.result = ParseResult()
        # Every newline's offset, so a line number is a bisect rather than a
        # recount from byte zero. `str.count` is O(file) per call and one is
        # needed per entry, which made parsing a large .bib quadratic: a
        # 280 KB bibliography took ~220 ms, and a project may hold twenty of them.
        self._newlines: List[int] = []
        offset = text.find("\n")
        while offset >= 0:
            self._newlines.append(offset)
            offset = text.find("\n", offset + 1)

    # -- primitives ---------------------------------------------------------

    def _line_at(self, index: int) -> int:
        # The line is one more than the number of newlines strictly before it.
        return bisect.bisect_left(self._newlines, index) + 1

    def _unterminated(self, what: str, line: int) -> None:
        """Record a record that ran off the end of the file inside an open brace."""
        if not self.result.unterminated:
            self.result.unterminated = f"{what} (line {line})"
        self.result.problems.append(
            f"line {line}: {what} is never closed — the file ends mid-entry, so bibtex "
            "stops reading here and anything appended is swallowed by the open brace"
        )

    def _skip_space(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def _at(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _skip_group(self, closer: str) -> bool:
        """Run to the end of the group whose opening delimiter was consumed.

        Returns False when the file ended first — every byte from the opening
        delimiter onward is then inside a brace nobody closed, and the caller has
        to say so rather than treating the record as merely finished.
        """
        # A brace-delimited group is already one level deep; a paren-delimited one
        # closes on a `)` that no inner brace is protecting.
        depth = 1 if closer == "}" else 0
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if char == "\\":
                self.pos += 1
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0 and closer == "}":
                    return True
            elif char == closer and depth == 0:
                return True
        return False

    def _read_braced(self) -> str:
        """`{The {\\TeX}book}` -> `The {\\TeX}book`: the outer layer only is dropped."""
        start = self.pos + 1
        depth = 0
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "\\":
                # \{ and \} are literal characters, not nesting. Skipping the
                # escaped character also steps safely over \\.
                self.pos += 2
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    value = self.text[start : self.pos]
                    self.pos += 1
                    return value
            self.pos += 1
        return self.text[start:]  # unterminated: hand back what there is

    def _read_quoted(self) -> str:
        self.pos += 1
        start = self.pos
        depth = 0
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "\\":
                self.pos += 2
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth = max(depth - 1, 0)
            elif char == '"' and depth == 0:
                # A quote inside braces is data, not a terminator.
                value = self.text[start : self.pos]
                self.pos += 1
                return value
            self.pos += 1
        return self.text[start:]

    def _read_value(self) -> str:
        """One field value: braced, quoted, or bare, joined across `#`."""
        parts: List[str] = []
        while True:
            self._skip_space()
            char = self._at()
            if char == "{":
                parts.append(self._read_braced())
            elif char == '"':
                parts.append(self._read_quoted())
            else:
                match = _BARE_VALUE_PATTERN.match(self.text, self.pos)
                if match is None:
                    break
                self.pos = match.end()
                token = match.group()
                # A bare token is a @string macro if one is defined, and
                # otherwise itself — a year, or a macro this file never set.
                parts.append(self.result.macros.get(token.lower(), token))
            self._skip_space()
            if self._at() == "#":
                self.pos += 1
                continue
            break
        return "".join(parts)

    # -- records ------------------------------------------------------------

    def _read_macros(self, closer: str, line: int) -> None:
        """@string{acm = "Association for Computing Machinery"}"""
        while True:
            self._skip_space()
            if not self._at():
                self._unterminated("@string", line)
                return
            if self._at() == closer:
                self.pos += 1
                return
            match = _FIELD_NAME_PATTERN.match(self.text, self.pos)
            if match is None:
                if not self._skip_group(closer):
                    self._unterminated("@string", line)
                return
            self.pos = match.end()
            self._skip_space()
            if self._at() != "=":
                if not self._skip_group(closer):
                    self._unterminated("@string", line)
                return
            self.pos += 1
            self.result.macros[match.group().lower()] = _collapse(self._read_value())
            self._skip_space()
            if self._at() == ",":
                self.pos += 1

    def _read_entry(self, entry_type: str, closer: str, start: int) -> None:
        line = self._line_at(start)
        self._skip_space()
        key_start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in (",", closer):
            self.pos += 1
        key = self.text[key_start : self.pos].strip()
        entry = BibEntry(entry_type=entry_type, key=key, line=line)

        if not entry.key:
            self.result.problems.append(
                f"line {line}: @{entry_type} has no citation key, so nothing can cite it"
            )
            if not self._skip_group(closer):
                self._unterminated(f"@{entry_type}", line)
            return
        if "{" in key or "}" in key:
            # bibtex reads a key up to the first comma or brace, so `@misc{a{b}c,`
            # defines `a{b` and throws the fields away. Say so: without this the
            # entry looks fine in the listing and every \cite of it is reported
            # undefined with no explanation.
            self.result.problems.append(
                f"line {line}: the citation key “{key}” contains a brace, so bibtex "
                "stops reading the key there and the rest of the entry is lost"
            )
        if self.pos >= len(self.text):
            self._finish(entry, truncated=True)
            return
        if self.text[self.pos] == closer:
            self.pos += 1  # a fieldless entry, e.g. @misc{key}
            self.result.entries.append(entry)
            return
        self.pos += 1  # the comma after the key

        while True:
            self._skip_space()
            if self.pos >= len(self.text):
                self._finish(entry, truncated=True)
                return
            if self.text[self.pos] == closer:
                self.pos += 1
                break
            match = _FIELD_NAME_PATTERN.match(self.text, self.pos)
            if match is None:
                self.result.problems.append(
                    f"line {self._line_at(self.pos)}: could not read a field name in "
                    f"“{entry.key}”; the rest of the entry was skipped"
                )
                if not self._skip_group(closer):
                    self._unterminated(f"“{entry.key}”", entry.line)
                break
            self.pos = match.end()
            name = match.group().lower()
            self._skip_space()
            if self._at() != "=":
                self.result.problems.append(
                    f"line {self._line_at(self.pos)}: “{name}” in “{entry.key}” has no "
                    "value; the rest of the entry was skipped"
                )
                if not self._skip_group(closer):
                    self._unterminated(f"“{entry.key}”", entry.line)
                break
            self.pos += 1
            entry.fields[name] = _collapse(self._read_value())
            self._skip_space()
            if self._at() == ",":
                self.pos += 1  # trailing commas are fine: the next turn sees the closer
                continue
            if self._at() == closer:
                self.pos += 1
                break
            if self.pos >= len(self.text):
                self._finish(entry, truncated=True)
                return
            self.result.problems.append(
                f"line {self._line_at(self.pos)}: unexpected “{self._at()}” after "
                f"“{name}” in “{entry.key}”; the rest of the entry was skipped"
            )
            if not self._skip_group(closer):
                self._unterminated(f"“{entry.key}”", entry.line)
            break
        self.result.entries.append(entry)

    def _finish(self, entry: BibEntry, *, truncated: bool) -> None:
        entry.truncated = truncated
        self._unterminated(f"“{entry.key}”", entry.line)
        self.result.entries.append(entry)

    # -- driver -------------------------------------------------------------

    def run(self) -> ParseResult:
        while True:
            start = self.text.find("@", self.pos)
            if start < 0:
                break
            # Everything between records is a comment as far as bibtex is
            # concerned — including % lines — so it is simply skipped.
            self.pos = start + 1
            match = _ENTRY_TYPE_PATTERN.match(self.text, self.pos)
            if match is None:
                continue
            self.pos = match.end()
            kind = match.group().lower()
            self._skip_space()
            opener = self._at()
            if opener not in _CLOSERS:
                continue
            self.pos += 1
            closer = _CLOSERS[opener]
            if kind in ("comment", "preamble"):
                if not self._skip_group(closer):
                    self._unterminated(f"@{kind}", self._line_at(start))
            elif kind == "string":
                self._read_macros(closer, self._line_at(start))
            else:
                self._read_entry(kind, closer, start)
        return self.result


def parse_bibtex(source: str) -> ParseResult:
    """Read a .bib file, never raising: malformed input becomes a problem string."""
    return _Scanner(source).run()


# ---------------------------------------------------------------------------
# What each entry type owes

# The standard bibtex styles' required fields. Each inner tuple is a set of
# alternatives — a @book needs an author *or* an editor, not both. Types absent
# from this table (@misc, and anything a custom style invents) require nothing,
# which is deliberate: reporting a field as missing when no style asks for it
# would train people to ignore the report.
REQUIRED_FIELDS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "article": (("author",), ("title",), ("journal",), ("year",)),
    "book": (("author", "editor"), ("title",), ("publisher",), ("year",)),
    "booklet": (("title",),),
    "inbook": (("author", "editor"), ("title",), ("chapter", "pages"), ("publisher",), ("year",)),
    "incollection": (("author",), ("title",), ("booktitle",), ("publisher",), ("year",)),
    "inproceedings": (("author",), ("title",), ("booktitle",), ("year",)),
    "conference": (("author",), ("title",), ("booktitle",), ("year",)),
    "manual": (("title",),),
    "mastersthesis": (("author",), ("title",), ("school",), ("year",)),
    "phdthesis": (("author",), ("title",), ("school",), ("year",)),
    "proceedings": (("title",), ("year",)),
    "techreport": (("author",), ("title",), ("institution",), ("year",)),
    "unpublished": (("author",), ("title",), ("note",)),
}


def missing_required_fields(entry: BibEntry) -> List[str]:
    """Which required field groups this entry satisfies none of, e.g. "author or editor"."""
    missing: List[str] = []
    for alternatives in REQUIRED_FIELDS.get(entry.entry_type, ()):
        if not any(entry.fields.get(name, "").strip() for name in alternatives):
            missing.append(" or ".join(alternatives))
    return missing


# ---------------------------------------------------------------------------
# What the document cites

# Covers \cite and the natbib/biblatex family around it — \citep, \citet,
# \citealp, \autocite, \parencite, \nocite — plus their starred and capitalised
# spellings, and the up-to-two optional arguments natbib takes.
_CITE_PATTERN = re.compile(r"\\[A-Za-z]*[Cc]ite[A-Za-z]*\*?\s*(?:\[[^\]]*\]\s*)*\{([^}]*)\}")

# \nocite{*} pulls every entry into the bibliography, which makes "uncited"
# meaningless for that document rather than merely empty.
NOCITE_ALL = "*"


def cited_keys(source: str) -> List[str]:
    """Every key cited by a .tex source, in first-seen order, comments excluded."""
    seen: List[str] = []
    for group in _CITE_PATTERN.findall(latex.strip_comments(source)):
        for raw in group.split(","):
            key = raw.strip()
            # `\newcommand{\mycite}[1]{\cite{#1}}` is a definition, not a
            # citation: `#` is bibtex's concatenation operator and cannot appear
            # in a key, so `#1` could only ever be reported as undefined — a
            # guaranteed false alarm in the one category people act on.
            if key and "#" not in key and key not in seen:
                seen.append(key)
    return seen


# ---------------------------------------------------------------------------
# The report


@dataclass(frozen=True)
class UndefinedCitation:
    key: str
    cited_in: List[str]


@dataclass(frozen=True)
class DefinedEntry:
    key: str
    defined_in: str


@dataclass(frozen=True)
class DuplicateKey:
    key: str
    defined_in: List[str]
    count: int


@dataclass(frozen=True)
class IncompleteEntry:
    key: str
    entry_type: str
    defined_in: str
    missing: List[str]


# Which lists give way first when the payload has to fit a budget, least
# important to most: undefined citations are last because they are the category
# that actually breaks the build.
_TRIM_ORDER = (
    # The file lists give way first: `fs_list` already reports them, and a
    # document split into 180 \input sections would otherwise blow the whole
    # budget on paths before a single finding was serialized.
    "tex_files",
    "bib_files",
    "entries",
    "defined_but_uncited",
    "parse_problems",
    "missing_required_fields",
    "duplicate_keys",
    "cited_but_undefined",
)


@dataclass(frozen=True)
class BibliographyReport:
    project_id: str
    project_name: str
    bib_files: List[str]
    tex_files: List[str]
    entries: List[BibEntry]
    undefined: List[UndefinedCitation]
    uncited: List[DefinedEntry]
    duplicates: List[DuplicateKey]
    incomplete: List[IncompleteEntry]
    problems: List[str]

    @property
    def ok(self) -> bool:
        """Uncited entries are dead weight, not breakage, so they do not count here."""
        return not (self.undefined or self.duplicates or self.incomplete or self.problems)

    def as_dict(self, *, max_chars: int = 0) -> Dict[str, Any]:
        """The report as JSON-ready data, optionally trimmed to fit `max_chars`.

        A tool result is clipped at a fixed length, and a clipped JSON document
        is not JSON — a 400-entry bibliography would reach the model as an
        unparseable fragment. So the payload trims itself instead, dropping the
        lists in `_TRIM_ORDER` until it fits. `counts` always carries the true
        sizes, and `truncated` names whichever lists were shortened, so a trimmed
        report still says exactly how much it is not showing.
        """
        lists: Dict[str, List[Any]] = {
            "bib_files": list(self.bib_files),
            "tex_files": list(self.tex_files),
            "entries": [
                {
                    "key": entry.key,
                    "type": entry.entry_type,
                    "defined_in": entry.source,
                    "author": entry.fields.get("author", ""),
                    "title": entry.fields.get("title", ""),
                    "year": entry.fields.get("year", ""),
                }
                for entry in self.entries[:MAX_ENTRIES_LISTED]
            ],
            "cited_but_undefined": [asdict(item) for item in self.undefined],
            "defined_but_uncited": [asdict(item) for item in self.uncited],
            "duplicate_keys": [asdict(item) for item in self.duplicates],
            "missing_required_fields": [asdict(item) for item in self.incomplete],
            "parse_problems": list(self.problems),
        }
        counts = {
            "bib_files": len(self.bib_files),
            "tex_files": len(self.tex_files),
            "entries": len(self.entries),
            "cited_but_undefined": len(self.undefined),
            "defined_but_uncited": len(self.uncited),
            "duplicate_keys": len(self.duplicates),
            "missing_required_fields": len(self.incomplete),
            "parse_problems": len(self.problems),
        }
        payload: Dict[str, Any] = {
            "project": self.project_name,
            "counts": counts,
            "ok": self.ok,
            **lists,
        }
        for name in _TRIM_ORDER:
            # Halved rather than popped one at a time: a 500-entry bibliography
            # would otherwise re-serialize the whole payload 490 times.
            while max_chars and payload[name] and len(json.dumps(payload)) > max_chars:
                payload[name] = payload[name][: len(payload[name]) // 2]
        payload["truncated"] = [
            name for name in lists if len(payload[name]) < counts[name]
        ]
        return payload


# ---------------------------------------------------------------------------
# Against a project


def _require_latex(project: Project) -> None:
    if not latex.is_latex_kind(project.kind):
        raise ProjectError(
            f"“{project.name}” is a {project.kind} project; bibliographies belong to "
            "LaTeX projects"
        )


def _files_with_suffix(db: Session, *, project: Project, suffix: str) -> List[ProjectFile]:
    # Scoping rides on `project`, which every caller resolves through the
    # workspace-scoped store lookups — there is no second filesystem here.
    return [
        file
        for file in store.list_files(db, project_id=project.id)
        if file.path.lower().endswith(suffix)
    ]


def bib_files(db: Session, *, project: Project) -> List[ProjectFile]:
    return _files_with_suffix(db, project=project, suffix=latex.BIB_SUFFIX)


def tex_files(db: Session, *, project: Project) -> List[ProjectFile]:
    return _files_with_suffix(db, project=project, suffix=latex.TEX_SUFFIX)


def load_entries(db: Session, *, project: Project) -> Tuple[List[BibEntry], List[str]]:
    """Every entry across every .bib in the project, plus the parse problems."""
    entries: List[BibEntry] = []
    problems: List[str] = []
    for file in bib_files(db, project=project):
        parsed = parse_bibtex(file.content)
        for entry in parsed.entries:
            entry.source = file.path
            entries.append(entry)
        problems.extend(f"{file.path}, {problem}" for problem in parsed.problems)
    return entries, problems


def validate_project(db: Session, *, project: Project) -> BibliographyReport:
    """Cross-reference the project's citations against its bibliography."""
    _require_latex(project)
    entries, problems = load_entries(db, project=project)

    defined: Dict[str, List[BibEntry]] = {}
    for entry in entries:
        defined.setdefault(entry.key.lower(), []).append(entry)

    citations: Dict[str, List[str]] = {}
    # Keys are compared folded, but an undefined one is reported as the author
    # typed it — "knuth1984" is not a string they can search their source for.
    spelling: Dict[str, str] = {}
    cite_all = False
    tex = tex_files(db, project=project)
    for file in tex:
        for key in cited_keys(file.content):
            if key == NOCITE_ALL:
                cite_all = True
                continue
            spelling.setdefault(key.lower(), key)
            citations.setdefault(key.lower(), []).append(file.path)

    undefined = [
        UndefinedCitation(key=spelling[folded], cited_in=paths)
        for folded, paths in citations.items()
        if folded not in defined
    ]
    uncited = (
        []
        if cite_all
        else [
            DefinedEntry(key=group[0].key, defined_in=group[0].source)
            for folded, group in defined.items()
            if folded not in citations
        ]
    )
    duplicates = [
        DuplicateKey(
            key=group[0].key,
            defined_in=sorted({entry.source for entry in group}),
            count=len(group),
        )
        for group in defined.values()
        if len(group) > 1
    ]
    incomplete = [
        IncompleteEntry(
            key=entry.key,
            entry_type=entry.entry_type,
            defined_in=entry.source,
            missing=missing,
        )
        for entry in entries
        if (missing := missing_required_fields(entry))
    ]
    return BibliographyReport(
        project_id=project.id,
        project_name=project.name,
        bib_files=[file.path for file in bib_files(db, project=project)],
        tex_files=[file.path for file in tex],
        entries=entries,
        undefined=undefined,
        uncited=uncited,
        duplicates=duplicates,
        incomplete=incomplete,
        problems=problems,
    )


# ---------------------------------------------------------------------------
# Adding an entry

_KEY_PATTERN = re.compile(r'^[^\s{}(),@\\"#%~]+$')


def _balanced(value: str) -> bool:
    """Are this value's braces balanced *the way bibtex counts them*?

    bibtex's brace counting is mechanical — it never treats `\\{` as an escape —
    so a value containing one leaves the entry open and swallows every entry
    after it in the file. Honouring the backslash here would accept exactly the
    values that break the compile this module exists to explain.
    """
    depth = 0
    for char in value:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _ends_in_a_lone_backslash(value: str) -> bool:
    """`See appendix\\` renders as `{See appendix\\}` — the brace is escaped away.

    An odd run of trailing backslashes swallows the closing brace of the field,
    and with it the rest of the entry: the file we just wrote no longer parses.
    An even run is `\\\\`, a legitimate TeX line break, and is left alone.
    """
    return (len(value) - len(value.rstrip("\\"))) % 2 == 1


def render_entry(entry_type: str, key: str, fields: Dict[str, str]) -> str:
    """The exact BibTeX text for one entry — the single source of what gets written.

    `plan_add_entry` builds both the preview and the file content from this one
    string, so the approval card cannot drift from what lands on disk.
    """
    kind = (entry_type or "").strip().lstrip("@").lower()
    if not _ENTRY_TYPE_PATTERN.fullmatch(kind):
        raise ProjectError(
            f"“{entry_type}” is not an entry type; use a word like article, book or "
            "inproceedings"
        )
    clean_key = (key or "").strip()
    if not clean_key:
        raise ProjectError("An entry needs a citation key — the string \\cite{...} will use")
    if len(clean_key) > MAX_KEY_CHARS:
        raise ProjectError(f"Citation key is longer than {MAX_KEY_CHARS} characters")
    if not _KEY_PATTERN.match(clean_key):
        raise ProjectError(
            f"“{clean_key}” cannot be a citation key: it contains whitespace or one of "
            "the characters bibtex uses as syntax"
        )

    cleaned: Dict[str, str] = {}
    for name, value in fields.items():
        label = (name or "").strip().lower()
        if not _FIELD_NAME_PATTERN.fullmatch(label):
            raise ProjectError(f"“{name}” is not a usable field name")
        text = _collapse(str(value))
        if not text:
            continue  # an empty field is noise in the file, not an error
        if len(text) > MAX_FIELD_VALUE_CHARS:
            raise ProjectError(
                f"The {label} field is longer than the {MAX_FIELD_VALUE_CHARS}-character limit"
            )
        if not _balanced(text):
            raise ProjectError(
                f"The {label} field has unbalanced braces, which would break every "
                "entry after it"
            )
        if _ends_in_a_lone_backslash(text):
            raise ProjectError(
                f"The {label} field ends in a lone backslash, which would escape the "
                "field's closing brace and swallow the rest of the entry"
            )
        cleaned[label] = text
    if not cleaned:
        raise ProjectError("An entry needs at least one field, such as title")
    if len(cleaned) > MAX_FIELDS_PER_ENTRY:
        raise ProjectError(f"An entry holds at most {MAX_FIELDS_PER_ENTRY} fields")

    width = max(len(name) for name in cleaned)
    lines = [f"@{kind}{{{clean_key},"]
    body = [f"  {name.ljust(width)} = {{{value}}}" for name, value in cleaned.items()]
    lines.append(",\n".join(body))
    lines.append("}")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class AddPlan:
    """What `add_entry` will do, computed without doing it."""

    path: str
    key: str
    entry: str
    content: str
    created: bool


def default_bib_path(db: Session, *, project: Project) -> str:
    """Where an entry goes when the caller does not say.

    One .bib is the overwhelming case and naming it every time is friction; none
    means the project lost its starter and the file beside the entry .tex is where
    `\\bibliography{refs}` already points. Several is genuinely ambiguous, and
    guessing would silently put a citation in a file the document does not read.
    """
    existing = bib_files(db, project=project)
    if len(existing) == 1:
        return existing[0].path
    if not existing:
        return latex.bibliography_path_for(project.entry_path)
    names = ", ".join(file.path for file in existing)
    raise ProjectError(f"{project.name} has several bibliographies ({names}); name one with `path`")


def plan_add_entry(
    db: Session,
    *,
    project: Project,
    entry_type: str,
    key: str,
    fields: Dict[str, str],
    path: str = "",
) -> AddPlan:
    _require_latex(project)
    target = store.normalize_path(path) if (path or "").strip() else default_bib_path(
        db, project=project
    )
    if not latex.is_bib_path(target):
        raise ProjectError(f"“{target}” is not a {latex.BIB_SUFFIX} file")

    rendered = render_entry(entry_type, key, fields)
    clean_key = key.strip()
    existing, _problems = load_entries(db, project=project)
    for entry in existing:
        # Across every .bib, not just the target: bibtex reads them all, and a key
        # defined twice is an error wherever the two halves live.
        if entry.key.lower() == clean_key.lower():
            raise ProjectError(
                f"“{entry.key}” is already defined in {entry.source}; use a different "
                "key, or edit that entry with fs_edit"
            )
    file = store.find_file(db, project_id=project.id, path=target)
    before = file.content if file is not None else ""
    # Appending after a brace nobody closed does not add an entry: the open brace
    # swallows the new one whole, so the write would appear to succeed and the key
    # would still be undefined. Asked of the file rather than of its entries,
    # because an unterminated @comment or @string leaves no entry to inspect and
    # swallows an append exactly the same way.
    tail = parse_bibtex(before).unterminated
    if tail:
        raise ProjectError(
            f"{target} ends mid-entry inside {tail}; close it first, or anything "
            "appended is swallowed by its open brace"
        )
    content = rendered if not before.strip() else before.rstrip("\n") + "\n\n" + rendered
    return AddPlan(
        path=target, key=clean_key, entry=rendered, content=content, created=file is None
    )


def add_entry(
    db: Session,
    *,
    workspace_id: str,
    project: Project,
    entry_type: str,
    key: str,
    fields: Dict[str, str],
    path: str = "",
) -> Tuple[ProjectFile, AddPlan]:
    """Append one entry to a project bibliography, refusing a key already in use."""
    plan = plan_add_entry(
        db, project=project, entry_type=entry_type, key=key, fields=fields, path=path
    )
    # Through the ordinary file write, so the size limits, path validation and
    # updated_at bump all apply exactly as they do to any other project file.
    file, _created = store.write_file(
        db, workspace_id=workspace_id, project=project, path=plan.path, content=plan.content
    )
    return file, plan


def describe_plan(project: Project, plan: AddPlan) -> str:
    """The approval card's body: where it lands, and the literal text appended."""
    destination = f"{project.name}/{plan.path}"
    opening = (
        f"Create {destination} with this entry:"
        if plan.created
        else f"Append this entry to {destination}:"
    )
    return f"{opening}\n\n{plan.entry}"


def entry_payload(entry: BibEntry) -> Dict[str, Any]:
    return {
        "key": entry.key,
        "entry_type": entry.entry_type,
        "fields": dict(entry.fields),
        "defined_in": entry.source,
        "line": entry.line,
        "truncated": entry.truncated,
    }


def list_entries(
    db: Session, *, project: Project
) -> Tuple[List[BibEntry], List[str], List[str]]:
    """Entries, the .bib paths they came from, and any parse problems."""
    _require_latex(project)
    entries, problems = load_entries(db, project=project)
    return entries, [file.path for file in bib_files(db, project=project)], problems

