"""Tests for the server-side LaTeX compile service.

Unit tests use no Docker — they exercise validation, argument building, and the
subprocess fallback with a fake latexmk (or verify the missing-binary path).
The optional integration test under @pytest.mark.latex needs the real image.
"""
from __future__ import annotations

import shutil

import pytest

from app.services.projects.compile import (
    CompileError,
    _latexmk_argv,
    _validate_files,
    compile_latex,
    image_available,
)

MINIMAL_TEX = r"\documentclass{article}\begin{document}Hello\end{document}"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_files_rejects_empty():
    with pytest.raises(CompileError, match="No files"):
        _validate_files([], "main.tex")


def test_validate_files_rejects_missing_entry():
    files = [{"path": "other.tex", "content": MINIMAL_TEX}]
    with pytest.raises(CompileError, match="not in the files"):
        _validate_files(files, "main.tex")


def test_validate_files_rejects_non_tex_entry():
    files = [{"path": "refs.bib", "content": ""}]
    with pytest.raises(CompileError, match="not a .tex file"):
        _validate_files(files, "refs.bib")


def test_validate_files_rejects_oversized_file():
    big = "x" * (256 * 1024 + 1)
    files = [{"path": "main.tex", "content": big}]
    with pytest.raises(CompileError, match="per-file limit"):
        _validate_files(files, "main.tex")


def test_validate_files_rejects_too_many_files():
    files = [{"path": f"f{i}.tex", "content": "x"} for i in range(201)]
    files.append({"path": "main.tex", "content": MINIMAL_TEX})
    with pytest.raises(CompileError, match="Too many files"):
        _validate_files(files, "main.tex")


def test_validate_files_accepts_valid_input():
    files = [{"path": "main.tex", "content": MINIMAL_TEX}]
    result = _validate_files(files, "main.tex")
    assert result == {"main.tex": MINIMAL_TEX}


# ---------------------------------------------------------------------------
# latexmk argv
# ---------------------------------------------------------------------------


def test_latexmk_argv_pdftex():
    argv = _latexmk_argv("main.tex", "pdftex")
    assert "latexmk" in argv
    assert "-pdf" in argv
    assert "-no-shell-escape" in argv
    assert "-interaction=nonstopmode" in argv
    assert "main.tex" in argv
    assert "-xelatex" not in argv


def test_latexmk_argv_xetex():
    argv = _latexmk_argv("paper.tex", "xetex")
    assert "-xelatex" in argv
    assert "-pdf" not in argv
    assert "paper.tex" in argv


# ---------------------------------------------------------------------------
# Subprocess provider (no Docker needed)
# ---------------------------------------------------------------------------


def test_compile_subprocess_missing_latexmk(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    result = compile_latex(
        [{"path": "main.tex", "content": MINIMAL_TEX}],
        "main.tex",
        provider="subprocess",
    )
    assert result.status == "failed"
    assert "not installed" in result.message


# ---------------------------------------------------------------------------
# Container provider validation
# ---------------------------------------------------------------------------


def test_compile_container_validation_runs_first():
    """Validation errors are raised before Docker is touched."""
    with pytest.raises(CompileError, match="No files"):
        compile_latex([], "main.tex", provider="container")


# ---------------------------------------------------------------------------
# Optional integration test — needs `make latex-image`
# ---------------------------------------------------------------------------


@pytest.mark.latex
@pytest.mark.skipif(
    not image_available("grain-latex:latest"),
    reason="requires grain-latex Docker image (make latex-image)",
)
def test_fullpage_and_tikz_compile():
    """Packages outside the old core tier compile with full TeX Live."""
    source = r"""\documentclass{article}
\usepackage{fullpage}
\usepackage{tikz}
\begin{document}
\begin{tikzpicture}
  \draw (0,0) -- (1,1);
\end{tikzpicture}
\end{document}
"""
    result = compile_latex(
        [{"path": "main.tex", "content": source}],
        "main.tex",
        provider="container",
    )
    assert result.status == "ok", f"Expected ok, got: {result.message}"
    assert result.pdf_base64 is not None
