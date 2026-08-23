"use client";

import { KeyRound, Plus, Trash2 } from "lucide-react";
import type { SandboxSecret, SandboxSecretInput } from "@workspace/api-client";
import { FormEvent, useState } from "react";

export type SandboxSecretsViewProps = {
  secrets: SandboxSecret[];
  addSecret: (input: SandboxSecretInput) => Promise<void>;
  removeSecret: (secret: SandboxSecret) => Promise<void>;
};

/**
 * The shape the sandbox requires of a secret name: an environment variable —
 * UPPERCASE, starting with a letter, digits and underscores after. Mirrors the
 * backend's `NAME_RE` so a bad name is caught at the field it came from rather
 * than surfacing as a 400 later. The backend stays authoritative: it also
 * refuses names the policy environment owns (`GRAIN_*`, `MPLBACKEND`, …), and
 * that refusal arrives as a legible error if one slips past this check.
 */
export function isValidSecretName(name: string): boolean {
  return /^[A-Z][A-Z0-9_]{0,127}$/.test(name.trim());
}

/** One line of provenance for a secret's card — it never carries the value. */
export function describeSecretMeta(secret: SandboxSecret): string {
  const date = new Date(secret.created_at);
  const when = Number.isNaN(date.getTime())
    ? secret.created_at
    : date.toLocaleDateString();
  return `Added ${when}`;
}

function AddSecretForm({
  onSubmit,
  onCancel,
  setError,
}: {
  onSubmit: (input: SandboxSecretInput) => Promise<void>;
  onCancel: () => void;
  setError: (message: string) => void;
}) {
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const cleanName = name.trim();
    // Validate the name here so a typo points at the field, not an opaque 400.
    if (!isValidSecretName(cleanName)) {
      setError(
        "A name must be UPPERCASE letters, digits and underscores, starting " +
          "with a letter — e.g. STRIPE_API_KEY.",
      );
      return;
    }
    if (!value) {
      setError("A secret needs a value.");
      return;
    }
    setBusy(true);
    try {
      await onSubmit({ name: cleanName, value });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="mcp-form" onSubmit={(event) => void submit(event)}>
      <label>
        Name
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="STRIPE_API_KEY"
          required
        />
      </label>

      <label>
        Value
        <textarea
          value={value}
          onChange={(event) => setValue(event.target.value)}
          rows={3}
          placeholder="The credential the sandbox code will read."
          required
        />
      </label>

      <div className="mcp-form-actions">
        <button type="button" className="ghost-button" onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className="primary-button" disabled={busy}>
          Save secret
        </button>
      </div>
    </form>
  );
}

export function SandboxSecretsView({
  secrets,
  addSecret,
  removeSecret,
}: SandboxSecretsViewProps) {
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState("");

  return (
    <div className="content-page">
      <div className="page-heading">
        <h1>Sandbox secrets</h1>
        {!adding && (
          <button className="primary-button" onClick={() => setAdding(true)}>
            <Plus size={15} /> Add secret
          </button>
        )}
      </div>

      <p className="mcp-hint">
        Credentials the sandbox code can read as environment variables — the way
        to connect your generated code to a service. A value is written once and
        encrypted at rest; it is never shown again, and it can only leave the
        sandbox when the workspace&rsquo;s network policy allows outbound access.
        Setting or removing one is an owner action.
      </p>

      {error && <div className="mcp-error">{error}</div>}

      {adding && (
        <AddSecretForm
          setError={setError}
          onCancel={() => {
            setError("");
            setAdding(false);
          }}
          onSubmit={async (input) => {
            setError("");
            await addSecret(input);
            setAdding(false);
          }}
        />
      )}

      {secrets.length === 0 && !adding ? (
        <div className="empty-state">
          <p>No sandbox secrets yet.</p>
        </div>
      ) : (
        <div className="mcp-list">
          {secrets.map((secret) => (
            <section key={secret.name} className="mcp-card">
              <header className="mcp-card-head">
                <div className="mcp-card-title">
                  <strong>
                    <code>{secret.name}</code>
                  </strong>
                  <span className="status-pill">
                    <KeyRound size={12} /> env var
                  </span>
                </div>
                <div className="mcp-card-actions">
                  <button
                    className="icon-button"
                    onClick={() => void removeSecret(secret)}
                    aria-label={`Remove ${secret.name}`}
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              </header>

              <div className="mcp-card-meta">{describeSecretMeta(secret)}</div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
