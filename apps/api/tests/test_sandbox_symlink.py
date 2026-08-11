"""The symlink escape: generated code cannot read a host file, but it can point
a harvestable name at one, and the harvester runs on the host as the API user."""
from pathlib import Path

from app.services.sandbox import local_exec


def test_a_symlinked_artifact_is_not_harvested(tmp_path: Path) -> None:
    secret = tmp_path / "env_secret"
    secret.write_bytes(b"OPENAI_API_KEY=sk-should-never-be-harvested")
    session = tmp_path / "session"
    session.mkdir()
    before = local_exec.snapshot(session)
    (session / "chart.png").symlink_to(secret)

    artifacts, _ = local_exec.harvest_artifacts(session, before)

    assert artifacts == (), "a symlink pointing outside the session was harvested"


def test_a_symlink_is_not_listed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"x" * 4096)
    session = tmp_path / "s2"
    session.mkdir()
    (session / "probe.txt").symlink_to(outside)

    provider = local_exec.LocalProvider(workdir=tmp_path, env={})
    from app.services.sandbox.types import SandboxHandle

    entries = provider.list_files(SandboxHandle(provider="x", external_id="s2"), "/")
    assert [e.name for e in entries] == [], "a symlink leaked into the listing"


def test_a_real_file_is_still_harvested(tmp_path: Path) -> None:
    session = tmp_path / "s3"
    session.mkdir()
    before = local_exec.snapshot(session)
    (session / "real.png").write_bytes(b"\x89PNG" + b"z" * 32)

    artifacts, _ = local_exec.harvest_artifacts(session, before)
    assert [a.kind for a in artifacts] == ["png"], "the fix broke normal harvesting"
