#!/usr/bin/env python3
"""Proof that the `container` sandbox driver does what ADR 0005 says it does.

ADR 0005 makes load-bearing security claims — no route out, a read-only root, no
capabilities, not root, bounded pids and memory — and `test_sandbox_local.py`
deliberately asserts none of them, because it asserts the *argv* and never starts
a daemon. That is the right trade for a unit test and it leaves the actual claims
untested: an argv can be perfect while the image is missing a package, the bind
mount is unwritable by the sandbox uid, or `--network none` turns out to leave a
resolver reachable. This script is the other half. It runs real containers
through the real driver and checks the claims from the inside.

It does not build the image. `make sandbox-image` is the build; conflating the
two would mean a proof that passes because it rebuilt around a problem.

Two design choices worth stating:

* The probes assert *positively* where they can. "the connection failed" is weak
  evidence — a timeout looks the same as a firewall, and a slow CI runner looks
  the same as a sandbox escape. So the network probe enumerates the interfaces
  (there must be exactly one, `lo`), reads the kernel routing table (there must
  be no route off-box), and requires the connect failures to carry ENETUNREACH
  rather than a timeout errno.
* Every probe returns an evidence string, and the script prints it. A gate that
  says only "OK" cannot be audited, and one nobody audits gets weakened by
  accident.

`apps/api/tests/test_sandbox_container_live.py` imports `PROBES` from here and
runs the same functions under pytest, skipping when Docker or the image is
absent. There is one implementation so the two cannot drift.

Usage:
    make sandbox-image
    python scripts/verify_sandbox_container.py [--image IMG] [--only SUBSTRING]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
# Importable without `pip install -e apps/api`, because the audience for this
# script is "somebody with Docker who wants to check the claim", which is not
# necessarily somebody with the API's virtualenv activated.
if str(REPO_ROOT / "apps" / "api") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from app.services.sandbox.container_provider import ContainerProvider  # noqa: E402
from app.services.sandbox.types import ExecResult, SandboxHandle, SandboxSpec  # noqa: E402

DEFAULT_IMAGE = os.environ.get("SANDBOX_CONTAINER_IMAGE", "jasmine-sandbox:latest")
DEFAULT_DOCKER = os.environ.get("SANDBOX_DOCKER_BINARY", "docker")

#: The environment the probes run with. Production builds this from `Settings`
#: via `policy.sandbox_env`; hard-coding the two harmless variables keeps the
#: proof runnable with no configuration at all. What `Settings` is *not* allowed
#: to leak into here is asserted by `test_sandbox_security.py`, which is the
#: right place for it — that claim needs no container.
SANDBOX_ENV: Dict[str, str] = {"JASMINE_SANDBOX": "1", "PYTHONUNBUFFERED": "1"}

# Errno numbers as *Linux* defines them, spelled out rather than read from this
# process's `errno` module. The numbers being compared were produced inside the
# container, which is always Linux; the comparison happens here, which may be
# macOS with Docker Desktop — and there ENETUNREACH is 51, not 101, and EAGAIN
# is 35, not 11. Using `errno.ENETUNREACH` would make the whole network proof
# fail on a Mac for a reason that has nothing to do with the sandbox.
EPERM = 1
EAGAIN = 11
EACCES = 13
EROFS = 30
ENETDOWN = 100
ENETUNREACH = 101
EHOSTUNREACH = 113

#: What the kernel returns when there is no route at all. A timeout is excluded
#: on purpose: `socket.timeout` carries `errno is None`, so requiring membership
#: here is what distinguishes "there is no network" from "the network was slow",
#: which is the whole difference between a proof and a coincidence.
UNREACHABLE = frozenset({ENETUNREACH, EHOSTUNREACH, ENETDOWN})

#: Written by every probe as its last line, so stray warnings on stdout (which
#: matplotlib and sklearn both emit) cannot be mistaken for the result.
RESULT_PREFIX = "RESULT "


class ProofFailure(AssertionError):
    """A claim did not hold. Subclasses AssertionError so pytest renders it as a
    plain test failure rather than an error, and so the message is the report."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProofFailure(message)


def describe(result: ExecResult) -> str:
    """A failed execution, rendered for a human reading CI logs."""
    return (
        f"    exit_code={result.exit_code} error={result.error!r} "
        f"duration={result.duration_ms}ms\n"
        f"    stdout: {result.stdout.strip()[-1200:] or '(empty)'}\n"
        f"    stderr: {result.stderr.strip()[-1200:] or '(empty)'}"
    )


