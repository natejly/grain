"""LaTeX projects: the starter document and the .tex-aware rules around it.

A LaTeX project is a Project with ``kind="latex"``. It is the same virtual
filesystem as a web project, written by the same fs_* tools, under the same size
limits — only the preview differs, and only two facts about the *content* differ.
Both live here: what a new one is seeded with, and the rule that its entry point
is a file the TeX engine can actually be pointed at.

The engine is a WebAssembly TeX Live that runs in the browser with the network
closed, carrying a fixed "core" package tier. That closed world is why
`UNAVAILABLE_PACKAGES` exists: a document that loads tikz cannot compile here and
no amount of retrying will change that, so the agent is told while it is writing
rather than the user discovering it in a 900-line transcript.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .store import STARTER_FILES, ProjectError, normalize_path

KIND = "latex"
WEB_KIND = "web"
KINDS = (WEB_KIND, KIND)

DEFAULT_ENTRY_PATH = "main.tex"
WEB_ENTRY_PATH = "index.tsx"
BIBLIOGRAPHY_PATH = "refs.bib"

TEX_SUFFIX = ".tex"
BIB_SUFFIX = ".bib"

# Packages we know are outside the vendored core tier. This is a shortlist of the
# ones people actually reach for first, not the complement of the tier: a package
# missing from here may still be missing from the bundle, in which case the
# compile fails with TeX's own "File `x.sty' not found". Being wrong in that
# direction is safe — being wrong the other way would refuse a document that
# compiles fine.
UNAVAILABLE_PACKAGES = frozenset(
    {
        "algorithm2e",
        "beamer",
        "biblatex",
        "biber",
        "fontspec",
        "minted",
        "pgf",
        "pgfplots",
        "siunitx",
        "booktabs",
        "tcolorbox",
        "tikz",
        "tikz-cd",
        "unicode-math",
    }
)

_LOAD_PATTERN = re.compile(
    r"\\(?:usepackage|RequirePackage|documentclass)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}"
)
_DOCUMENTCLASS_PATTERN = re.compile(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{[^}]+\}")
_COMMENT_PATTERN = re.compile(r"(?<!\\)%.*")


def is_latex_kind(kind: str) -> bool:
    return (kind or "").strip().lower() == KIND


def normalize_kind(kind: str) -> str:
    """Accept a project kind, defaulting to web, refusing anything unknown."""
    value = (kind or "").strip().lower() or WEB_KIND
    if value not in KINDS:
        raise ProjectError(f"Unknown project kind “{kind}”; use {' or '.join(KINDS)}")
    return value


def is_tex_path(path: str) -> bool:
    return path.strip().lower().endswith(TEX_SUFFIX)


def is_bib_path(path: str) -> bool:
    return path.strip().lower().endswith(BIB_SUFFIX)


def bibliography_path_for(entry_path: str) -> str:
    """The .bib that belongs beside a LaTeX entry file.

    This engine runs bibtex from the entry file's own directory, so the .bib has
    to sit next to the entry rather than at the project root — the same rule the
    starter follows, shared so `\\bibliography{refs}` keeps resolving when an
    entry is added to a project whose bibliography was deleted.
    """
    entry = entry_path or DEFAULT_ENTRY_PATH
    directory = entry[: entry.rfind("/") + 1] if "/" in entry else ""
    return directory + BIBLIOGRAPHY_PATH


def validate_entry_path(entry_path: str) -> str:
    """Normalize a LaTeX entry path and insist it names a .tex file.

    The engine is handed this path as the root file to typeset. Pointing it at
    refs.bib or a .sty produces a transcript nobody can act on, so the rule is
    enforced where the project is created rather than at compile time.
    """
    clean = normalize_path(entry_path or DEFAULT_ENTRY_PATH)
    if not is_tex_path(clean):
        raise ProjectError(
            f"A LaTeX project's entry file must end in {TEX_SUFFIX}; “{clean}” does not"
        )
    return clean


def strip_comments(source: str) -> str:
    """Drop TeX line comments so a commented-out \\usepackage is not "used"."""
    return _COMMENT_PATTERN.sub("", source)


def packages_used(source: str) -> List[str]:
    """Every package and class name the source loads, in first-seen order."""
    seen: List[str] = []
    for group in _LOAD_PATTERN.findall(strip_comments(source)):
        for raw in group.split(","):
            name = raw.strip()
            if name and name not in seen:
                seen.append(name)
    return seen


def unavailable_packages(source: str) -> List[str]:
    """The loaded packages that the offline bundle is known not to carry."""
    return [name for name in packages_used(source) if name in UNAVAILABLE_PACKAGES]


def has_documentclass(source: str) -> bool:
    return _DOCUMENTCLASS_PATTERN.search(strip_comments(source)) is not None


def seed_warnings(entry_path: str, files: Dict[str, str]) -> List[str]:
    """Advisory problems with a LaTeX project's files — never fatal.

    Creation still succeeds: half-written source is a normal state for a project
    the agent is about to keep editing. But saying nothing means the first
    compile is the first the user hears of it.
    """
    warnings: List[str] = []
    source = files.get(entry_path, "")
    if not has_documentclass(source):
        warnings.append(
            f"{entry_path} has no \\documentclass, so it will not compile on its own"
        )
    missing = sorted({name for content in files.values() for name in unavailable_packages(content)})
    if missing:
        warnings.append(
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not in the "
            "offline TeX bundle; the compile will fail with a missing-file error"
        )
    return warnings


_TEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_TEX_ESCAPE_PATTERN = re.compile("[" + re.escape("".join(_TEX_ESCAPES)) + "]")


def _tex_escape(text: str) -> str:
    """Make a project name safe to drop into \\title{...}.

    One pass, not one pass per character: successive `str.replace` calls re-read
    what the earlier ones wrote, so escaping ``\\`` to ``\\textbackslash{}`` and
    then escaping braces turns a backslash into ``\\textbackslash\\{\\}``, which
    typesets as ``\\{}``. A single substitution can never re-escape its own output.
    """
    return _TEX_ESCAPE_PATTERN.sub(lambda match: _TEX_ESCAPES[match.group()], text)


# Every package loaded here is in the offline core tier, and this exact shape —
# article + amsmath + natbib/bibtex + \label/\ref — is the one proven to compile
# in the browser engine. Changing the preamble risks reaching outside the tier,
# so keep new packages to ones the bundle is known to carry.
#
# The @TOKEN@ placeholders are substituted rather than %-formatted: % starts a
# comment in TeX, so a percent-format template would have to double every one of
# them and would break the moment someone added a comment to this document.
_STARTER_TEX = r"""\documentclass[11pt]{article}

