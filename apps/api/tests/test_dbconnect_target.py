"""dbconnect refuses a target that is the application's own host or files.

The SQL guard proves *what runs*; this proves *where it runs*. A user-supplied
DSN can name any host or path, and the two never-legitimate targets are the
application's own box — loopback, and the cloud-metadata address a network DSN
can reach — and the application's own files, most sharply the SQLite database
that holds every tenant. RFC1918 stays allowed: reaching a private VPC database
is the feature, not the threat.

`_guard_target` is a pure function over (kind, url, settings), so this drives it
directly — no database, no app fixtures. Run with `--noconftest`.
"""
from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest
from sqlalchemy.engine.url import make_url

from app.services.dbconnect.engine import DbConnectError, _guard_target


def _resolves_to(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 5432))],
    )


def _files(**overrides) -> SimpleNamespace:
    base = dict(
        database_url="sqlite:////srv/app/data/workspace.db",
        objects_dir="/srv/app/data/objects",
        sandbox_workdir="/srv/app/data/sandboxes",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# -- network engines: the API's own host and the metadata service are refused --


def test_a_postgres_host_on_loopback_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolves_to(monkeypatch, "127.0.0.1")
    with pytest.raises(DbConnectError, match="own network"):
        _guard_target("postgres", make_url("postgresql://u:p@db.local:5432/x"), _files())


def test_a_host_on_the_metadata_range_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolves_to(monkeypatch, "169.254.169.254")
    with pytest.raises(DbConnectError, match="own network"):
        _guard_target("mysql", make_url("mysql://u:p@metadata:3306/x"), _files())


def test_a_private_vpc_host_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # RFC1918 is the operator's own network — reaching it is the point.
    _resolves_to(monkeypatch, "10.0.0.5")
    _guard_target("postgres", make_url("postgresql://u:p@db.internal:5432/x"), _files())


def test_a_public_host_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolves_to(monkeypatch, "93.184.216.34")
    _guard_target("postgres", make_url("postgresql://u:p@db.example.com/x"), _files())


def test_an_unresolvable_host_is_left_for_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(*args, **kwargs):
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    # Not our error to raise: connecting will report it as the real failure.
    _guard_target("postgres", make_url("postgresql://u:p@nope.invalid/x"), _files())


# -- file engines: the application's own files are refused --


def test_the_apps_own_sqlite_database_is_refused() -> None:
    with pytest.raises(DbConnectError, match="belongs to the application"):
        _guard_target(
            "sqlite", make_url("sqlite:////srv/app/data/workspace.db"), _files()
        )


def test_a_file_inside_the_object_store_is_refused() -> None:
    with pytest.raises(DbConnectError, match="belongs to the application"):
        _guard_target(
            "duckdb", make_url("duckdb:////srv/app/data/objects/steal.duckdb"), _files()
        )


def test_a_users_own_file_elsewhere_is_allowed() -> None:
    _guard_target("sqlite", make_url("sqlite:////home/analyst/sales.db"), _files())


def test_an_in_memory_database_is_allowed() -> None:
    _guard_target("sqlite", make_url("sqlite://"), _files())


def test_a_deployment_on_postgres_protects_no_sqlite_path() -> None:
    # When the app itself does not run on SQLite there is no own-database file to
    # protect, and an unrelated local file is fine.
    settings = _files(database_url="postgresql://app:app@10.9.9.9/app")
    _guard_target("sqlite", make_url("sqlite:////srv/app/data/workspace.db"), settings)
