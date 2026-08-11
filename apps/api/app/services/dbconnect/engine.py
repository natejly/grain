"""Connect to user-supplied databases, introspect them, and run guarded SQL.

The agent is allowed to point at production databases, so read-only-ness cannot
rest on a single check. Four layers, cheapest first:

1. `ensure_select` parses the statement with comments stripped and string/
   identifier bodies masked, so `/**/DROP` and `'; DROP` cannot smuggle a second
   statement or a data-modifying CTE past a naive keyword scan.
2. The session guards below ask the server itself to refuse writes where it can
   (Postgres read-only transactions, SQLite `PRAGMA query_only`).
3. Every read runs in a transaction that is rolled back, so even a write that
   slipped through both layers never lands.
4. Row and byte caps bound what comes back, so a `SELECT *` on a fact table
   cannot blow the model's context.

None of that substitutes for giving the DSN a least-privilege database role, but
it means a confused model cannot damage a database that a careless one could.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.exc import NoSuchModuleError, SQLAlchemyError

from ...config import Settings, get_settings
from ...models import DbConnection
from ..crypto import EncryptionNotConfiguredError, decrypt_secret

DEFAULT_ROW_LIMIT = 200
MAX_ROW_LIMIT = 1000
MAX_RESULT_CHARS = 8000
DEFAULT_TIMEOUT_MS = 15_000

# Beyond this many tables we stop sending per-table columns and send names only;
# the model can ask for a specific table. A 500-table warehouse still fits.
FULL_DETAIL_TABLE_CEILING = 12
MAX_TABLES_LISTED = 200
MAX_COLUMNS_PER_TABLE = 100

SUPPORTED_ENGINES = ("postgres", "mysql", "sqlite", "duckdb")

# Declared engine -> (accepted URL backends, preferred driver, install hint).
_ENGINE_DIALECTS: Dict[str, Tuple[Tuple[str, ...], str, str]] = {
    "postgres": (("postgresql", "postgres"), "postgresql+psycopg", "pip install 'psycopg[binary]'"),
    "mysql": (("mysql", "mariadb"), "mysql+pymysql", "pip install pymysql"),
    "sqlite": (("sqlite",), "sqlite", ""),
    "duckdb": (("duckdb",), "duckdb", "pip install duckdb-engine"),
}


class DbConnectError(RuntimeError):
    """Anything the user can fix: bad DSN, missing driver, server refused."""


class SqlGuardError(DbConnectError):
    """The statement was rejected before it ever reached the database."""


# --------------------------------------------------------------------------
# URL construction and engine cache


def build_url(engine: str, dsn: str) -> URL:
    """Turn a stored DSN into a SQLAlchemy URL for the declared engine."""
    kind = (engine or "").strip().lower()
    if kind not in _ENGINE_DIALECTS:
        raise DbConnectError(
            f"Unsupported engine “{engine}”. Supported: {', '.join(SUPPORTED_ENGINES)}."
        )
    raw = (dsn or "").strip()
    if not raw:
        raise DbConnectError("This connection has no DSN configured.")

    backends, preferred, _hint = _ENGINE_DIALECTS[kind]
    if "://" not in raw:
        # A bare filesystem path is the natural way to name a local file database.
        if kind not in ("sqlite", "duckdb"):
            raise DbConnectError(
                f"A {kind} DSN must be a URL, e.g. {backends[0]}://user:password@host:5432/dbname"
            )
        # SQLAlchemy counts slashes: sqlite:///x.db is relative to the process
        # cwd, sqlite:////x.db is the absolute /x.db. Prefixing with exactly
        # three keeps a relative path relative and an absolute path absolute —
        # writing two for the absolute case silently reinterprets /tmp/a.db as
        # tmp/a.db and opens (or creates) a different file.
        raw = f"{kind}:///{raw}"

    try:
        url = make_url(raw)
    except Exception as exc:  # make_url raises ArgumentError subclasses and ValueError
        raise DbConnectError(f"Could not parse the DSN: {exc}") from exc

    backend, _, driver = url.drivername.partition("+")
    if backend.lower() not in backends:
        raise DbConnectError(
            f"This connection is declared as {kind} but its DSN points at “{backend}”."
        )
    # Honour an explicitly pinned driver (psycopg2, asyncmy, …); otherwise pick
    # the one we actually depend on.
    return url if driver else url.set(drivername=preferred)


_DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306}


def _protected_paths(settings: Settings) -> List[Path]:
    """Files and directories a user connection must never be allowed to open.

    The application's own SQLite database is the sharp one: it holds every
    workspace's rows, sessions and encrypted secrets, so a `sqlite:///` DSN that
    named it would let one tenant `SELECT` the whole deployment out. Its object
    store and sandbox scratch go in for the same reason.
    """
    paths: List[Path] = []
    db_url = str(getattr(settings, "database_url", "") or "")
    prefix = "sqlite:///"
    if db_url.startswith(prefix):
        raw = db_url[len(prefix) :]
        if raw and raw != ":memory:":
            paths.append(Path(raw).resolve())
    for attr in ("objects_dir", "sandbox_workdir"):
        value = getattr(settings, attr, None)
        if value:
            try:
                paths.append(Path(value).resolve())
            except (TypeError, ValueError):
                continue
    return paths


def _guard_target(kind: str, url: URL, settings: Settings) -> None:
    """Refuse a connection aimed at the application's own host or files.

    dbconnect is *meant* to reach production databases — including private ones in
    an operator's own network — so this deliberately does not block RFC1918. What
    it blocks is the target that is never a tenant's database and always the
    application's own box: the loopback and link-local addresses a network DSN can
    name (cloud metadata answers on 169.254.169.254), and, for the file-backed
    engines, a path inside the app's own data — most sharply its own SQLite
    database, which a plain SELECT would otherwise read every tenant out of.

    The network check resolves the host once here; a DSN could rebind afterwards,
    but the driver speaks SQL rather than HTTP, so the value is closing the plain
    "point it at localhost or the metadata service" case, not defeating an
    attacker who controls the target's own DNS. An address that will not resolve
    is left for `create_engine` to report as the connection error it is.
    """
    if kind in ("postgres", "mysql"):
        host = (url.host or "").strip().rstrip(".")
        if not host:
            return
        try:
            infos = socket.getaddrinfo(
                host, url.port or _DEFAULT_PORTS.get(kind), type=socket.SOCK_STREAM
            )
        except socket.gaierror:
            return
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                raise DbConnectError(
                    f"“{host}” resolves to {ip}, an address on this server's own "
                    "network. A connection may not point at the API host or the "
                    "cloud metadata service; use the database's real address."
                )
        return
    if kind in ("sqlite", "duckdb"):
        raw = (url.database or "").strip()
        if not raw or raw == ":memory:":
            return
        target = Path(raw).resolve()
        for protected in _protected_paths(settings):
            if target == protected or target.is_relative_to(protected):
                raise DbConnectError(
                    "This connection would open a file that belongs to the "
                    "application itself. Point it at your own database file."
                )


def _fingerprint(url: URL, read_only: bool) -> str:
    return f"{url.render_as_string(hide_password=False)}|{read_only}"


_ENGINES: Dict[str, Tuple[str, Engine]] = {}


def _connect_args(kind: str, url: URL) -> Dict[str, Any]:
    if kind == "sqlite":
        # Engines are cached across requests and FastAPI serves them on a thread
        # pool, so the connection may legitimately move between threads.
        return {"check_same_thread": False}
    if kind == "postgres":
        return {"connect_timeout": 10}
    if kind == "mysql":
        return {"connect_timeout": 10}
    return {}


def get_engine(connection: DbConnection) -> Engine:
    """A pooled Engine for this connection, rebuilt when its config changes."""
    url = build_url(connection.engine, decrypted_dsn(connection))
    stamp = _fingerprint(url, bool(connection.read_only))
    cached = _ENGINES.get(connection.id)
    if cached is not None:
        if cached[0] == stamp:
            return cached[1]
        dispose(connection.id)

    kind = connection.engine.strip().lower()
    try:
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args=_connect_args(kind, url),
        )
    except (ModuleNotFoundError, ImportError) as exc:
        hint = _ENGINE_DIALECTS[kind][2]
        suffix = f" Install it with `{hint}`." if hint else ""
        raise DbConnectError(
            f"The {kind} driver is not installed in this deployment.{suffix}"
        ) from exc
    except NoSuchModuleError as exc:
        raise DbConnectError(f"No SQLAlchemy dialect for “{url.drivername}”.") from exc
    except SQLAlchemyError as exc:
        raise DbConnectError(f"Could not build a connection: {_clean_error(exc, url)}") from exc

    # Refuse a target that is the application's own host or files — before the
    # engine is cached or handed back, and so before any caller opens it. This
    # runs after create_engine on purpose: create_engine opens no connection (it
    # is lazy), so a missing driver still reports as a missing driver, while a
    # target on this server's own network never gets dialed. Every caller — query,
    # describe, the Test button — reaches a live database through this one door.
    try:
        _guard_target(kind, url, get_settings())
    except DbConnectError:
        engine.dispose()
        raise

    _ENGINES[connection.id] = (stamp, engine)
    return engine


def dispose(connection_id: str) -> None:
    """Drop the cached engine — call after editing or deleting a connection."""
    cached = _ENGINES.pop(connection_id, None)
    if cached is not None:
        cached[1].dispose()


def dispose_all() -> None:
    for connection_id in list(_ENGINES):
        dispose(connection_id)


def decrypted_dsn(connection: DbConnection) -> str:
    try:
        return decrypt_secret(connection.dsn_encrypted)
    except EncryptionNotConfiguredError as exc:
        raise DbConnectError(
            "Database connections need INTEGRATIONS_ENCRYPTION_KEY to be configured."
        ) from exc
    except Exception as exc:
        raise DbConnectError(
            "The stored DSN could not be decrypted; re-enter it for this connection."
        ) from exc


def redacted_dsn(connection: DbConnection) -> str:
    """A display form with the password removed — safe to return over the API."""
    try:
        return build_url(connection.engine, decrypted_dsn(connection)).render_as_string(
            hide_password=True
        )
    except DbConnectError:
        return ""


def _clean_error(exc: BaseException, url: Optional[URL] = None) -> str:
    original = getattr(exc, "orig", None)
    message = str(original) if original is not None else str(exc)
    message = message.split("\n(Background on this error")[0].strip()
    password = url.password if url is not None else None
    if password:
        message = message.replace(str(password), "***")
    return message[:500] or exc.__class__.__name__


# --------------------------------------------------------------------------
# SQL guard

# Keywords that can appear *inside* an otherwise well-formed SELECT and still
# change data — data-modifying CTEs, `SELECT … INTO`, `SELECT … INTO OUTFILE`,
# procedure calls. Statement-initial-only keywords (COPY, SET, LOAD, PRAGMA …)
# are already unreachable because the first token must be SELECT or WITH, and
# leaving them out avoids rejecting ordinary columns named `load` or `copy`.
_FORBIDDEN = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "UPSERT",
    "INTO",
    "CREATE",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "ATTACH",
    "DETACH",
    "VACUUM",
    "REINDEX",
    "PRAGMA",
    "INSTALL",
    "EXEC",
    "EXECUTE",
    "DEALLOCATE",
    "DISCARD",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN) + r")\b", re.IGNORECASE)
_LEADING_RE = re.compile(r"^(SELECT|WITH)\b", re.IGNORECASE)
_HAS_LIMIT_RE = re.compile(r"\b(LIMIT|FETCH\s+(FIRST|NEXT))\b", re.IGNORECASE)
# Postgres and DuckDB have a second string syntax, `$tag$ … $tag$`, in which a
# lone `'` is an ordinary character. `_scan` deliberately does not implement it
# (doing so would mask `$q$ … $q$` on MySQL, where that is a legal identifier and
# not a string at all), so a dollar-quote opener means our idea of where the
# single-quoted strings start can disagree with the server's — which is exactly
# how `SELECT $$'$$ AS a; DROP TABLE t; SELECT $$'$$ AS b LIMIT 1` used to slip a
# second statement past the mask. Divergence can only *begin* at an opener the
# scanner still sees as code, so rejecting one here closes the whole family.
_DOLLAR_QUOTE_RE = re.compile(r"\$(?:[A-Za-z_\u0080-\uffff][A-Za-z0-9_\u0080-\uffff]*)?\$")
# MySQL and MariaDB *execute* the body of /*! ... */ and /*!50700 ... */ instead
# of ignoring it. Stripping them as ordinary comments would hide a whole
# statement from every check below, so they are refused outright \u2014 a read query
# has no legitimate reason to carry one.
_EXECUTABLE_COMMENT_RE = re.compile(r"/\*!")


def _scan(sql: str) -> Tuple[str, str]:
    """Strip comments; return (executable sql, same sql with literals masked).

    Both strings have identical length so callers can slice them in lockstep.
    Backslashes are *not* treated as escapes: standard SQL does not have them,
    and misreading a MySQL `'\\''` ends the literal early, which produces a
    spurious rejection rather than a keyword we failed to see.
    """
    out: List[str] = []
    masked: List[str] = []
    i = 0
    length = len(sql)
    while i < length:
        char = sql[i]
        pair = sql[i : i + 2]
        if pair == "--":
            end = sql.find("\n", i)
            i = length if end == -1 else end
            continue
        if pair == "/*":
            depth = 1  # Postgres nests block comments; a naive find() would stop early.
            i += 2
            while i < length and depth:
                nxt = sql[i : i + 2]
                if nxt == "/*":
                    depth += 1
                    i += 2
                elif nxt == "*/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise SqlGuardError("Unterminated block comment.")
            out.append(" ")
            masked.append(" ")
            continue
        if char in ("'", '"', "`"):
            quote = char
            out.append(char)
            masked.append(char)
            i += 1
            closed = False
            while i < length:
                if sql[i] == quote:
                    if sql[i : i + 2] == quote * 2:  # doubled quote is an escaped quote
                        out.append(quote * 2)
                        masked.append("xx")
                        i += 2
                        continue
                    out.append(quote)
                    masked.append(quote)
                    i += 1
                    closed = True
                    break
                out.append(sql[i])
                masked.append("x")
                i += 1
            if not closed:
                raise SqlGuardError("Unterminated quoted string.")
            continue
        out.append(char)
        masked.append(char)
        i += 1
    return "".join(out), "".join(masked)


def ensure_single_statement(sql: str) -> str:
    """Comment-free, single-statement SQL, or raise. No SELECT requirement."""
    body, mask = _scan(sql or "")
    end = len(mask)
    while end and (mask[end - 1].isspace() or mask[end - 1] == ";"):
        end -= 1
    body, mask = body[:end].strip(), mask[:end].strip()
    if not mask:
        raise SqlGuardError("No SQL statement was provided.")
    if ";" in mask:
        raise SqlGuardError(
            "Multiple statements are not allowed — send one statement per call."
        )
    return body


def ensure_select(sql: str) -> str:
    """Return executable SQL, or raise if it is anything but one read query."""
    # Checked against the raw text: the masking pass treats these as comments,
    # which is exactly how they would slip past.
    if _EXECUTABLE_COMMENT_RE.search(sql):
        raise SqlGuardError(
            "MySQL executable comments (/*! … */) are not allowed in a read "
            "query — the server runs their contents."
        )
    body = ensure_single_statement(sql)
    _, mask = _scan(body)
    if _DOLLAR_QUOTE_RE.search(mask):
        raise SqlGuardError(
            "Dollar-quoted strings ($$…$$) are not allowed in a read query — "
            "use ordinary '…' literals."
        )
    probe = mask.lstrip("( \t\r\n")
    if not _LEADING_RE.match(probe):
        first = (re.split(r"\W+", probe, maxsplit=1)[0] or probe[:12]).upper()
        raise SqlGuardError(
            f"Only SELECT queries are allowed here; this statement starts with “{first}”. "
            "Use sql_execute for writes."
        )
    found = _FORBIDDEN_RE.search(mask)
    if found:
        raise SqlGuardError(
            f"“{found.group(1).upper()}” is not allowed in a read query. "
            "Use sql_execute if you really need to modify data."
        )
    return body


def enforce_limit(sql: str, limit: int) -> str:
    """Wrap an unbounded query so the server stops after `limit` rows."""
    _, mask = _scan(sql)
    if _HAS_LIMIT_RE.search(mask):
        return sql
    return f"SELECT * FROM (\n{sql}\n) AS dbconnect_bounded LIMIT {int(limit)}"


# --------------------------------------------------------------------------
# Execution


@dataclass
class QueryResult:
    columns: List[str] = field(default_factory=list)
    rows: List[List[Any]] = field(default_factory=list)
    truncated: bool = False
    limit: int = DEFAULT_ROW_LIMIT
    sql: str = ""

    def as_payload(self) -> Dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": len(self.rows),
            "truncated": self.truncated,
        }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


def _fit_row(row: List[Any], budget: int) -> List[Any]:
    """Clip one row's text cells so the row itself cannot exceed `budget`.

    A single TEXT/JSON column can hold megabytes, and the caller has to hand the
    model *something* for the first row rather than an empty result — so the row
    comes back with each cell clipped to an even share of the budget instead of
    blowing MAX_RESULT_CHARS wide open.
    """
    share = max(16, budget // max(1, len(row)))
    return [
        f"{value[:share]}… (+{len(value) - share} chars)"
        if isinstance(value, str) and len(value) > share
        else value
        for value in row
    ]


def _apply_read_guards(conn: Any, kind: str, timeout_ms: int) -> List[str]:
    """Ask the server to enforce read-only itself. Returns teardown statements.

    Best effort by design: an old server or a restricted role may refuse a guard,
    and that must not turn a legitimate query into an error — the parser and the
    rollback still stand behind it.
    """
    if kind == "postgres":
        guards = [
            ("SET TRANSACTION READ ONLY", ""),
            (f"SET LOCAL statement_timeout = {int(timeout_ms)}", ""),
        ]
    elif kind == "mysql":
        # MySQL cannot switch an already-open transaction to read-only, so the
        # timeout is the only server-side guard available here.
        guards = [(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_ms)}", "")]
    elif kind == "sqlite":
        guards = [("PRAGMA query_only = ON", "PRAGMA query_only = OFF")]
    else:
        guards = []

    teardown: List[str] = []
    for apply_sql, undo_sql in guards:
        try:
            conn.exec_driver_sql(apply_sql)
        except SQLAlchemyError:
            continue
        if undo_sql:
            teardown.append(undo_sql)
    return teardown


def run_query(
    connection: DbConnection,
    sql: str,
    limit: int = DEFAULT_ROW_LIMIT,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> QueryResult:
    """Run one SELECT and return capped rows. Raises DbConnectError on refusal."""
    body = ensure_select(sql)
    capped = max(1, min(int(limit or DEFAULT_ROW_LIMIT), MAX_ROW_LIMIT))
    # One row past the cap, so the server still stops early but we can tell the
    # model its result was cut short rather than silently handing back a slice.
    prepared = enforce_limit(body, capped + 1)
    engine = get_engine(connection)
    kind = connection.engine.strip().lower()

    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            teardown: List[str] = []
            try:
                teardown = _apply_read_guards(conn, kind, timeout_ms)
                cursor = conn.exec_driver_sql(prepared)
                columns = [str(name) for name in cursor.keys()]
                fetched = cursor.fetchmany(capped)
                more = cursor.fetchone() is not None
            finally:
                transaction.rollback()
                for statement in teardown:
                    # The pooled connection outlives this transaction; leaving
                    # query_only latched would break the write path later.
                    try:
                        conn.exec_driver_sql(statement)
                    except SQLAlchemyError:
                        conn.invalidate()
    except SqlGuardError:
        raise
    except SQLAlchemyError as exc:
        raise DbConnectError(_clean_error(exc)) from exc

    rows: List[List[Any]] = []
    budget = MAX_RESULT_CHARS
    truncated = more
    for record in fetched:
        row = [_jsonable(value) for value in record]
        cost = len(json.dumps(row, default=str))
        if cost > budget:
            # The first row still has to come back — but clipped, not raw: one
            # multi-megabyte cell would otherwise sail straight past the cap and
            # into the model's context.
            if not rows:
                rows.append(_fit_row(row, budget))
            truncated = True
            break
        budget -= cost
        rows.append(row)
    return QueryResult(
        columns=columns, rows=rows, truncated=truncated, limit=capped, sql=prepared
    )


def run_statement(connection: DbConnection, sql: str) -> str:
    """Run one write statement and commit it. Only for read_only=False rows."""
    if connection.read_only:
        raise DbConnectError(
            f"Connection “{connection.name}” is marked read-only; writes are refused."
        )
    body = ensure_single_statement(sql)
    engine = get_engine(connection)
    try:
        with engine.begin() as conn:
            cursor = conn.exec_driver_sql(body)
            affected = cursor.rowcount
    except SQLAlchemyError as exc:
        raise DbConnectError(_clean_error(exc)) from exc
    if affected is None or affected < 0:
        return "Statement executed."
    plural = "row" if affected == 1 else "rows"
    return f"Statement executed; {affected} {plural} affected."


# --------------------------------------------------------------------------
# Introspection


def _columns_for(inspector: Any, table: str, schema: Optional[str]) -> Dict[str, Any]:
    columns = inspector.get_columns(table, schema=schema)
    pk = inspector.get_pk_constraint(table, schema=schema) or {}
    primary = [str(name) for name in (pk.get("constrained_columns") or [])]
    shown = columns[:MAX_COLUMNS_PER_TABLE]
    payload: Dict[str, Any] = {
        "name": table,
        "columns": [
            {
                "name": str(column["name"]),
                "type": str(column.get("type")),
                "nullable": bool(column.get("nullable", True)),
            }
            for column in shown
        ],
        "primary_key": primary,
    }
    if schema:
        payload["schema"] = schema
    if len(columns) > len(shown):
        payload["columns_omitted"] = len(columns) - len(shown)
    return payload


def describe(
    connection: DbConnection, table: Optional[str] = None
) -> Dict[str, Any]:
    """Tables, or one table's columns, primary key and foreign keys."""
    engine = get_engine(connection)
    try:
        inspector = inspect(engine)
        if table:
            schema, _, bare = table.rpartition(".")
            schema_name = schema or None
            names = set(inspector.get_table_names(schema=schema_name)) | set(
                inspector.get_view_names(schema=schema_name)
            )
            if bare not in names:
                close = sorted(names)[:40]
                raise DbConnectError(
                    f"No table “{table}”. Tables here: {', '.join(close) or 'none'}."
                )
            payload = _columns_for(inspector, bare, schema_name)
            payload["foreign_keys"] = [
                {
                    "columns": [str(item) for item in (fk.get("constrained_columns") or [])],
                    "references": fk.get("referred_table"),
                    "referred_columns": [
                        str(item) for item in (fk.get("referred_columns") or [])
                    ],
                }
                for fk in (inspector.get_foreign_keys(bare, schema=schema_name) or [])
            ]
            return {"connection": connection.name, "table": payload}

        tables = list(inspector.get_table_names())
        views = list(inspector.get_view_names())
        total = len(tables) + len(views)
        listed = (tables + views)[:MAX_TABLES_LISTED]
        if total <= FULL_DETAIL_TABLE_CEILING:
            detail: List[Dict[str, Any]] = [
                _columns_for(inspector, name, None) for name in listed
            ]
        else:
            detail = [{"name": name} for name in listed]
    except DbConnectError:
        raise
    except SQLAlchemyError as exc:
        raise DbConnectError(_clean_error(exc)) from exc

    payload = {
        "connection": connection.name,
        "engine": connection.engine,
        "table_count": total,
        "tables": detail,
    }
    if total > len(listed):
        payload["tables_omitted"] = total - len(listed)
    if total > FULL_DETAIL_TABLE_CEILING:
        payload["note"] = "Call describe_schema with `table` set for column details."
    return payload


def check_connection(connection: DbConnection) -> str:
    """Open a connection and run the dialect's trivial query. Returns a summary."""
    engine = get_engine(connection)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except SQLAlchemyError as exc:
        raise DbConnectError(_clean_error(exc)) from exc
    return f"Connected to {connection.name} ({connection.engine})."