% The TeX engine runs in your browser with no network, carrying a fixed set of
% packages. These six are in it. tikz, beamer, biblatex, siunitx and booktabs
% are not, and loading them fails the compile.
\usepackage[margin=1in]{geometry}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{graphicx}
\usepackage{natbib}
\usepackage{hyperref}

\title{@TITLE@}
\author{}
\date{\today}

\begin{document}
\maketitle

\begin{abstract}
Replace this with a sentence about what the document argues.
\end{abstract}

\section{Introduction}
\label{sec:intro}

Edit \texttt{@ENTRY@} and the preview recompiles. Text can be
\emph{emphasised}, math can be inline like $e^{i\pi} + 1 = 0$, and sources are
cited with \citep{knuth1984}.

\section{Results}
\label{sec:results}

Section~\ref{sec:intro} promised an equation, so here are two:
\begin{align}
  \int_{-\infty}^{\infty} e^{-x^{2}} \, dx &= \sqrt{\pi}, \label{eq:gauss} \\
  \sum_{n=1}^{\infty} \frac{1}{n^{2}} &= \frac{\pi^{2}}{6}.
\end{align}
Equation~\eqref{eq:gauss} is the Gaussian integral.

\bibliographystyle{plainnat}
\bibliography{@BIB@}

\end{document}
"""

_TOKEN_PATTERN = re.compile("@(?:TITLE|ENTRY|BIB)@")

_STARTER_BIB = r"""@book{knuth1984,
  author    = {Donald E. Knuth},
  title     = {The {\TeX}book},
  publisher = {Addison-Wesley},
  year      = {1984}
}
"""


def starter_files(name: str = "", entry_path: str = DEFAULT_ENTRY_PATH) -> Dict[str, str]:
    """A LaTeX project that compiles to a real PDF the moment it is created."""
    entry = entry_path or DEFAULT_ENTRY_PATH
    # The .bib sits next to the entry file, and this engine runs bibtex from the
    # entry file's own directory — so \bibliography takes the bare stem. Passing
    # the root-relative path instead ("paper/refs" for paper/main.tex) makes
    # bibtex exit 2 and the starter project fails to compile on creation.
    bib_path = bibliography_path_for(entry)
    values = {
        "@TITLE@": _tex_escape(name.strip()) or "New document",
        "@ENTRY@": _tex_escape(entry),
        "@BIB@": BIBLIOGRAPHY_PATH[: -len(".bib")],
    }
    # One pass, for the same reason as _tex_escape: substituting the tokens in
    # sequence lets a later token match text an earlier one just inserted, so a
    # project named “@BIB@” would be titled after its bibliography.
    tex = _TOKEN_PATTERN.sub(lambda match: values[match.group()], _STARTER_TEX)
    return {entry: tex, bib_path: _STARTER_BIB}


def resolve_seed(
    *,
    kind: str = WEB_KIND,
    name: str = "",
    entry_path: str = "",
    files: Optional[Dict[str, str]] = None,
) -> Tuple[str, str, Dict[str, str]]:
    """Settle what a new project of either kind is created with.

    Returns ``(kind, entry_path, files)`` ready for `store.create_project`, so a
    caller never has to branch on the kind itself — which is the whole point:
    every path that can make a project routes through here, and none of them can
    forget the .tex rule or seed a LaTeX project with a React starter.
    """
    resolved = normalize_kind(kind)
    if resolved != KIND:
        seeded = STARTER_FILES if files is None else files
        return resolved, normalize_path(entry_path or WEB_ENTRY_PATH), dict(seeded)
    entry = validate_entry_path(entry_path)
    return resolved, entry, dict(files) if files is not None else starter_files(name, entry)
