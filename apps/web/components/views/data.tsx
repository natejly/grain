"use client";

import {
  ChevronDown,
  ChevronRight,
  Database,
  KeyRound,
  Plus,
  Table2,
  Trash2,
  Zap,
} from "lucide-react";
import { FormEvent, useRef, useState } from "react";

export type DbEngine = "postgres" | "mysql" | "sqlite" | "duckdb";

export type DbConnection = {
  id: string;
  name: string;
  engine: string;
  read_only: boolean;
  status: string;
  last_error: string;
  /** The DSN with its password replaced — the real one is never returned. */
  dsn_summary: string;
  created_at: string;
};

export type DbConnectionInput = {
  name: string;
  engine: DbEngine;
  dsn: string;
  read_only: boolean;
};

export type DbColumn = {
  name: string;
  type: string;
  nullable: boolean;
  primary_key: boolean;
};

export type DbForeignKey = {
  columns: string[];
  references: string;
  referred_columns: string[];
};

export type DbTable = {
  name: string;
  schema_name: string;
  /** False on large databases, where columns arrive only when asked for by name. */
  columns_loaded: boolean;
  columns: DbColumn[];
  columns_omitted: number;
  foreign_keys: DbForeignKey[];
};

export type DbSchema = {
  connection_id: string;
  connection: string;
  engine: string;
  table_count: number;
  tables: DbTable[];
  tables_omitted: number;
  note: string;
};

export type DataViewProps = {
  connections: DbConnection[];
  addConnection: (input: DbConnectionInput) => Promise<void>;
  testConnection: (connectionId: string) => Promise<void>;
  removeConnection: (connection: DbConnection) => Promise<void>;
  loadSchema: (connectionId: string, table?: string) => Promise<DbSchema>;
};

const ENGINE_LABELS: Record<DbEngine, string> = {
  postgres: "PostgreSQL",
  mysql: "MySQL / MariaDB",
  sqlite: "SQLite file",
  duckdb: "DuckDB file",
};

