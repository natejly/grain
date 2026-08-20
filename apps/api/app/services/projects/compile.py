"""Server-side LaTeX compilation via a sandboxed TeX Live container.

The contract mirrors the browser's wasmtex path: caller sends a file map and
an entry path, gets back a status, log, and (on success) the PDF bytes.  The
difference is that this runs a full TeX Live installation, so packages like
tikz, beamer, and biblatex work.

Security is the same model as the Python sandbox (ADR 0005):
    docker run --rm --network none --read-only --cap-drop ALL
    --security-opt no-new-privileges --user 65534:65534
    -no-shell-escape (TeX flag, not Docker)
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional

from .store import MAX_FILE_BYTES, MAX_FILES_PER_PROJECT, MAX_PROJECT_BYTES

log = logging.getLogger(__name__)

LatexEngine = Literal["pdftex", "xetex"]

MOUNT = "/workspace"
SANDBOX_USER = "65534:65534"

_ENGINE_FLAGS: Dict[LatexEngine, List[str]] = {
    "pdftex": ["-pdf"],
    "xetex": ["-xelatex"],
}


@dataclass
class CompileResult:
    status: Literal["ok", "failed"]
    message: str
    log: str
    pdf_base64: Optional[str] = None


class CompileError(Exception):
    pass


def _validate_files(
    files: List[Dict[str, str]], entry_path: str
) -> Dict[str, str]:
    """Validate and normalise the file map; raise CompileError on violations."""
    if not files:
        raise CompileError("No files provided")
    if len(files) > MAX_FILES_PER_PROJECT:
        raise CompileError(
            f"Too many files ({len(files)}); limit is {MAX_FILES_PER_PROJECT}"
        )
    file_map: Dict[str, str] = {}
    total = 0
    for f in files:
        path = f.get("path", "").strip()
        content = f.get("content", "")
        if not path:
            raise CompileError("File entry missing 'path'")
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise CompileError(
                f"{path} exceeds the {MAX_FILE_BYTES:,}-byte per-file limit"
            )
        total += size
        file_map[path] = content
    if total > MAX_PROJECT_BYTES:
        raise CompileError(
            f"Project exceeds the {MAX_PROJECT_BYTES:,}-byte limit"
        )
    if entry_path not in file_map:
        raise CompileError(f'Entry file "{entry_path}" is not in the files')
    if not entry_path.lower().endswith(".tex"):
        raise CompileError(f'"{entry_path}" is not a .tex file')
    return file_map


def _cli_env() -> Dict[str, str]:
    """Environment for the docker CLI (not the container)."""
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    for key in (
        "HOME",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "DOCKER_CERT_PATH",
        "DOCKER_TLS_VERIFY",
        "XDG_RUNTIME_DIR",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _docker_binary() -> str:
    return shutil.which("docker") or "docker"


def image_available(image: str) -> bool:
    """True if the Docker image exists locally."""
    docker = _docker_binary()
    try:
        result = subprocess.run(
            [docker, "image", "inspect", image],
            capture_output=True,
            env=_cli_env(),
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def compile_latex(
    files: List[Dict[str, str]],
    entry_path: str,
    *,
    engine: LatexEngine = "pdftex",
    image: str = "grain-latex:latest",
    timeout_seconds: int = 60,
    memory_mb: int = 2048,
    cpus: float = 2.0,
    pids_limit: int = 256,
    provider: Literal["container", "subprocess"] = "container",
) -> CompileResult:
    """Compile a LaTeX project and return the result."""
    file_map = _validate_files(files, entry_path)

    if provider == "subprocess":
        return _compile_subprocess(file_map, entry_path, engine, timeout_seconds)

    return _compile_container(
        file_map,
        entry_path,
        engine=engine,
        image=image,
        timeout_seconds=timeout_seconds,
        memory_mb=memory_mb,
        cpus=cpus,
        pids_limit=pids_limit,
    )


def _write_files(tmpdir: Path, file_map: Dict[str, str]) -> None:
    """Stage the project files into a temp directory."""
    for path, content in file_map.items():
        dest = tmpdir / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def _read_pdf(tmpdir: Path, entry_path: str) -> Optional[str]:
    """Read the compiled PDF and return base64-encoded content."""
    pdf_name = entry_path.rsplit(".", 1)[0] + ".pdf"
    pdf_path = tmpdir / pdf_name
    if pdf_path.is_file():
        return base64.b64encode(pdf_path.read_bytes()).decode("ascii")
    for candidate in tmpdir.rglob("*.pdf"):
        return base64.b64encode(candidate.read_bytes()).decode("ascii")
    return None


def _latexmk_argv(entry_path: str, engine: LatexEngine) -> List[str]:
    """Build the latexmk command line."""
    argv = ["latexmk"]
    argv.extend(_ENGINE_FLAGS[engine])
    argv.extend([
        "-interaction=nonstopmode",
        "-no-shell-escape",
        "-halt-on-error",
        entry_path,
    ])
    return argv


def _read_log(tmpdir: Path, entry_path: str) -> str:
    """Read the TeX log file if it exists."""
    log_name = entry_path.rsplit(".", 1)[0] + ".log"
    log_path = tmpdir / log_name
    if log_path.is_file():
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    return ""


def _compile_container(
    file_map: Dict[str, str],
    entry_path: str,
    *,
    engine: LatexEngine,
    image: str,
    timeout_seconds: int,
    memory_mb: int,
    cpus: float,
    pids_limit: int,
) -> CompileResult:
    """Run latexmk inside a Docker container with full isolation."""
    docker = _docker_binary()
    tmpdir = Path(tempfile.mkdtemp(prefix="grain-latex-"))
    try:
        os.chmod(tmpdir, 0o777)
        _write_files(tmpdir, file_map)

        name = f"grain-latex-{uuid.uuid4().hex[:12]}"
        inner_argv = _latexmk_argv(entry_path, engine)

        argv: List[str] = [
            docker, "run", "--rm",
            "--name", name,
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            "--user", SANDBOX_USER,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(pids_limit),
            "--memory", f"{memory_mb}m",
            "--memory-swap", f"{memory_mb}m",
            "--cpus", str(cpus),
            "-v", f"{tmpdir}:{MOUNT}:rw",
            "-w", MOUNT,
            image,
            *inner_argv,
        ]

        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout_seconds + 15,
                env=_cli_env(),
            )
        except subprocess.TimeoutExpired:
            _kill_container(docker, name)
            tex_log = _read_log(tmpdir, entry_path)
            return CompileResult(
                status="failed",
                message=f"Compile timed out after {timeout_seconds} seconds.",
                log=tex_log,
            )

        tex_log = _read_log(tmpdir, entry_path)
        combined_log = tex_log or (result.stdout + "\n" + result.stderr).strip()

        if result.returncode == 0:
            pdf_b64 = _read_pdf(tmpdir, entry_path)
            if pdf_b64:
                return CompileResult(
                    status="ok",
                    message="Compiled successfully.",
                    log=combined_log,
                    pdf_base64=pdf_b64,
                )
            return CompileResult(
                status="failed",
                message="latexmk exited 0 but no PDF was produced.",
                log=combined_log,
            )

        return CompileResult(
            status="failed",
            message=_summarise_error(combined_log, entry_path),
            log=combined_log,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _compile_subprocess(
    file_map: Dict[str, str],
    entry_path: str,
    engine: LatexEngine,
    timeout_seconds: int,
) -> CompileResult:
    """Run latexmk directly on the host — tests and CI only."""
    latexmk = shutil.which("latexmk")
    if not latexmk:
        return CompileResult(
            status="failed",
            message="latexmk is not installed on this host.",
            log="",
        )
    tmpdir = Path(tempfile.mkdtemp(prefix="grain-latex-"))
    try:
        _write_files(tmpdir, file_map)
        argv = _latexmk_argv(entry_path, engine)

        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout_seconds,
                cwd=str(tmpdir),
            )
        except subprocess.TimeoutExpired:
            tex_log = _read_log(tmpdir, entry_path)
            return CompileResult(
                status="failed",
                message=f"Compile timed out after {timeout_seconds} seconds.",
                log=tex_log,
            )

        tex_log = _read_log(tmpdir, entry_path)
        combined_log = tex_log or (result.stdout + "\n" + result.stderr).strip()

        if result.returncode == 0:
            pdf_b64 = _read_pdf(tmpdir, entry_path)
            if pdf_b64:
                return CompileResult(
                    status="ok",
                    message="Compiled successfully.",
                    log=combined_log,
                    pdf_base64=pdf_b64,
                )
            return CompileResult(
                status="failed",
                message="latexmk exited 0 but no PDF was produced.",
                log=combined_log,
            )

        return CompileResult(
            status="failed",
            message=_summarise_error(combined_log, entry_path),
            log=combined_log,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _kill_container(docker: str, name: str) -> None:
    try:
        subprocess.run(
            [docker, "kill", name],
            capture_output=True,
            timeout=10,
            env=_cli_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _summarise_error(log_text: str, entry_path: str) -> str:
    """Extract the first actionable TeX error from the log."""
    for line in log_text.splitlines():
        if line.startswith("! "):
            return line[2:].strip()
        if "Fatal error" in line:
            return line.strip()
    return "Compile failed. See the log for details."