# --- the harness --------------------------------------------------------


@dataclass(frozen=True)
class MountVerdict:
    """Whether the sandbox uid can write to a session directory the driver made.

    Checked once and cached, because it is a property of the driver rather than
    of any one probe, and because when it is false almost every probe fails with
    the same uninformative PermissionError. Knowing it up front turns a wall of
    red into one diagnosis.
    """

    ok: bool
    detail: str = ""


@dataclass
class Harness:
    """Somewhere to run probes: an image, a docker binary, a workdir.

    Sessions are tracked and torn down together. Each probe opens its own so a
    failure cannot contaminate the next one, and so `--only` reproduces exactly
    what CI ran.
    """

    root: Path
    image: str = DEFAULT_IMAGE
    docker: str = DEFAULT_DOCKER
    _open: List[Tuple[ContainerProvider, SandboxHandle]] = field(default_factory=list)
    _verdict: Optional[MountVerdict] = None

    def provider(self, *, memory_mb: int = 2048, pids_limit: int = 256) -> ContainerProvider:
        return ContainerProvider(
            workdir=self.root,
            env=SANDBOX_ENV,
            image=self.image,
            docker_binary=self.docker,
            memory_mb=memory_mb,
            pids_limit=pids_limit,
        )

    def open_session(
        self, *, memory_mb: int = 2048, pids_limit: int = 256
    ) -> Tuple[ContainerProvider, SandboxHandle, Path]:
        provider = self.provider(memory_mb=memory_mb, pids_limit=pids_limit)
        handle = provider.create(SandboxSpec(workspace_id="proof", network="none"))
        self._open.append((provider, handle))
        root = self.root / handle.external_id
        if not self.mount_verdict().ok:
            # The mount is broken and `test_workspace_is_writable` is already
            # failing for it. Widening the mode here means the other twelve
            # claims still get proved instead of being masked by one defect;
            # when the driver is fixed this branch never runs.
            root.chmod(0o777)
        return provider, handle, root

    def mount_verdict(self) -> MountVerdict:
        if self._verdict is None:
            self._verdict = self._probe_mount()
        return self._verdict

    def _probe_mount(self) -> MountVerdict:
        provider = self.provider()
        handle = provider.create(SandboxSpec(workspace_id="proof", network="none"))
        root = self.root / handle.external_id
        try:
            result = provider.run_code(handle, _MOUNT_PROBE, timeout=60.0)
            if result.ok and RESULT_PREFIX in result.stdout:
                return MountVerdict(True)
            stat = root.stat()
            return MountVerdict(
                False,
                "the container (uid 65534) could not write to its own bind mount.\n"
                f"    host session dir: {root} mode={stat.st_mode & 0o777:04o} "
                f"uid={stat.st_uid} gid={stat.st_gid}\n"
                "    the driver creates it with the API user's umask, so nothing the\n"
                "    sandbox writes — a chart, a cleaned CSV — can land. Fix belongs in\n"
                "    services/sandbox/local_exec.ensure_session_root.\n"
                + describe(result),
            )
        finally:
            provider.kill(handle)

    def close(self) -> None:
        for provider, handle in self._open:
            provider.kill(handle)
        self._open.clear()

    def containers_running(self) -> List[str]:
        """Container ids alive right now that this harness started.

        `probe_timeout` kills what this returns, so "from our image" is not a
        narrow enough filter: the execution host that runs this proof is also the
        host serving real sandbox executions from the same image, and killing
        those would make the proof an outage. Attribution is by bind-mount
        source, which is the only thing that distinguishes our containers from
        anyone else's — the driver names no container and sets no label. A
        container we cannot attribute is left alone.
        """
        try:
            completed = subprocess.run(
                [self.docker, "ps", "--quiet", "--filter", f"ancestor={self.image}"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        # Both spellings: `-v` is built from the resolved session path, while
        # `self.root` may still be the symlinked one (/tmp on macOS).
        prefixes = tuple({f"{self.root}{os.sep}", f"{self.root.resolve()}{os.sep}"})
        ours: List[str] = []
        for container_id in completed.stdout.split():
            try:
                inspected = subprocess.run(
                    [
                        self.docker,
                        "inspect",
                        "--format",
                        "{{range .Mounts}}{{.Source}}\n{{end}}",
                        container_id,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if inspected.returncode != 0:
                continue
            if any(line.startswith(prefixes) for line in inspected.stdout.splitlines()):
                ours.append(container_id)
        return ours


def run_json(
    provider: ContainerProvider,
    handle: SandboxHandle,
    code: str,
    *,
    timeout: float = 120.0,
) -> Tuple[ExecResult, Dict[str, Any]]:
    """Run a probe and parse the report it printed.

    The assertions live on this side rather than inside the container so that a
    failure message can carry the whole report, instead of a traceback from a
    process nobody can attach to.
    """
    result = provider.run_code(handle, code, timeout=timeout)
    require(result.ok, "the probe did not run cleanly\n" + describe(result))
    for line in reversed(result.stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            payload = json.loads(line[len(RESULT_PREFIX) :])
            assert isinstance(payload, dict)
            return result, payload
    raise ProofFailure("the probe printed no RESULT line\n" + describe(result))


# --- probe sources (run inside the container) ---------------------------

_MOUNT_PROBE = r"""
import json, os

with open("/workspace/.mount-probe", "w") as fh:
    fh.write("ok")
os.unlink("/workspace/.mount-probe")
print("RESULT " + json.dumps({"ok": True}))
"""

_HELLO = r"""
import json, os, sys

print("hello from the sandbox")
print("RESULT " + json.dumps({"python": sys.version.split()[0], "cwd": os.getcwd()}))
"""

_IDENTITY = r"""
import json, os

wanted = ("Uid", "Gid", "Groups", "CapEff", "CapPrm", "CapBnd", "CapInh", "NoNewPrivs")
status = {}
with open("/proc/self/status") as fh:
    for line in fh:
        key, _, value = line.partition(":")
        if key in wanted:
            status[key] = value.strip()
print("RESULT " + json.dumps({
    "uid": os.getuid(), "euid": os.geteuid(), "gid": os.getgid(), "status": status,
}))
"""

_NETWORK = r"""
import json, socket, time, urllib.request


def attempt(fn):
    started = time.monotonic()
    try:
        fn()
    except BaseException as exc:
        reason = getattr(exc, "reason", None)
        return {
            "raised": type(exc).__name__,
            "errno": getattr(exc, "errno", None),
            "reason": type(reason).__name__ if reason is not None else None,
            "reason_errno": getattr(reason, "errno", None),
            "message": str(exc)[:160],
            "seconds": round(time.monotonic() - started, 3),
        }
    return {"raised": None, "errno": None, "reason": None, "reason_errno": None,
            "message": "SUCCEEDED", "seconds": round(time.monotonic() - started, 3)}


def tcp():
    socket.create_connection(("1.1.1.1", 443), timeout=8).close()


def udp():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(8)
    try:
        sock.sendto(b"\x00", ("8.8.8.8", 53))
    finally:
        sock.close()


def raw():
    socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP).close()


def http():
    urllib.request.urlopen("http://1.1.1.1/", timeout=8).read(16)


routes = []
with open("/proc/net/route") as fh:
    next(fh, None)
    for line in fh:
        parts = line.split()
        if len(parts) >= 3:
            routes.append({"iface": parts[0], "destination": parts[1], "gateway": parts[2]})

print("RESULT " + json.dumps({
    "interfaces": sorted(name for _, name in socket.if_nameindex()),
    "routes": routes,
    "tcp": attempt(tcp),
    "udp": attempt(udp),
    "dns": attempt(lambda: socket.getaddrinfo("example.com", 80)),
    "http": attempt(http),
    "raw_socket": attempt(raw),
}))
"""

_FILESYSTEM = r"""
import json, os, subprocess, sysconfig


def attempt(fn):
    try:
        fn()
    except BaseException as exc:
        return {"raised": type(exc).__name__, "errno": getattr(exc, "errno", None),
                "message": str(exc)[:160]}
    return {"raised": None, "errno": None, "message": "SUCCEEDED"}


def writer(path):
    def go():
        with open(path, "w") as fh:
            fh.write("x")
        os.unlink(path)
    return go


mounts = {}
with open("/proc/mounts") as fh:
    for line in fh:
        parts = line.split()
        if len(parts) >= 4:
            mounts[parts[1]] = parts[3].split(",")

targets = [
    "/probe.txt",
    "/etc/probe.txt",
    "/usr/lib/probe.txt",
    os.path.join(sysconfig.get_paths()["purelib"], "probe.py"),
    # /var/tmp is mode 1777 in the base image, so a refusal here cannot be a
    # permission check — it can only be the read-only mount.
    "/var/tmp/probe.txt",
]

report = {
    "root_options": mounts.get("/", []),
    "workspace_options": mounts.get("/workspace", []),
    "tmp_options": mounts.get("/tmp", []),
    "readonly": {path: attempt(writer(path)) for path in targets},
    "writable": {path: attempt(writer(path))
                 for path in ("/workspace/probe.txt", "/tmp/probe.txt")},
}

# /tmp is the only writable place in the image, so it is mounted noexec:
# staging a binary there and running it has to fail.
staged = "/tmp/exec-probe.sh"
with open(staged, "w") as fh:
    fh.write("#!/bin/sh\necho staged\n")
os.chmod(staged, 0o755)
report["tmp_exec"] = attempt(lambda: subprocess.run([staged], capture_output=True))
os.unlink(staged)
print("RESULT " + json.dumps(report))
"""

_PACKAGES = r"""
import importlib, json

wanted = ["numpy", "pandas", "scipy", "sklearn", "matplotlib", "statsmodels", "sympy",
          "pyarrow", "openpyxl", "xlsxwriter", "PIL", "dateutil", "pytz", "requests",
          "bs4", "lxml", "tabulate"]
found, missing = {}, {}
for name in wanted:
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        missing[name] = str(exc)[:120]
        continue
    found[name] = str(getattr(module, "__version__", "present"))

# Importing proves the wheel is there. Fitting a model proves it works, which is
# the claim that matters for an image nobody can pip-install into. Guarded so a
# missing package still produces a report — "sklearn is not in the image" is a
# far better failure than an ImportError traceback from an unnamed probe.
slope = rvalue = None
if not {"numpy", "pandas", "scipy", "sklearn"} & set(missing):
    import numpy as np
    import pandas as pd
    from scipy import stats
    from sklearn.linear_model import LinearRegression

    frame = pd.DataFrame({"x": np.arange(50, dtype=float)})
    frame["y"] = 2.0 * frame["x"] + 1.0
    model = LinearRegression().fit(frame[["x"]], frame["y"])
    slope = float(model.coef_[0])
    rvalue = float(stats.linregress(frame["x"], frame["y"]).rvalue)
print("RESULT " + json.dumps({
    "found": found, "missing": missing, "slope": slope, "rvalue": rvalue,
}))
"""

_CHART = r"""
import json, os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

x = np.linspace(0.0, 4.0 * np.pi, 240)
figure, axes = plt.subplots(figsize=(4.0, 3.0), dpi=100)
axes.plot(x, np.sin(x))
axes.set_title("sandbox proof")
figure.savefig("chart.png")
plt.close(figure)
print("RESULT " + json.dumps({"bytes": os.path.getsize("chart.png"), "cwd": os.getcwd()}))
"""

_PERSIST_WRITE = r"""
import json

with open("carried.txt", "w") as fh:
    fh.write("persisted")
carried_global = 41
print("RESULT " + json.dumps({"wrote": True}))
"""

_PERSIST_READ = r"""
import json

with open("carried.txt") as fh:
    carried = fh.read()
try:
    carried_global
except NameError:
    leaked = False
else:
    leaked = True
print("RESULT " + json.dumps({"carried": carried, "leaked": leaked}))
"""

_FORKS = r"""
import json, os, time

children = 0
failure = None
try:
    for _ in range(300):
        pid = os.fork()
        if pid == 0:
            # The child has to stay alive or nothing accumulates. It is reaped by
            # the pid namespace when this process (container pid 1) exits.
            time.sleep(15)
            os._exit(0)
        children += 1
except OSError as exc:
    failure = {"errno": exc.errno, "message": str(exc)[:120]}
print("RESULT " + json.dumps({"children": children, "failure": failure}), flush=True)
"""

#: Printed by the memory probe before it allocates a single byte. The probe's
#: expected outcome is a container the kernel killed, which reaches this side as
#: "no RESULT line, non-zero exit" — and so does a container that never started.
#: Requiring the marker is what separates "the cap held" from "docker refused a
#: flag", which would otherwise be indistinguishable and read as a pass.
_MEMORY_MARKER = "MEMPROBE-RUNNING"

_MEMORY = r"""
import json

print("MEMPROBE-RUNNING", flush=True)
blocks = []
allocated = 0
outcome = "no-limit"
try:
    # bytearray zero-fills, so every byte is resident rather than reserved. The
    # ceiling is deliberately low: if the cgroup limit is not working, this must
    # not be the thing that takes the CI runner down.
    while allocated < 1024 * 1024 * 1024:
        blocks.append(bytearray(32 * 1024 * 1024))
        allocated += 32 * 1024 * 1024
except MemoryError:
    outcome = "MemoryError"
print("RESULT " + json.dumps({"allocated_mb": allocated // (1024 * 1024),
                              "outcome": outcome}), flush=True)
"""

_SPIN = "while True:\n    pass\n"


# --- probes -------------------------------------------------------------


def probe_workspace_writable(harness: Harness) -> str:
    verdict = harness.mount_verdict()
    require(verdict.ok, verdict.detail)
    return "the container's uid can write to the bind-mounted session directory"


def probe_hello(harness: Harness) -> str:
    provider, handle, _ = harness.open_session()
    result, payload = run_json(provider, handle, _HELLO, timeout=90.0)
    require(
        "hello from the sandbox" in result.stdout,
        "stdout did not come back from the container\n" + describe(result),
    )
    require(result.exit_code == 0, f"expected exit 0, got {result.exit_code}")
    require(payload["cwd"] == "/workspace", f"cwd is {payload['cwd']}, expected /workspace")
    return f"python {payload['python']} ran in /workspace and its stdout came back"


def probe_identity(harness: Harness) -> str:
    provider, handle, _ = harness.open_session()
    _, payload = run_json(provider, handle, _IDENTITY, timeout=90.0)
    status = payload["status"]
    require(payload["uid"] != 0 and payload["euid"] != 0, f"running as root: {payload}")
    require(payload["uid"] == 65534, f"expected uid 65534 (nobody), got {payload['uid']}")
    for name in ("CapEff", "CapPrm", "CapBnd", "CapInh"):
        require(
            set(status.get(name, "x")) == {"0"},
            f"{name}={status.get(name)!r} — --cap-drop ALL did not take effect",
        )
    require(
        status.get("NoNewPrivs") == "1",
        f"NoNewPrivs={status.get('NoNewPrivs')!r} — a setuid binary could regain caps",
    )
    return (
        f"uid={payload['uid']} (not root), capability sets all zero, NoNewPrivs=1"
    )


def probe_network(harness: Harness) -> str:
    provider, handle, _ = harness.open_session()
    _, payload = run_json(provider, handle, _NETWORK, timeout=120.0)

    # Positive first: --network none means a namespace with loopback and nothing
    # else. Asserting the interface list is the claim; the failed connections
    # below are only corroboration.
    interfaces = payload["interfaces"]
    require(
        interfaces == ["lo"],
        f"expected exactly one interface (lo), found {interfaces} — the container "
        "is attached to a network",
    )
    routes = payload["routes"]
    require(
        all(route["iface"] == "lo" for route in routes),
        f"the routing table has an off-box route: {routes}",
    )
    require(
        not any(route["destination"] == "00000000" for route in routes),
        f"the container has a default route: {routes}",
    )

    tcp = payload["tcp"]
    require(
        tcp["errno"] in UNREACHABLE,
        "a TCP connect to 1.1.1.1:443 did not fail with an unreachable-network "
        f"errno (a timeout would prove nothing): {tcp}",
    )
    require(tcp["seconds"] < 5.0, f"the TCP failure took {tcp['seconds']}s — that is a hang")

    udp = payload["udp"]
    require(
        udp["errno"] in UNREACHABLE,
        f"a UDP datagram to 8.8.8.8:53 was not refused by the kernel: {udp}",
    )

    dns = payload["dns"]
    require(
        dns["raised"] == "gaierror",
        f"DNS resolution of example.com did not fail with gaierror: {dns}",
    )
    require(dns["seconds"] < 20.0, f"DNS took {dns['seconds']}s — that is a timeout, not a refusal")

    http = payload["http"]
    require(
        http["raised"] == "URLError" and http["reason_errno"] in UNREACHABLE,
        f"an HTTP request to a literal IP did not fail unreachably: {http}",
    )

    raw = payload["raw_socket"]
    require(
        raw["errno"] == EPERM,
        f"a raw socket was not refused — CAP_NET_RAW is still present: {raw}",
    )

    return (
        "interfaces=['lo'], no routes off-box, TCP/UDP ENETUNREACH, DNS gaierror, "
        "HTTP unreachable, raw socket EPERM"
    )


def probe_filesystem(harness: Harness) -> str:
    provider, handle, _ = harness.open_session()
    _, payload = run_json(provider, handle, _FILESYSTEM, timeout=90.0)

    require(
        "ro" in payload["root_options"],
        f"/ is not mounted read-only: {payload['root_options']}",
    )
    allowed = {EROFS, EACCES}
    for path, outcome in payload["readonly"].items():
        require(
            outcome["errno"] in allowed,
            f"writing {path} was not refused: {outcome}",
        )
    # The one target whose refusal can only be the mount: /var/tmp is 1777, so a
    # permission check would have let this through.
    var_tmp = payload["readonly"]["/var/tmp/probe.txt"]
    require(
        var_tmp["errno"] == EROFS,
        f"/var/tmp (mode 1777) refused with {var_tmp} rather than EROFS — the "
        "refusal is a permission check, not a read-only filesystem",
    )
    for path, outcome in payload["writable"].items():
        require(outcome["raised"] is None, f"{path} should be writable but was not: {outcome}")
    for flag in ("noexec", "nosuid"):
        require(flag in payload["tmp_options"], f"/tmp is missing {flag}: {payload['tmp_options']}")
    require(
        payload["tmp_exec"]["errno"] == EACCES,
        f"a binary staged in /tmp executed: {payload['tmp_exec']}",
    )
    return "/ is ro (EROFS even on 1777 /var/tmp), /workspace and /tmp are rw, /tmp is noexec"


def probe_packages(harness: Harness) -> str:
    provider, handle, _ = harness.open_session()
    _, payload = run_json(provider, handle, _PACKAGES, timeout=180.0)
    found, missing = payload["found"], payload["missing"]
    # The five the product cannot work without. The rest of the Dockerfile's list
    # is reported rather than required, so that adding or renaming an optional
    # library is not a CI failure in a file this proof does not own.
    for name in ("numpy", "pandas", "scipy", "sklearn", "matplotlib"):
        require(name not in missing, f"{name} does not import in the image: {missing.get(name)}")
    require(payload["slope"] is not None, "the packages imported but the regression never ran")
    require(abs(payload["slope"] - 2.0) < 1e-6, f"sklearn fitted slope {payload['slope']}, not 2")
    require(abs(payload["rvalue"] - 1.0) < 1e-9, f"scipy r={payload['rvalue']}, not 1")
    note = f"; NOT IN IMAGE: {sorted(missing)}" if missing else ""
    return (
        f"numpy {found['numpy']}, pandas {found['pandas']}, scipy {found['scipy']}, "
        f"sklearn {found['sklearn']} import and fit a regression correctly"
        f" ({len(found)} of {len(found) + len(missing)} listed packages present){note}"
    )


def probe_chart_artifact(harness: Harness) -> str:
    provider, handle, root = harness.open_session()
    result, payload = run_json(provider, handle, _CHART, timeout=180.0)
    require(
        (root / "chart.png").is_file(),
        "matplotlib reported a file but nothing landed on the host bind mount",
    )
    pngs = [artifact for artifact in result.artifacts if artifact.kind == "png"]
    require(
        len(pngs) == 1,
        f"expected exactly one harvested PNG, got {[a.kind for a in result.artifacts]}",
    )
    png = pngs[0]
    require(png.is_main, "the only artifact was not marked is_main")
    require(png.mime == "image/png", f"mime is {png.mime}")
    raw = base64.b64decode(png.data, validate=True)
    require(raw[:8] == b"\x89PNG\r\n\x1a\n", "the artifact payload is not a PNG")
    require(raw[12:16] == b"IHDR", "the PNG has no IHDR chunk")
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    require(
        (width, height) == (400, 300),
        f"the figure came out {width}x{height}, expected 400x300",
    )
    require(len(raw) == payload["bytes"], "the harvested bytes differ from what was written")
    return (
        f"matplotlib wrote a {width}x{height} PNG ({len(raw)} bytes) inside the "
        "container and it came back as a harvested artifact"
    )


def probe_persistence(harness: Harness) -> str:
    provider, handle, _ = harness.open_session()
    run_json(provider, handle, _PERSIST_WRITE, timeout=90.0)
    _, payload = run_json(provider, handle, _PERSIST_READ, timeout=90.0)
    require(
        payload["carried"] == "persisted",
        f"a file written by the first execution did not survive: {payload}",
    )
    require(
        payload["leaked"] is False,
        "a global from the first execution was still defined in the second — the "
        "driver is not the throwaway-container model ADR 0005 describes",
    )
    return "files persist across executions in one session; interpreter state does not"


def probe_fork_bomb(harness: Harness) -> str:
    limit = 48
    provider, handle, _ = harness.open_session(pids_limit=limit)
    started = time.monotonic()
    _, payload = run_json(provider, handle, _FORKS, timeout=120.0)
    elapsed = time.monotonic() - started
    failure = payload["failure"]
    require(
        failure is not None,
        f"300 forks all succeeded under --pids-limit {limit}: {payload}",
    )
    require(
        failure["errno"] == EAGAIN,
        f"forking stopped for the wrong reason: {failure}",
    )
    require(
        payload["children"] < 300,
        f"the pid cap did not bind: {payload['children']} children",
    )
    require(elapsed < 90.0, f"the contained fork bomb took {elapsed:.0f}s to return")
    return (
        f"fork() failed with EAGAIN after {payload['children']} children under "
        f"--pids-limit {limit}; the run returned in {elapsed:.1f}s"
    )


def probe_memory_cap(harness: Harness) -> str:
    cap_mb = 256
    provider, handle, _ = harness.open_session(memory_mb=cap_mb)
    result = provider.run_code(handle, _MEMORY, timeout=120.0)
    # Before interpreting the outcome, establish that there was one. The pass
    # this probe is looking for is "no RESULT line and a non-zero exit", which is
    # also what a rejected docker flag (125), an unreachable daemon or a missing
    # image produce — so without the marker every one of those reads as proof
    # that the cap held. The marker is printed before the first byte is
    # allocated, so seeing it means the container started and ran this code.
    require(
        _MEMORY_MARKER in result.stdout,
        "the memory probe never started inside the container, so nothing about "
        "the memory cap was tested\n" + describe(result),
    )
    # A wall-clock timeout also ends in a SIGKILLed docker CLI and a non-zero
    # exit; `error` is the only thing that tells it apart from an OOM kill.
    require(
        not result.error,
        f"the memory probe did not finish ({result.error}), so the cap was never "
        "shown to bind\n" + describe(result),
    )
    reached = 0
    outcome = ""
    for line in reversed(result.stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            payload = json.loads(line[len(RESULT_PREFIX) :])
            reached, outcome = payload["allocated_mb"], payload["outcome"]
            break
    require(
        outcome != "no-limit",
        f"the process allocated {reached} MB against a {cap_mb} MB limit without "
        "being stopped\n" + describe(result),
    )
    if outcome == "MemoryError":
        require(
            reached < 1024,
            f"MemoryError only at {reached} MB, above the {cap_mb} MB cap\n" + describe(result),
        )
        return f"a runaway allocation raised MemoryError at {reached} MB (cap {cap_mb} MB)"
    # No RESULT line: the kernel OOM killer got there first, which is the
    # expected outcome and the stronger one. It has to be *that* exit code and
    # not merely a non-zero one: a container the daemon refused to start (125),
    # or one whose interpreter never ran (127), also produces no RESULT line, and
    # accepting any failure here would report "the kernel killed it" for a probe
    # that never allocated a byte.
    require(
        result.exit_code == 137,
        f"the memory hog neither reported MemoryError nor was SIGKILLed; docker "
        f"exited {result.exit_code}, so the probe did not run rather than being "
        "contained\n" + describe(result),
    )
    return (
        f"a runaway allocation against a {cap_mb} MB cap was killed by the kernel "
        f"(exit {result.exit_code}); the host was unaffected"
    )


def probe_timeout(harness: Harness) -> str:
    provider, handle, _ = harness.open_session()
    before = set(harness.containers_running())
    started = time.monotonic()
    result = provider.run_code(handle, _SPIN, timeout=5.0)
    elapsed = time.monotonic() - started
    require(
        "timed out" in result.error,
        "an infinite loop did not come back as a timeout result\n" + describe(result),
    )
    require(not result.ok, "a timed-out execution reported ok")
    # The driver gives docker 15s past the inner budget to tear down, so ~20s is
    # the expected wall clock. Anything near the 120s default means the kill did
    # not land.
    require(elapsed < 60.0, f"the timeout took {elapsed:.0f}s to return")

    leaked = [cid for cid in harness.containers_running() if cid not in before]
    note = ""
    if leaked:
        # Not fatal here: the claim under test is "kills and returns a result".
        # But it is worth shouting about, because a container the daemon still
        # owns after the CLI was killed is exactly the leaked compute ADR 0005
        # says this design does not have.
        note = f"  [WARNING] {len(leaked)} container(s) still running after the timeout: {leaked}"
        for cid in leaked:
            subprocess.run([harness.docker, "kill", cid], capture_output=True, check=False)
    return f"an infinite loop was killed and returned a result in {elapsed:.1f}s{note}"


@dataclass(frozen=True)
class Probe:
    """One claim, its check, and the sentence printed when it holds."""

    name: str
    claim: str
    run: Callable[[Harness], str]


PROBES: Tuple[Probe, ...] = (
    Probe("workspace-writable", "the session bind mount is writable by the sandbox uid",
          probe_workspace_writable),
    Probe("hello", "a script runs and its stdout comes back", probe_hello),
    Probe("identity", "not root, no capabilities, no new privileges", probe_identity),
    Probe("network", "there is no network, positively", probe_network),
    Probe("filesystem", "read-only outside /workspace and /tmp", probe_filesystem),
    Probe("packages", "the pre-baked scientific stack is real", probe_packages),
    Probe("chart-artifact", "matplotlib produces a PNG that is harvested", probe_chart_artifact),
    Probe("persistence", "files persist between executions, interpreter state does not",
          probe_persistence),
    Probe("fork-bomb", "a fork bomb is contained by --pids-limit", probe_fork_bomb),
    Probe("memory-cap", "a memory hog is contained by --memory", probe_memory_cap),
    Probe("timeout", "a wall-clock timeout kills and returns a result", probe_timeout),
)


# --- runner -------------------------------------------------------------


def _preflight(image: str, docker: str) -> Optional[str]:
    """Why we cannot even start, or None. Distinct from a failed proof: "Docker
    is not installed" and "the sandbox is not isolated" must not exit alike."""
    if shutil.which(docker) is None:
        return f"no container runtime: '{docker}' is not on PATH"
    try:
        completed = subprocess.run(
            [docker, "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not reach the container runtime: {exc}"
    if completed.returncode != 0:
        return f"the image '{image}' is not built. Run: make sandbox-image"
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--docker", default=DEFAULT_DOCKER)
    parser.add_argument(
        "--workdir",
        default="",
        help="host directory for session mounts (default: a temp directory)",
    )
    parser.add_argument("--only", default="", help="run probes whose name contains this")
    parser.add_argument("--list", action="store_true", help="print the probes and exit")
    args = parser.parse_args(argv)

    if args.list:
        for probe in PROBES:
            print(f"{probe.name:20s} {probe.claim}")
        return 0

    blocked = _preflight(args.image, args.docker)
    if blocked is not None:
        print(f"cannot verify: {blocked}", file=sys.stderr)
        return 2

    selected = [p for p in PROBES if args.only in p.name]
    if not selected:
        print(f"no probe matches --only {args.only!r}", file=sys.stderr)
        return 2

    root = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="sandbox-proof-"))
    root.mkdir(parents=True, exist_ok=True)
    harness = Harness(root=root, image=args.image, docker=args.docker)

    print(f"sandbox container proof — image {args.image}, workdir {root}")
    failures: List[Tuple[str, str]] = []
    proved: List[Tuple[str, str]] = []
    try:
        for probe in selected:
            started = time.monotonic()
            try:
                evidence = probe.run(harness)
            except Exception as exc:
                # Deliberately broad: a probe that raises anything at all has
                # failed to establish its claim, and a proof tool that crashes
                # halfway through is worse than one that reports which claim it
                # could not check.
                detail = f"{type(exc).__name__}: {exc}"
                failures.append((probe.name, detail))
                print(f"  FAIL  {probe.name:20s} ({time.monotonic() - started:5.1f}s)")
                print("        " + detail.replace("\n", "\n        "))
            else:
                proved.append((probe.name, evidence))
                print(f"  PASS  {probe.name:20s} ({time.monotonic() - started:5.1f}s)  {evidence}")
    finally:
        harness.close()
        if not args.workdir:
            shutil.rmtree(root, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED — {len(failures)} of {len(selected)} claims did not hold:")
        for name, detail in failures:
            print(f"  {name}: {detail.splitlines()[0]}")
        return 1
    print(f"PROVED — {len(proved)} claims held against a real container:")
    for name, evidence in proved:
        print(f"  {name}: {evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
