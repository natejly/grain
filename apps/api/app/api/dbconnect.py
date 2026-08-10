from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.engine.url import URL
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..config import get_settings, project_root
from ..database import get_db
from ..models import DbConnection
from ..services.audit import record_audit
from ..services.crypto import EncryptionNotConfiguredError, encrypt_secret
from ..services.dbconnect import (
    SUPPORTED_ENGINES,
    DbConnectError,
    build_url,
    check_connection,
    describe,
    dispose,
    redacted_dsn,
)

router = APIRouter(prefix="/api/db", tags=["database"])

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,79}$")

# Engines whose DSN names a host the server dials; the rest name a local file.
NETWORK_ENGINES = ("postgres", "mysql")


class DbConnectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    engine: Literal["postgres", "mysql", "sqlite", "duckdb"] = "postgres"
    # A full SQLAlchemy URL (or a filesystem path for sqlite/duckdb). Carries the
    # password, so it goes one way: in on create, never back out.
    dsn: str = Field(min_length=1, max_length=2000)
    read_only: bool = True


class DbConnectionOut(BaseModel):
    id: str
    name: str
    engine: str
    read_only: bool
    status: str
    last_error: str
    # The stored DSN with its password replaced, e.g. postgresql://app:***@db:5432/sales.
    dsn_summary: str
    created_at: datetime


class DbColumnOut(BaseModel):
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False


class DbForeignKeyOut(BaseModel):
    columns: List[str] = Field(default_factory=list)
    references: str = ""
    referred_columns: List[str] = Field(default_factory=list)


class DbTableOut(BaseModel):
    name: str
    schema_name: str = ""
    # Large databases come back as names only; the client re-requests one table.
    columns_loaded: bool = False
    columns: List[DbColumnOut] = Field(default_factory=list)
    columns_omitted: int = 0
    foreign_keys: List[DbForeignKeyOut] = Field(default_factory=list)


class DbSchemaOut(BaseModel):
    connection_id: str
    connection: str
    engine: str
    table_count: int
    tables: List[DbTableOut] = Field(default_factory=list)
    tables_omitted: int = 0
    note: str = ""


# Cloud metadata services hand out credentials to anything that can reach them,
# and a DSN is an address this server dials on the user's behalf — the same
# footgun mcp.py guards against for remote MCP URLs.
_BLOCKED_HOSTNAMES = {"metadata.google.internal", "metadata", "instance-data"}
_BLOCKED_IPS = {ipaddress.ip_address("fd00:ec2::254")}


def _blocked_host(host: str) -> bool:
    bare = host.strip().strip("[]").lower()
    if not bare:
        return False
    if bare in _BLOCKED_HOSTNAMES:
        return True
    try:
        address = ipaddress.ip_address(bare)
    except ValueError:
        # A name we cannot classify without DNS; the driver's own timeout is the
        # backstop. Resolving here would make create() slow and flaky.
        return False
    return address in _BLOCKED_IPS or address.is_link_local or address.is_multicast


def _own_database_path() -> Optional[Path]:
    url = get_settings().database_url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix) :]
    if not raw or raw == ":memory:":
        return None
    return _resolve_local(raw)


def _resolve_local(raw: str) -> Path:
    # Settings anchor relative sqlite paths to the repo root, so a DSN written
    # the same way has to be resolved the same way to be compared with one.
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_root() / path
    try:
        return path.resolve()
    except OSError:
        return path


def _normalize_dsn(engine: str, dsn: str) -> str:
    """Canonicalise a bare filesystem path into an unambiguous absolute URL.

    `sqlite:///x.db` is cwd-relative and `sqlite:////x.db` absolute, so a bare
    `/data/x.db` has to be resolved here — stored as typed it would reopen
    against whatever directory the server happens to run from.
    """
    raw = dsn.strip()
    if engine in NETWORK_ENGINES or "://" in raw:
        return raw
    return f"{engine}:///{_resolve_local(raw)}"


def _guard_target(engine: str, url: URL) -> None:
    """Refuse DSNs that point somewhere a user did not mean to expose."""
    if engine in NETWORK_ENGINES:
        if not url.host:
            example = (
                "postgresql://user:password@host:5432/dbname"
                if engine == "postgres"
                else "mysql://user:password@host:3306/dbname"
            )
            raise HTTPException(
                status_code=422, detail=f"A {engine} DSN needs a host, e.g. {example}"
            )
        if _blocked_host(url.host):
            raise HTTPException(
                status_code=422,
                detail="That host is a link-local or metadata address, not a database",
            )
        return

    database = (url.database or "").strip()
    if not database or database == ":memory:":
        raise HTTPException(
            status_code=422, detail=f"A {engine} DSN needs a file path"
        )
    own = _own_database_path()
    if own is not None and _resolve_local(database) == own:
        raise HTTPException(
            status_code=422,
            detail="That path is this workspace's own database; pick a different file",
        )


def _connection_out(connection: DbConnection) -> DbConnectionOut:
    return DbConnectionOut(
        id=connection.id,
        name=connection.name,
        engine=connection.engine,
        read_only=bool(connection.read_only),
        status=connection.status,
        last_error=connection.last_error or "",
        dsn_summary=redacted_dsn(connection),
        created_at=connection.created_at,
    )


def _load(db: Session, actor: Actor, connection_id: str) -> DbConnection:
    connection = db.scalar(
        select(DbConnection).where(
            DbConnection.id == connection_id,
            DbConnection.workspace_id == actor.workspace_id,
        )
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="Database connection not found")
    return connection


