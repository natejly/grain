from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.config import get_settings
from app.database import SessionLocal
from app.models import DbConnection, Workspace
from app.services.crypto import encrypt_secret
from app.services.dbconnect import engine as dbengine
from app.services.dbconnect.tools import registry_tools, resolve
from app.services.llm_tools import ToolContext


@pytest.fixture(scope="module")
def encryption():
    settings = get_settings()
    original = settings.integrations_encryption_key
    settings.integrations_encryption_key = SecretStr(Fernet.generate_key().decode())
    yield settings
    settings.integrations_encryption_key = original


@pytest.fixture(scope="module")
def sample_db():
    """A throwaway SQLite file with two tables and enough rows to hit the caps."""
    handle, path = tempfile.mkstemp(suffix=".db", prefix="dbconnect-test-")
    os.close(handle)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            region TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            amount REAL,
            note TEXT
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            body TEXT
        );
        """
    )
    # One cell far larger than the whole byte budget, so the cap has to clip it.
    conn.execute("INSERT INTO documents (id, body) VALUES (1, ?)", ("y" * 2_000_000,))
    conn.executemany(
        "INSERT INTO customers (id, name, region) VALUES (?, ?, ?)",
        [(i, f"Customer {i}", "north" if i % 2 else "south") for i in range(1, 501)],
    )
    conn.executemany(
        "INSERT INTO orders (id, customer_id, amount, note) VALUES (?, ?, ?, ?)",
        [(i, i, i * 1.5, "x" * 200) for i in range(1, 501)],
    )
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


def _make_connection(workspace_id: str, path: str, *, name: str, read_only: bool):
    with SessionLocal() as session:
        row = DbConnection(
            workspace_id=workspace_id,
            name=name,
            engine="sqlite",
            dsn_encrypted=encrypt_secret(f"sqlite:///{path}"),
            read_only=read_only,
            status="ready",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


@pytest.fixture(scope="module")
def connected(client, encryption, sample_db):
    """Two connections — one read-only, one writable — cleaned up afterwards."""
    workspace_id = client.get("/api/bootstrap").json()["identity"]["workspace_id"]
    reader = _make_connection(workspace_id, sample_db, name="analytics", read_only=True)
    writer = _make_connection(workspace_id, sample_db, name="staging", read_only=False)
    yield workspace_id, reader, writer
    dbengine.dispose_all()
    with SessionLocal() as session:
        for connection_id in (reader, writer):
            row = session.get(DbConnection, connection_id)
            if row is not None:
                session.delete(row)
        session.commit()


@pytest.fixture()
def solo(client, encryption, sample_db):
    """A separate workspace holding exactly one connection."""
    with SessionLocal() as session:
        workspace = Workspace(name="dbconnect-solo")
        session.add(workspace)
        session.commit()
        workspace_id = workspace.id
    connection_id = _make_connection(workspace_id, sample_db, name="only", read_only=True)
    yield workspace_id, connection_id
    dbengine.dispose(connection_id)
    with SessionLocal() as session:
        row = session.get(DbConnection, connection_id)
        if row is not None:
            session.delete(row)
        workspace = session.get(Workspace, workspace_id)
        if workspace is not None:
            session.delete(workspace)
        session.commit()


def _row(connection_id: str, session) -> DbConnection:
    row = session.get(DbConnection, connection_id)
    assert row is not None
    return row


def _tools(workspace_id: str):
    session = SessionLocal()
    context = ToolContext(
        workspace_id=workspace_id, user_id="tester", conversation_id="conv"
    )
    return session, context, registry_tools(session, context)


# --------------------------------------------------------------------------
# The guard, in isolation — no database needed


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO customers (name) VALUES ('x')",
        "UPDATE customers SET name = 'x'",
        "DELETE FROM customers",
        "DROP TABLE customers",
        "ALTER TABLE customers ADD COLUMN sneaky TEXT",
        "CREATE TABLE evil (id INTEGER)",
        "TRUNCATE TABLE customers",
        "PRAGMA query_only = OFF",
        "ATTACH DATABASE '/tmp/x.db' AS x",
    ],
)
def test_writes_are_rejected_before_execution(sql):
    with pytest.raises(dbengine.SqlGuardError):
        dbengine.ensure_select(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "/**/DROP TABLE customers",
        "SELECT 1 /* harmless */; DROP TABLE customers",
        "-- comment\nDROP TABLE customers",
        "SELECT 1 -- \n; DELETE FROM customers",
        "WITH x AS (/*note*/DELETE FROM customers RETURNING id) SELECT * FROM x",
        "SEL/**/ECT 1",
    ],
)
def test_comment_smuggled_ddl_is_rejected(sql):
    with pytest.raises(dbengine.SqlGuardError):
        dbengine.ensure_select(sql)


# Postgres and DuckDB read `$$…$$` as a string literal in which a lone `'` is an
# ordinary character. The guard masks `'…'` spans, so these strings used to hand
# it a mask with no semicolon in it at all — while the server saw three
# statements, the middle one a write, and a trailing LIMIT kept enforce_limit
# from wrapping (and so breaking) the payload.
DOLLAR_QUOTE_SMUGGLERS = [
    "SELECT $$'$$ AS a; DROP TABLE customers; SELECT $$'$$ AS b LIMIT 1",
    "SELECT $q$'$q$ AS a; DELETE FROM customers; COMMIT; "
    "SELECT $q$'$q$ AS b LIMIT 1",
]


@pytest.mark.parametrize("sql", DOLLAR_QUOTE_SMUGGLERS)
def test_dollar_quoting_cannot_hide_a_stacked_write(sql):
    with pytest.raises(dbengine.SqlGuardError):
        dbengine.ensure_select(sql)


def test_the_dollar_quote_payload_really_would_have_written():
    """Proof the rejection above is not theatre: the string mutates a real engine.

    DuckDB is one of the supported engines, takes `$$…$$`, runs every statement in
    a single call, and gets no server-side read-only guard — and the embedded
    COMMIT lands the delete before run_query's rollback can undo it.
    """
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE TABLE customers (id INTEGER)")
    con.execute("INSERT INTO customers VALUES (1), (2), (3)")
    con.execute("BEGIN TRANSACTION")
    con.execute(DOLLAR_QUOTE_SMUGGLERS[1])
    assert con.execute("SELECT COUNT(*) FROM customers").fetchall() == [(0,)]


MYSQL_EXECUTABLE_COMMENTS = [
    "SELECT 1 /*! ; DROP TABLE t */",
    "SELECT 1 /*!50700 ; DROP TABLE t */",
    "/*! DELETE FROM t */ SELECT 1",
    "SELECT 1 /*!UNION SELECT * FROM secrets*/",
]


@pytest.mark.parametrize("sql", MYSQL_EXECUTABLE_COMMENTS)
def test_mysql_executable_comments_are_rejected(sql):
    """MySQL runs the body of /*! … */, so stripping it as a comment hides a write.

    The masking pass treats these like any other block comment, which is exactly
    how a DROP would reach a mysql connection unseen — hence the raw-text check
    ahead of the mask.
    """
    with pytest.raises(dbengine.SqlGuardError):
        dbengine.ensure_select(sql)


def test_ordinary_block_comments_are_still_allowed():
    """The inert kind must keep working, or every commented query breaks."""
    assert dbengine.ensure_select("SELECT 1 /* ; DROP TABLE t */")
    assert dbengine.ensure_select("/* a note */ SELECT 2")


def test_dollar_signs_in_ordinary_queries_are_still_fine():
    for sql in (
        "SELECT name FROM customers WHERE note = 'a$b'",
        "SELECT '$$' AS literal",
        "SELECT amount FROM orders WHERE amount > 1",
    ):
        assert dbengine.ensure_select(sql) == sql


def test_multiple_statements_are_rejected():
    with pytest.raises(dbengine.SqlGuardError) as caught:
        dbengine.ensure_select("SELECT 1; SELECT 2")
    assert "one statement" in str(caught.value)


def test_a_trailing_semicolon_is_fine():
    assert dbengine.ensure_select("SELECT 1;  ") == "SELECT 1"


def test_semicolons_inside_string_literals_are_not_statement_breaks():
    sql = "SELECT 'a;b' AS pair"
    assert dbengine.ensure_select(sql) == sql


def test_keywords_inside_string_literals_do_not_trip_the_guard():
    sql = "SELECT name FROM customers WHERE note = 'please delete this'"
    assert dbengine.ensure_select(sql) == sql


def test_read_only_cte_is_allowed():
    sql = (
        "WITH totals AS (SELECT region, COUNT(*) c FROM customers GROUP BY region) "
        "SELECT * FROM totals"
    )
    assert dbengine.ensure_select(sql) == sql


def test_select_into_is_rejected():
    with pytest.raises(dbengine.SqlGuardError):
        dbengine.ensure_select("SELECT * INTO backup FROM customers")


def test_empty_sql_is_rejected():
    with pytest.raises(dbengine.SqlGuardError):
        dbengine.ensure_select("   -- nothing here\n")


def test_limit_is_forced_when_absent_and_left_alone_when_present():
    forced = dbengine.enforce_limit("SELECT * FROM customers", 25)
    assert forced.startswith("SELECT * FROM (")
    assert forced.rstrip().endswith("LIMIT 25")
    already = "SELECT * FROM customers LIMIT 3"
    assert dbengine.enforce_limit(already, 25) == already


def test_build_url_rejects_an_unsupported_engine():
    with pytest.raises(dbengine.DbConnectError):
        dbengine.build_url("oracle", "oracle://host/db")


def test_build_url_rejects_a_dsn_from_a_different_backend():
    with pytest.raises(dbengine.DbConnectError) as caught:
        dbengine.build_url("sqlite", "postgresql://user:pw@host/db")
    assert "postgresql" in str(caught.value)


@pytest.mark.parametrize(
    ("engine", "dsn"),
    [
        ("sqlite", "/tmp/analytics.db"),
        ("duckdb", "/var/db/warehouse.duckdb"),
    ],
)
def test_absolute_file_dsns_stay_absolute(engine, dsn):
    """sqlite:///x is relative and sqlite:////x is absolute.

    Getting this wrong does not error — it silently opens (or creates) a
    different file relative to the server's cwd, which is the same class of bug
    recorded in tasks/lessons.md for the app's own database URL.
    """
    assert dbengine.build_url(engine, dsn).database == dsn


def test_relative_file_dsns_stay_relative():
    assert dbengine.build_url("sqlite", "data/analytics.db").database == "data/analytics.db"


def test_postgres_urls_get_the_driver_we_depend_on():
    url = dbengine.build_url("postgres", "postgresql://user:pw@host:5432/db")
    assert url.drivername == "postgresql+psycopg"
    assert "pw" not in url.render_as_string(hide_password=True)


def test_missing_driver_is_a_clean_error_not_an_import_traceback(encryption):
    row = DbConnection(
        id="missing-driver-probe",
        workspace_id="w",
        name="pg",
        engine="postgres",
        dsn_encrypted=encrypt_secret("postgresql://user:pw@127.0.0.1:5432/db"),
        read_only=True,
    )
    try:
        import psycopg  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("psycopg is installed here, so there is no missing driver to test")
    with pytest.raises(dbengine.DbConnectError) as caught:
        dbengine.get_engine(row)
    assert "driver is not installed" in str(caught.value)


# --------------------------------------------------------------------------
# Against a real SQLite file


def test_select_returns_rows(connected):
    workspace_id, reader, _ = connected
    with SessionLocal() as session:
        result = dbengine.run_query(
            _row(reader, session),
            "SELECT name, region FROM customers WHERE id = 7",
        )
    assert result.columns == ["name", "region"]
    assert result.rows == [["Customer 7", "north"]]


def test_row_cap_is_applied(connected):
    workspace_id, reader, _ = connected
    with SessionLocal() as session:
        result = dbengine.run_query(
            _row(reader, session), "SELECT id FROM customers", limit=10
        )
    assert len(result.rows) == 10
    assert result.truncated is True


def test_row_cap_is_clamped_to_the_hard_maximum(connected):
    workspace_id, reader, _ = connected
    with SessionLocal() as session:
        result = dbengine.run_query(
            _row(reader, session), "SELECT id FROM customers", limit=99_999
        )
    assert result.limit == dbengine.MAX_ROW_LIMIT


def test_byte_cap_truncates_wide_results(connected):
    workspace_id, reader, _ = connected
    with SessionLocal() as session:
        result = dbengine.run_query(
            _row(reader, session), "SELECT id, note FROM orders", limit=200
        )
    assert result.truncated is True
    assert len(result.rows) < 200
    assert len(json.dumps(result.rows)) <= dbengine.MAX_RESULT_CHARS + 500


def test_one_huge_cell_cannot_blow_the_byte_cap(connected):
    """The cap has to bound the *first* row too, not only the ones after it."""
    workspace_id, reader, _ = connected
    with SessionLocal() as session:
        result = dbengine.run_query(_row(reader, session), "SELECT body FROM documents")
    assert result.truncated is True
    assert len(json.dumps(result.rows)) <= dbengine.MAX_RESULT_CHARS + 500
    assert len(result.rows) == 1
    assert 0 < len(result.rows[0][0]) < 2_000_000


def test_a_rejected_write_leaves_the_database_untouched(connected):
    workspace_id, reader, _ = connected
    with SessionLocal() as session:
        row = _row(reader, session)
        with pytest.raises(dbengine.SqlGuardError):
            dbengine.run_query(row, "DELETE FROM customers WHERE id = 1")
        after = dbengine.run_query(row, "SELECT COUNT(*) FROM customers")
    assert after.rows[0][0] == 500


def test_introspection_lists_tables_and_columns(connected):
    workspace_id, reader, _ = connected
    with SessionLocal() as session:
        payload = dbengine.describe(_row(reader, session))
    names = {table["name"] for table in payload["tables"]}
    assert {"customers", "orders"} <= names
    customers = next(t for t in payload["tables"] if t["name"] == "customers")
    assert [column["name"] for column in customers["columns"]] == ["id", "name", "region"]
    assert customers["primary_key"] == ["id"]


def test_introspection_of_one_table_includes_types_and_foreign_keys(connected):
    workspace_id, reader, _ = connected
    with SessionLocal() as session:
        payload = dbengine.describe(_row(reader, session), "orders")
    table = payload["table"]
    types = {column["name"]: column["type"] for column in table["columns"]}
    assert "INTEGER" in types["id"].upper()
    assert table["foreign_keys"][0]["references"] == "customers"


def test_introspection_of_an_unknown_table_lists_real_tables(connected):
    workspace_id, reader, _ = connected
    with SessionLocal() as session:
        with pytest.raises(dbengine.DbConnectError) as caught:
            dbengine.describe(_row(reader, session), "custmers")
    assert "customers" in str(caught.value)


def test_engines_are_cached_and_disposed_on_config_change(connected):
    workspace_id, reader, _ = connected
    with SessionLocal() as session:
        row = _row(reader, session)
        first = dbengine.get_engine(row)
        assert dbengine.get_engine(row) is first
        row.read_only = False
        second = dbengine.get_engine(row)
    assert second is not first


def test_writes_need_a_writable_connection(connected):
    workspace_id, reader, writer = connected
    with SessionLocal() as session:
        with pytest.raises(dbengine.DbConnectError) as caught:
            dbengine.run_statement(_row(reader, session), "DELETE FROM customers")
        assert "read-only" in str(caught.value)
        summary = dbengine.run_statement(
            _row(writer, session),
            "INSERT INTO customers (id, name, region) VALUES (9001, 'Temp', 'west')",
        )
        assert "1 row" in summary
        dbengine.run_statement(
            _row(writer, session), "DELETE FROM customers WHERE id = 9001"
        )


def test_the_server_side_guard_refuses_writes_even_if_the_parser_is_bypassed(connected):
    """Layer 2 on its own: SQLite must refuse the write, parser out of the picture."""
    workspace_id, reader, _ = connected
    with SessionLocal() as session:
        engine = dbengine.get_engine(_row(reader, session))
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            dbengine._apply_read_guards(conn, "sqlite", dbengine.DEFAULT_TIMEOUT_MS)
            with pytest.raises(Exception) as caught:
                conn.exec_driver_sql("DELETE FROM customers WHERE id = 1")
            assert "readonly" in str(caught.value).lower()
        finally:
            transaction.rollback()
            conn.exec_driver_sql("PRAGMA query_only = OFF")


def test_reads_do_not_latch_the_pooled_connection_read_only(connected):
    workspace_id, _reader, writer = connected
    with SessionLocal() as session:
        row = _row(writer, session)
        dbengine.run_query(row, "SELECT COUNT(*) FROM customers")
        dbengine.run_statement(
            row, "INSERT INTO customers (id, name, region) VALUES (9100, 'After', 'east')"
        )
        dbengine.run_statement(row, "DELETE FROM customers WHERE id = 9100")


def test_check_connection_reports_success(connected):
    workspace_id, reader, _ = connected
    with SessionLocal() as session:
        assert "analytics" in dbengine.check_connection(_row(reader, session))


# --------------------------------------------------------------------------
# Tool surface


def test_registry_exposes_the_four_tools_with_the_right_gating(connected):
    workspace_id, _reader, _writer = connected
    session, _context, tools = _tools(workspace_id)
    try:
        assert set(tools) == {
            "list_connections",
            "describe_schema",
            "sql_query",
            "sql_execute",
        }
        assert tools["sql_query"].read_only is True
        assert tools["sql_execute"].read_only is False
        assert tools["sql_execute"].preview is not None
    finally:
        session.close()


def test_registry_is_empty_without_connections():
    session = SessionLocal()
    try:
        context = ToolContext(
            workspace_id="workspace-with-nothing", user_id="u", conversation_id="c"
        )
        assert registry_tools(session, context) == {}
    finally:
        session.close()


def test_list_connections_tool_reports_both_rows(connected):
    workspace_id, _reader, _writer = connected
    session, context, tools = _tools(workspace_id)
    try:
        payload = json.loads(tools["list_connections"].executor(session, context, {}).content)
    finally:
        session.close()
    names = {item["name"]: item for item in payload["connections"]}
    assert names["analytics"]["read_only"] is True
    assert names["staging"]["read_only"] is False


def test_sql_query_tool_addresses_a_connection_by_name(connected):
    workspace_id, _reader, _writer = connected
    session, context, tools = _tools(workspace_id)
    try:
        result = tools["sql_query"].executor(
            session,
            context,
            {"connection": "ANALYTICS", "sql": "SELECT COUNT(*) AS n FROM orders"},
        )
    finally:
        session.close()
    payload = json.loads(result.content)
    assert payload["columns"] == ["n"]
    assert payload["rows"] == [[500]]


def test_unknown_connection_name_lists_the_real_ones(connected):
    workspace_id, _reader, _writer = connected
    session, context, tools = _tools(workspace_id)
    try:
        result = tools["sql_query"].executor(
            session, context, {"connection": "warehouse", "sql": "SELECT 1"}
        )
    finally:
        session.close()
    assert result.content.startswith("Error:")
    assert "analytics" in result.content and "staging" in result.content


def test_ambiguous_omitted_connection_asks_for_a_name(connected):
    workspace_id, _reader, _writer = connected
    session, context, tools = _tools(workspace_id)
    try:
        result = tools["describe_schema"].executor(session, context, {})
    finally:
        session.close()
    assert "analytics" in result.content and "staging" in result.content


def test_single_connection_workspaces_may_omit_the_connection(solo):
    workspace_id, connection_id = solo
    session, context, tools = _tools(workspace_id)
    try:
        assert resolve(session, workspace_id).id == connection_id
        payload = json.loads(
            tools["sql_query"].executor(session, context, {"sql": "SELECT 1 AS one"}).content
        )
        assert payload["rows"] == [[1]]
    finally:
        session.close()


def test_sql_query_tool_never_raises_on_a_bad_statement(connected):
    workspace_id, reader, _ = connected
    session, context, tools = _tools(workspace_id)
    try:
        result = tools["sql_query"].executor(
            session, context, {"connection": reader, "sql": "DROP TABLE customers"}
        )
    finally:
        session.close()
    assert result.content.startswith("Error:")
    assert "DROP" in result.content


def test_a_nonsense_limit_becomes_the_default_instead_of_raising(connected):
    """json.loads accepts NaN/Infinity, and int() refuses both — tools must not."""
    workspace_id, reader, _ = connected
    session, context, tools = _tools(workspace_id)
    try:
        for bad in (float("nan"), float("inf"), float("-inf")):
            result = tools["sql_query"].executor(
                session,
                context,
                {"connection": reader, "sql": "SELECT 1 AS n", "limit": bad},
            )
            assert json.loads(result.content)["rows"] == [[1]]
    finally:
        session.close()


def test_sql_execute_preview_warns_and_refuses_read_only_connections(connected):
    workspace_id, reader, _ = connected
    session, context, tools = _tools(workspace_id)
    try:
        preview = tools["sql_execute"].preview(
            session, context, {"connection": reader, "sql": "DELETE FROM customers"}
        )
    finally:
        session.close()
    assert "read-only" in preview