const DSN_PLACEHOLDERS: Record<DbEngine, string> = {
  postgres: "postgresql://user:password@host:5432/dbname",
  mysql: "mysql://user:password@host:3306/dbname",
  sqlite: "/absolute/path/to/database.db",
  duckdb: "/absolute/path/to/warehouse.duckdb",
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Schema-qualified name: what the API wants back and what makes a unique key. */
function qualify(table: DbTable): string {
  return table.schema_name ? `${table.schema_name}.${table.name}` : table.name;
}

function AddConnectionForm({
  onSubmit,
  onCancel,
}: {
  onSubmit: (input: DbConnectionInput) => Promise<void>;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [engine, setEngine] = useState<DbEngine>("postgres");
  const [dsn, setDsn] = useState("");
  const [readOnly, setReadOnly] = useState(true);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await onSubmit({
        name: name.trim(),
        engine,
        dsn: dsn.trim(),
        read_only: readOnly,
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="mcp-form" onSubmit={(event) => void submit(event)}>
      <div className="mcp-form-row">
        <label>
          Name
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="analytics"
            required
          />
          <span className="field-hint">
            The agent refers to this database by name in its tools.
          </span>
        </label>
        <label>
          Engine
          <select
            value={engine}
            onChange={(event) => setEngine(event.target.value as DbEngine)}
          >
            {Object.entries(ENGINE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <label>
        Connection string
        <input
          value={dsn}
          onChange={(event) => setDsn(event.target.value)}
          placeholder={DSN_PLACEHOLDERS[engine]}
          required
        />
        <span className="field-hint">
          Encrypted at rest and never read back — only a redacted summary is shown.
        </span>
      </label>

      <label className="db-toggle">
        <input
          type="checkbox"
          checked={readOnly}
          onChange={(event) => setReadOnly(event.target.checked)}
        />
        Read-only — the agent may query but never write
        <span className="field-hint">
          Unchecking lets the agent propose writes, which you approve one statement
          at a time. Point the connection at a least-privilege database user either way.
        </span>
      </label>

      <div className="mcp-form-actions">
        <button type="button" className="ghost-button" onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className="primary-button" disabled={busy}>
          Add connection
        </button>
      </div>
    </form>
  );
}

function TableRow({
  table,
  expanded,
  onToggle,
}: {
  table: DbTable;
  expanded: boolean;
  onToggle: () => void;
}) {
  const label = qualify(table);
  return (
    <li>
      <button className="schema-table" onClick={onToggle} aria-expanded={expanded}>
        {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <Table2 size={13} />
        <code>{label}</code>
        {table.columns_loaded && (
          <span className="schema-count">{table.columns.length} columns</span>
        )}
      </button>

      {expanded && table.columns.length > 0 && (
        <ul className="schema-columns">
          {table.columns.map((column) => (
            <li key={column.name} className="schema-column">
              {column.primary_key && <KeyRound size={11} />}
              <code>{column.name}</code>
              <span>{column.type}</span>
              {!column.nullable && <em>not null</em>}
            </li>
          ))}
          {table.columns_omitted > 0 && (
            <li className="schema-column">
              <span>+{table.columns_omitted} more columns</span>
            </li>
          )}
          {table.foreign_keys.map((key, index) => (
            <li key={`fk-${index}`} className="schema-column">
              <code>{key.columns.join(", ")}</code>
              <span>
                → {key.references}({key.referred_columns.join(", ")})
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function DataView({
  connections,
  addConnection,
  testConnection,
  removeConnection,
  loadSchema,
}: DataViewProps) {
  const [adding, setAdding] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [schema, setSchema] = useState<DbSchema | null>(null);
  const [schemaError, setSchemaError] = useState("");
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  // Schema reads are slow enough to overtake each other when someone clicks
  // through connections; only the newest request may write to state.
  const ticket = useRef(0);

  async function test(connectionId: string) {
    setTesting(connectionId);
    try {
      await testConnection(connectionId);
    } finally {
      setTesting(null);
    }
  }

  async function browse(connectionId: string) {
    const mine = ++ticket.current;
    if (openId === connectionId) {
      setOpenId(null);
      return;
    }
    setOpenId(connectionId);
    setSchema(null);
    setSchemaError("");
    setExpanded(null);
    setLoading(true);
    try {
      const loaded = await loadSchema(connectionId);
      if (ticket.current !== mine) return;
      setSchema(loaded);
    } catch (error) {
      // An unreachable database is an ordinary state here, not a crash.
      if (ticket.current === mine) setSchemaError(message(error));
    } finally {
      if (ticket.current === mine) setLoading(false);
    }
  }

  /** Expand a table, fetching its columns when the list arrived as names only. */
  async function toggleTable(connectionId: string, table: DbTable) {
    const name = qualify(table);
    if (expanded === name) {
      setExpanded(null);
      return;
    }
    setExpanded(name);
    if (table.columns_loaded) return;
    const mine = ticket.current;
    try {
      const detail = await loadSchema(connectionId, name);
      const loaded = detail.tables[0];
      if (!loaded || ticket.current !== mine) return;
      setSchema((current) =>
        current === null
          ? current
          : {
              ...current,
              tables: current.tables.map((item) =>
                qualify(item) === name ? loaded : item,
              ),
            },
      );
    } catch (error) {
      if (ticket.current === mine) setSchemaError(message(error));
    }
  }

  return (
    <div className="content-page">
      <div className="page-heading">
        <h1>Databases</h1>
        {!adding && (
          <button className="primary-button" onClick={() => setAdding(true)}>
            <Plus size={15} /> Add connection
          </button>
        )}
      </div>

      {adding && (
        <AddConnectionForm
          onCancel={() => setAdding(false)}
          onSubmit={async (input) => {
            await addConnection(input);
            setAdding(false);
          }}
        />
      )}

      {connections.length === 0 && !adding ? (
        <div className="empty-state">
          <Database size={22} />
          <p>
            No databases connected. Add one and the agent can inspect its schema and
            query it.
          </p>
        </div>
      ) : (
        <div className="mcp-list">
          {connections.map((connection) => (
            <section key={connection.id} className={`mcp-card ${connection.status}`}>
              <header className="mcp-card-head">
                <div className="mcp-card-title">
                  <strong>{connection.name}</strong>
                  <span className={`status-pill ${connection.status}`}>
                    {connection.status}
                  </span>
                  {connection.read_only && (
                    <span className="status-pill">read-only</span>
                  )}
                </div>
                <div className="mcp-card-actions">
                  <button
                    className="ghost-button"
                    onClick={() => void test(connection.id)}
                    disabled={testing === connection.id}
                  >
                    <Zap size={14} />
                    {testing === connection.id ? "Testing…" : "Test"}
                  </button>
                  <button
                    className="ghost-button"
                    onClick={() => void browse(connection.id)}
                  >
                    <Table2 size={14} />
                    {openId === connection.id ? "Hide schema" : "Browse schema"}
                  </button>
                  <button
                    className="icon-button"
                    onClick={() => void removeConnection(connection)}
                    aria-label={`Remove ${connection.name}`}
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              </header>

              <div className="mcp-card-meta">
                {connection.dsn_summary || connection.engine}
                <span className="secret-pill">password encrypted</span>
              </div>

              {connection.last_error && (
                <div className="mcp-error">{connection.last_error}</div>
              )}

              {openId === connection.id && (
                <div className="schema-browser">
                  {loading && <p className="mcp-hint">Reading schema…</p>}
                  {schemaError && <div className="mcp-error">{schemaError}</div>}
                  {schema && schema.tables.length === 0 && !loading && (
                    <p className="mcp-hint">This database has no tables yet.</p>
                  )}
                  {schema && schema.tables.length > 0 && (
                    <>
                      <ul className="schema-tables">
                        {schema.tables.map((table) => (
                          <TableRow
                            key={qualify(table)}
                            table={table}
                            expanded={expanded === qualify(table)}
                            onToggle={() => void toggleTable(connection.id, table)}
                          />
                        ))}
                      </ul>
                      <p className="mcp-hint">
                        {schema.table_count} tables
                        {schema.tables_omitted > 0 &&
                          ` — ${schema.tables_omitted} not shown`}
                        {schema.note && ` — ${schema.note}`}
                      </p>
                    </>
                  )}
                </div>
              )}
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
