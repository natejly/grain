"""Agent tools for the workspace's configured database connections."""
from __future__ import annotations

import json
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import DbConnection
from ..llm_tools import ToolContext, ToolResult, ToolSpec
from . import engine as db


def list_connections(db_session: Session, workspace_id: str) -> List[DbConnection]:
    return list(
        db_session.scalars(
            select(DbConnection)
            .where(DbConnection.workspace_id == workspace_id)
            .order_by(DbConnection.name)
        )
    )


def resolve(db_session: Session, workspace_id: str, ref: str = "") -> DbConnection:
    """Find a connection by id or name.

    Models remember the name they were told, not the uuid, and in the common
    single-database workspace they should not have to say anything at all.
    """
    rows = list_connections(db_session, workspace_id)
    if not rows:
        raise db.DbConnectError(
            "No database connections are configured for this workspace."
        )
    wanted = (ref or "").strip()
    if not wanted:
        if len(rows) == 1:
            return rows[0]
        raise db.DbConnectError(
            "Several databases are connected — name one of: "
            + ", ".join(row.name for row in rows)
        )
    for row in rows:
        if row.id == wanted:
            return row
    folded = wanted.casefold()
    for row in rows:
        if row.name.casefold() == folded:
            return row
    raise db.DbConnectError(
        f"No connection named “{wanted}”. Available: "
        + ", ".join(row.name for row in rows)
    )


def _text(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _connection_ref(args: Dict[str, Any]) -> str:
    return _text(args, "connection") or _text(args, "connection_id")


def _list(db_session: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    rows = list_connections(db_session, context.workspace_id)
    if not rows:
        return ToolResult(
            content="No databases are connected. The user can add one in Connections."
        )
    return ToolResult(
        content=json.dumps(
            {
                "connections": [
                    {
                        "id": row.id,
                        "name": row.name,
                        "engine": row.engine,
                        "read_only": bool(row.read_only),
                        "status": row.status,
                        **({"last_error": row.last_error} if row.last_error else {}),
                    }
                    for row in rows
                ]
            }
        )
    )


def _describe(db_session: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        connection = resolve(db_session, context.workspace_id, _connection_ref(args))
        payload = db.describe(connection, _text(args, "table") or None)
    except db.DbConnectError as exc:
        return ToolResult(content=f"Error: {exc}")
    return ToolResult(content=json.dumps(payload, default=str))


def _limit(args: Dict[str, Any]) -> int:
    value = args.get("limit")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return db.DEFAULT_ROW_LIMIT
    try:
        # json.loads accepts NaN and Infinity, and int() refuses both.
        return int(value)
    except (ValueError, OverflowError):
        return db.DEFAULT_ROW_LIMIT


def _query(db_session: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        connection = resolve(db_session, context.workspace_id, _connection_ref(args))
        result = db.run_query(connection, _text(args, "sql"), limit=_limit(args))
    except db.DbConnectError as exc:
        return ToolResult(content=f"Error: {exc}")
    return ToolResult(content=json.dumps(result.as_payload(), default=str))


def _execute(db_session: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        connection = resolve(db_session, context.workspace_id, _connection_ref(args))
        summary = db.run_statement(connection, _text(args, "sql"))
    except db.DbConnectError as exc:
        return ToolResult(content=f"Error: {exc}")
    return ToolResult(content=f"{summary} ({connection.name})")


def _preview_execute(db_session: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    statement = _text(args, "sql")
    try:
        connection = resolve(db_session, context.workspace_id, _connection_ref(args))
    except db.DbConnectError as exc:
        return f"This statement will fail: {exc}"
    if connection.read_only:
        return (
            f"This statement will fail: “{connection.name}” is marked read-only.\n\n"
            f"{statement}"
        )
    try:
        statement = db.ensure_single_statement(statement)
    except db.DbConnectError as exc:
        return f"This statement will fail: {exc}"
    return (
        f"Write to “{connection.name}” ({connection.engine}). This runs against the "
        "real database and is committed — it cannot be undone from here.\n\n"
        f"{statement}"
    )


_TARGET: Dict[str, Any] = {
    "connection": {
        "type": "string",
        "description": (
            "Connection name or id. Omit when the workspace has one database."
        ),
    }
}


def registry_tools(db_session: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """Empty unless the workspace has connected at least one database."""
    if not list_connections(db_session, context.workspace_id):
        return {}
    return {
        "list_connections": ToolSpec(
            name="list_connections",
            description=(
                "List the databases this workspace can reach, with their engine, "
                "status, and whether they are read-only."
            ),
            parameters={"type": "object", "properties": {}},
            executor=_list,
        ),
        "describe_schema": ToolSpec(
            name="describe_schema",
            description=(
                "Inspect a connected database. With no `table`, lists its tables "
                "(plus columns when the database is small). With `table`, returns "
                "that table's columns, types, primary key and foreign keys. "
                "Always describe before writing SQL — do not guess column names."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **_TARGET,
                    "table": {
                        "type": "string",
                        "description": "Table name, optionally schema-qualified.",
                    },
                },
            },
            executor=_describe,
        ),
        "sql_query": ToolSpec(
            name="sql_query",
            description=(
                "Run one read-only SQL SELECT (or WITH … SELECT) and return rows. "
                "Anything else is rejected, the query runs in a transaction that is "
                "rolled back, and results are capped — so aggregate in SQL rather "
                "than selecting raw rows."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **_TARGET,
                    "sql": {"type": "string", "description": "A single SELECT."},
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"Max rows, default {db.DEFAULT_ROW_LIMIT}, "
                            f"hard cap {db.MAX_ROW_LIMIT}."
                        ),
                    },
                },
                "required": ["sql"],
            },
            executor=_query,
        ),
        "sql_execute": ToolSpec(
            name="sql_execute",
            description=(
                "Run one writing SQL statement (INSERT/UPDATE/DELETE/DDL) against a "
                "connection the user marked writable. The user approves the exact "
                "statement first. Use sql_query for anything that only reads."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **_TARGET,
                    "sql": {"type": "string", "description": "A single statement."},
                },
                "required": ["sql"],
            },
            executor=_execute,
            read_only=False,
            preview=_preview_execute,
        ),
    }


__all__ = ["registry_tools", "resolve", "list_connections"]