@router.get("/connections", response_model=List[DbConnectionOut])
def list_connections(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[DbConnectionOut]:
    rows = db.scalars(
        select(DbConnection)
        .where(DbConnection.workspace_id == actor.workspace_id)
        .order_by(DbConnection.name.asc())
    )
    return [_connection_out(row) for row in rows]


@router.post("/connections", response_model=DbConnectionOut, status_code=201)
def create_connection(
    payload: DbConnectionRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DbConnectionOut:
    name = payload.name.strip()
    if not NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail="Name must be letters, digits, spaces, dashes, or underscores",
        )
    if payload.engine not in SUPPORTED_ENGINES:
        raise HTTPException(
            status_code=422,
            detail=f"Supported engines: {', '.join(SUPPORTED_ENGINES)}",
        )
    dsn = _normalize_dsn(payload.engine, payload.dsn)
    try:
        url = build_url(payload.engine, dsn)
    except DbConnectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _guard_target(payload.engine, url)

    duplicate = db.scalar(
        select(DbConnection).where(
            DbConnection.workspace_id == actor.workspace_id,
            DbConnection.name == name,
        )
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="That connection name is taken")
    try:
        # The DSN the user typed, not the rendered URL: rendering would pin a
        # driver they deliberately left unset.
        dsn_encrypted = encrypt_secret(dsn)
    except EncryptionNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    connection = DbConnection(
        workspace_id=actor.workspace_id,
        name=name,
        engine=payload.engine,
        dsn_encrypted=dsn_encrypted,
        read_only=payload.read_only,
        status="unknown",
        last_error="",
        created_by=actor.user_id,
    )
    db.add(connection)
    db.flush()  # the id is assigned on insert, and the audit row references it
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="db_connection.created",
        resource_type="db_connection",
        resource_id=connection.id,
        detail={"name": name, "engine": payload.engine, "read_only": payload.read_only},
    )
    db.commit()
    return _connection_out(connection)


@router.post("/connections/{connection_id}/test", response_model=DbConnectionOut)
def test_connection(
    connection_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DbConnectionOut:
    """Dial the database and record the outcome on the row.

    An unreachable database is an expected state, not a 500 — the row comes back
    with status "error" and the reason, so the UI can show it inline.
    """
    connection = _load(db, actor, connection_id)
    # The cached engine may hold a pool that predates a config or network change.
    dispose(connection.id)
    try:
        check_connection(connection)
    except DbConnectError as exc:
        connection.status = "error"
        connection.last_error = str(exc)[:1000]
    else:
        connection.status = "ready"
        connection.last_error = ""
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="db_connection.tested",
        resource_type="db_connection",
        resource_id=connection.id,
        detail={"name": connection.name, "status": connection.status},
    )
    db.commit()
    return _connection_out(connection)


def _table_out(payload: Dict[str, Any]) -> DbTableOut:
    raw_columns = payload.get("columns")
    primary = {str(name) for name in (payload.get("primary_key") or [])}
    columns = [
        DbColumnOut(
            name=str(column.get("name", "")),
            type=str(column.get("type", "")),
            nullable=bool(column.get("nullable", True)),
            primary_key=str(column.get("name", "")) in primary,
        )
        for column in (raw_columns or [])
    ]
    return DbTableOut(
        name=str(payload.get("name", "")),
        schema_name=str(payload.get("schema") or ""),
        columns_loaded=raw_columns is not None,
        columns=columns,
        columns_omitted=int(payload.get("columns_omitted") or 0),
        foreign_keys=[
            DbForeignKeyOut(
                columns=[str(item) for item in (fk.get("columns") or [])],
                references=str(fk.get("references") or ""),
                referred_columns=[
                    str(item) for item in (fk.get("referred_columns") or [])
                ],
            )
            for fk in (payload.get("foreign_keys") or [])
        ],
    )


@router.get("/connections/{connection_id}/schema", response_model=DbSchemaOut)
def get_schema(
    connection_id: str,
    table: str = Query(default="", max_length=200),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DbSchemaOut:
    """Tables for a connection, or one table's columns when `table` is given.

    Databases past a dozen tables come back as names only; ask again with
    `table=` for the columns of the one the user opened.
    """
    connection = _load(db, actor, connection_id)
    try:
        payload = describe(connection, table.strip() or None)
    except DbConnectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    single = payload.get("table")
    if isinstance(single, dict):
        return DbSchemaOut(
            connection_id=connection.id,
            connection=connection.name,
            engine=connection.engine,
            table_count=1,
            tables=[_table_out(single)],
        )
    return DbSchemaOut(
        connection_id=connection.id,
        connection=connection.name,
        engine=connection.engine,
        table_count=int(payload.get("table_count") or 0),
        tables=[
            _table_out(item)
            for item in (payload.get("tables") or [])
            if isinstance(item, dict)
        ],
        tables_omitted=int(payload.get("tables_omitted") or 0),
        note=str(payload.get("note") or ""),
    )


@router.delete("/connections/{connection_id}", status_code=204)
def delete_connection(
    connection_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    connection = _load(db, actor, connection_id)
    name = connection.name
    dispose(connection.id)
    db.delete(connection)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="db_connection.deleted",
        resource_type="db_connection",
        resource_id=connection_id,
        detail={"name": name},
    )
    db.commit()
