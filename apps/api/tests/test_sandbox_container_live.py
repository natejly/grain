"""The container driver's ADR 0005 claims, checked against a real container.

`test_sandbox_local.py` asserts the argv the driver builds and explicitly never
starts a daemon, because a test that needs Docker is a test that does not run.
That reasoning is sound and it leaves a hole: an argv can be perfect while the
image is missing a package, the bind mount is unwritable by uid 65534, or
`--network none` leaves something reachable. These tests close it — and they
close it in the way that survives, by skipping cleanly when there is no runtime
instead of failing a laptop that has none. Wherever Docker and the image exist
(CI, an execution host, a developer who ran `make sandbox-image`) they run
without being asked.

The probes are not written here. They live in `scripts/verify_sandbox_container.py`
so that the standalone proof and this test file cannot drift into two different
claims about the same driver; this module is the pytest wrapper around that one
implementation. The script is loaded by path rather than imported, because
`scripts/` is not a package and making it one to satisfy an import would be the
tail wagging the dog.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

import pytest

IMAGE = os.environ.get("SANDBOX_CONTAINER_IMAGE", "grain-sandbox:latest")
DOCKER = os.environ.get("SANDBOX_DOCKER_BINARY", "docker")

#: Set wherever a skip would be a lie — CI sets it, two steps after building the
#: image. A skipped test is a green test, so this is what stops the gate
#: reporting success on the day the image tag drifts or the daemon fails to come
#: up. Unset by default, so a laptop with no Docker still skips.
REQUIRED = os.environ.get("SANDBOX_PROOF_REQUIRED", "").strip().lower() not in ("", "0", "false")

_VERIFY_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "verify_sandbox_container.py"


def _load_probes() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_sandbox_proof", _VERIFY_SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken checkout
        raise RuntimeError(f"cannot load the sandbox proof from {_VERIFY_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _image_available() -> bool:
    """Whether a container runtime holds the sandbox image.

    Runs at collection, so it has to be cheap on the common path: `which` short-
    circuits before any subprocess on the machines that have no runtime at all,
    which is every laptop in this project.

    The timeout is short because this is on the critical path of the *main* test
    gate, not just of this file: a docker CLI installed against a daemon that is
    down, starting, or pointing at a dead remote context blocks here on every
    `pytest apps/api/tests` anyone runs. CI inspects an image it built one step
    earlier, so the answer there is immediate; nothing legitimate needs half a
    minute, and `SANDBOX_PROOF_REQUIRED` below is what stops a wrong answer from
    passing quietly.
    """
    if shutil.which(DOCKER) is None:
        return False
    try:
        completed = subprocess.run(
            [DOCKER, "image", "inspect", IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _mount_round_trips() -> bool:
    """Whether this runtime can bind-mount the directory the probes will use.

    Every session is a bind mount, so a runtime that cannot share the test's temp
    directory proves nothing: Docker does not fail such a mount, it silently
    presents an *empty* directory, and all eleven probes then fail with
    `can't open file '/workspace/.grain_exec.py'` — a message that reads like a
    driver bug and is not one.

    This is a real condition on developer machines. Colima and Lima share only
    `$HOME` by default, while pytest's temp root follows `$TMPDIR` to
    `/var/folders/...`, which is outside it. Probed with the same `mkdtemp` the
    fixture's temp root comes from, because the answer depends on *which* path is
    being shared.

    The modes below are load-bearing, and their absence is why this used to fire
    on Linux CI — where, per the paragraph above, it should never fire at all.
    `mkdtemp` creates 0700 owned by the calling user; the container runs as uid
    65534. On Docker Desktop the bind mount goes through a VM that flattens
    ownership, so the probe reads its own file and passes. On native Linux the
    ownership is real, 65534 cannot traverse a 0700 directory owned by someone
    else, and `cat` fails — which this function then reported as "the runtime
    cannot share this path".

    That conflation is the bug: a probe meant to ask whether the RUNTIME shares a
    directory was actually asking whether an unprivileged user could read one, and
    under SANDBOX_PROOF_REQUIRED it turned that false negative into a collection
    error that failed the whole job. Widening to 0755/0644 is what the real driver
    does for a session root (`local_exec.ensure_session_root`), so the probe now
    tests the same thing the product does. A failure after this means the mount
    genuinely did not come through.
    """
    probe_root = Path(tempfile.mkdtemp(prefix="sandbox-mount-probe-"))
    try:
        probe_root.chmod(0o755)
        sentinel = probe_root / "sentinel"
        sentinel.write_text("ok", encoding="utf-8")
        sentinel.chmod(0o644)
        completed = subprocess.run(
            [DOCKER, "run", "--rm", "-v", f"{probe_root}:/probe", IMAGE,
             "cat", "/probe/sentinel"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)
    return completed.returncode == 0 and completed.stdout.strip() == "ok"


_AVAILABLE = _image_available()
# Only worth asking once the image is there; it costs a container start.
_MOUNTS = _AVAILABLE and _mount_round_trips()

# Refusing to collect rather than skipping is what gives `REQUIRED` teeth:
# `_image_available` answers "no" for a wedged daemon or a timed-out `docker
# image inspect` exactly as readily as for a laptop with no Docker, and every one
# of those otherwise exits 0 having proved nothing. A collection error is red and
# carries the reason.
if REQUIRED and not _AVAILABLE:
    raise RuntimeError(
        f"SANDBOX_PROOF_REQUIRED is set, but '{IMAGE}' is not inspectable by "
        f"'{DOCKER}'. Refusing to skip the container proof silently: either the "
        "image was not built or the runtime is not answering."
    )
if REQUIRED and not _MOUNTS:
    raise RuntimeError(
        f"SANDBOX_PROOF_REQUIRED is set, but '{DOCKER}' could not bind-mount a "
        f"{tempfile.gettempdir()} directory into '{IMAGE}' — the mount came back "
        "empty. Every probe would fail for the runtime's file sharing rather than "
        "for anything about the driver, so this refuses instead."
    )

pytestmark = pytest.mark.skipif(
    not (_AVAILABLE and _MOUNTS),
    reason=(
        f"no container runtime holding '{IMAGE}' that can bind-mount "
        f"{tempfile.gettempdir()} (need {DOCKER} on PATH, `make sandbox-image`, "
        "and a runtime that shares the temp root — Colima/Lima share only $HOME; "
        "run scripts/verify_sandbox_container.py --workdir ~/somewhere instead)"
    ),
)

proof = _load_probes()


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """One workdir and one set of sessions for the whole module.

    Module-scoped because each probe still opens its own session — the sharing
    here is the temp root and the one-off check of whether the bind mount is
    writable, which costs a container start and is a property of the driver
    rather than of any single probe.
    """
    box = proof.Harness(
        root=tmp_path_factory.mktemp("sandbox-proof"), image=IMAGE, docker=DOCKER
    )
    try:
        yield box
    finally:
        box.close()


@pytest.mark.parametrize(
    "probe", proof.PROBES if (_AVAILABLE and _MOUNTS) else [], ids=lambda p: str(p.name)
)
def test_container_sandbox_claim(probe: Any, harness: Any) -> None:
    """One claim from ADR 0005, against a real container.

    The assertion is inside the probe: it raises `ProofFailure`, an
    `AssertionError` whose message carries the container's own report, so a
    failure in CI reads as evidence rather than as a diff of two booleans.
    """
    evidence = probe.run(harness)
    assert evidence, f"{probe.name} proved nothing"
    # Printed so `-s`/`-rA` output is an audit trail of what the container was
    # actually observed to do, not a row of green dots.
    print(f"{probe.name}: {evidence}")
