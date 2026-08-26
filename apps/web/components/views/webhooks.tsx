"use client";

import type {
  ApiToken,
  InboundAddressRow,
  WebhookDelivery,
  WebhookEndpoint,
  WebhookEvent,
} from "@workspace/api-client";
import {
  Check,
  Copy,
  KeyRound,
  Mail,
  Plus,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import {
  WEBHOOK_EVENTS,
  deliveriesFor,
  deliveryLabel,
  deliveryTone,
  eventLabel,
  tokenState,
  tokenUseLabel,
} from "./webhook-format";
import { describeError } from "./shared";

/**
 * The machine surface, in one place: the bearer tokens external systems call
 * in with (`/api/hooks/...`) and the webhook endpoints workspace events are
 * pushed out to.
 *
 * Self-contained like CronsView — nothing here is anybody's business until
 * they open the settings page, so the lists are fetched on mount through the
 * `api` singleton (the members.tsx precedent) rather than at page load.
 *
 * Two raw-exactly-once rules meet here. A minted token's secret appears in
 * the 201 and never again — shown once with a copy button, like an invite
 * link. A webhook's signing secret is the inverse: *written* once and never
 * echoed, so a stored one is only ever the `secret stored` pill.
 */
export function WebhooksView({ setError }: { setError: (message: string) => void }) {
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [addresses, setAddresses] = useState<InboundAddressRow[]>([]);
  const [endpoints, setEndpoints] = useState<WebhookEndpoint[]>([]);
  const [deliveries, setDeliveries] = useState<WebhookDelivery[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [forbidden, setForbidden] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api.listApiTokens(),
      api.listInboundAddresses(),
      api.listWebhooks(),
      api.listWebhookDeliveries(),
    ])
      .then(([tokenRows, addressRows, endpointRows, deliveryRows]) => {
        if (cancelled) return;
        setTokens(tokenRows);
        setAddresses(addressRows);
        setEndpoints(endpointRows);
        setDeliveries(deliveryRows);
        setLoaded(true);
      })
      .catch((caught) => {
        if (cancelled) return;
        // The whole surface is owner-gated; a member who finds the page gets
        // told why it is empty rather than a toast full of 403s.
        setForbidden(true);
        setLoaded(true);
        const message = describeError(caught, "");
        if (message && !/owner/i.test(message)) setError(message);
      });
    return () => {
      cancelled = true;
    };
  }, [setError]);

  async function refreshDeliveries() {
    try {
      setDeliveries(await api.listWebhookDeliveries());
    } catch {
      // The trail is a convenience; a failed refresh keeps the stale rows.
    }
  }

  async function redeliver(deliveryId: string) {
    try {
      await api.redeliverWebhookDelivery(deliveryId);
      await refreshDeliveries();
    } catch (caught) {
      setError(describeError(caught, "Could not requeue the delivery"));
    }
  }

  if (loaded && forbidden) {
    return (
      <div className="content-page">
        <div className="page-heading">
          <h1>API &amp; Webhooks</h1>
        </div>
        <div className="empty-state">
          <p>Only a workspace owner can manage API tokens and webhooks.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="content-page">
      <div className="page-heading">
        <h1>API &amp; Webhooks</h1>
      </div>

      <TokensSection tokens={tokens} setTokens={setTokens} setError={setError} />

      <InboundAddressesSection
        addresses={addresses}
        setAddresses={setAddresses}
        setError={setError}
      />

      <EndpointsSection
        endpoints={endpoints}
        setEndpoints={setEndpoints}
        setError={setError}
        onChanged={() => void refreshDeliveries()}
      />

      <DeliveriesSection
        deliveries={deliveries}
        endpoints={endpoints}
        onRedeliver={(id) => void redeliver(id)}
      />
    </div>
  );
}

function TokensSection({
  tokens,
  setTokens,
  setError,
}: {
  tokens: ApiToken[];
  setTokens: (update: (rows: ApiToken[]) => ApiToken[]) => void;
  setError: (message: string) => void;
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  /** The just-minted secret — the one time it exists on this side. */
  const [minted, setMinted] = useState("");
  const [copied, setCopied] = useState(false);

  async function mint(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      const created = await api.createApiToken(name.trim());
      const { secret, ...row } = created;
      setTokens((rows) => [...rows, row]);
      setMinted(secret);
      setCopied(false);
      setName("");
    } catch (caught) {
      setError(describeError(caught, "Could not create the token"));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(token: ApiToken) {
    try {
      await api.revokeApiToken(token.id);
      const stamp = new Date().toISOString();
      setTokens((rows) =>
        rows.map((row) =>
          row.id === token.id ? { ...row, revoked_at: stamp } : row,
        ),
      );
    } catch (caught) {
      setError(describeError(caught, "Could not revoke the token"));
    }
  }

  async function copy() {
    try {
      await navigator.clipboard?.writeText(minted);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <section className="mcp-card">
      <header className="mcp-card-head">
        <div className="mcp-card-title">
          <strong>API tokens</strong>
        </div>
      </header>
      <p className="field-hint">
        A token lets an external system trigger workflows and post notes into
        threads as you, over <code>/api/hooks</code>. It carries your access —
        revoke it the moment it stops being needed. These are the same tokens
        the MCP page manages; minting or revoking in either place applies to
        both.
      </p>

      <form className="mcp-form-row" onSubmit={(event) => void mint(event)}>
        <label>
          Token name
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="e.g. CI pipeline"
            required
          />
        </label>
        <button type="submit" className="primary-button" disabled={busy}>
          <KeyRound size={14} /> {busy ? "Minting…" : "Create token"}
        </button>
      </form>

      {minted && (
        <div className="invite-link" role="status">
          <p className="field-hint">
            This secret is shown once and cannot be read back — copy it now.
          </p>
          <div className="invite-link-row">
            <code>{minted}</code>
            <button
              type="button"
              className="ghost-button"
              aria-label="Copy API token"
              onClick={() => void copy()}
            >
              {copied ? <Check size={12} /> : <Copy size={12} />}
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        </div>
      )}

      {tokens.length === 0 ? (
        <p className="section-note">No tokens yet — nothing can call in.</p>
      ) : (
        <ul className="share-link-list">
          {tokens.map((token) => {
            const state = tokenState(token);
            return (
              <li key={token.id}>
                <div>
                  <span className="admin-tag">{state}</span>
                  <span className="share-link-meta">
                    {token.name || "Unnamed token"} · {tokenUseLabel(token)}
                  </span>
                </div>
                {state === "active" && (
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={() => void revoke(token)}
                  >
                    <X size={12} /> Revoke
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function InboundAddressesSection({
  addresses,
  setAddresses,
  setError,
}: {
  addresses: InboundAddressRow[];
  setAddresses: (
    update: (rows: InboundAddressRow[]) => InboundAddressRow[],
  ) => void;
  setError: (message: string) => void;
}) {
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  /** The just-minted address — the one time it exists on this side. */
  const [minted, setMinted] = useState("");
  const [copied, setCopied] = useState(false);

  async function mint(event: FormEvent) {
    event.preventDefault();
    if (!label.trim()) return;
    setBusy(true);
    try {
      const created = await api.createInboundAddress(label.trim());
      const { address, ...row } = created;
      setAddresses((rows) => [...rows, row]);
      setMinted(address);
      setCopied(false);
      setLabel("");
    } catch (caught) {
      setError(describeError(caught, "Could not create the address"));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(row: InboundAddressRow) {
    try {
      const updated = await api.revokeInboundAddress(row.id);
      setAddresses((rows) =>
        rows.map((item) => (item.id === updated.id ? updated : item)),
      );
    } catch (caught) {
      setError(describeError(caught, "Could not revoke the address"));
    }
  }

  async function copy() {
    try {
      await navigator.clipboard?.writeText(minted);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <section className="mcp-card">
      <header className="mcp-card-head">
        <div className="mcp-card-title">
          <strong>Email in</strong>
        </div>
      </header>
      <p className="field-hint">
        Mail sent to a minted address lands as a new personal thread of yours
        — nothing runs on its account until you reply. Only the mail&apos;s
        text lands: attachments are dropped. The address is the secret:
        revoke it if it leaks.
      </p>

      <form className="mcp-form-row" onSubmit={(event) => void mint(event)}>
        <label>
          Address label
          <input
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder="e.g. Support inbox"
            required
          />
        </label>
        <button type="submit" className="primary-button" disabled={busy}>
          <Mail size={14} /> {busy ? "Minting…" : "Create address"}
        </button>
      </form>

      {minted && (
        <div className="invite-link" role="status">
          <p className="field-hint">
            This address is shown once and cannot be read back — copy it now.
          </p>
          <div className="invite-link-row">
            <code>{minted}</code>
            <button
              type="button"
              className="ghost-button"
              aria-label="Copy inbound address"
              onClick={() => void copy()}
            >
              {copied ? <Check size={12} /> : <Copy size={12} />}
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        </div>
      )}

      {addresses.length === 0 ? (
        <p className="section-note">No addresses yet — no mail can land.</p>
      ) : (
        <ul className="share-link-list">
          {addresses.map((row) => {
            const state = tokenState(row);
            return (
              <li key={row.id}>
                <div>
                  <span className="admin-tag">{state}</span>
                  <span className="share-link-meta">
                    {row.label || "Unnamed address"} · created{" "}
                    {new Date(row.created_at).toLocaleDateString()}
                  </span>
                </div>
                {state === "active" && (
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={() => void revoke(row)}
                  >
                    <X size={12} /> Revoke
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function EndpointsSection({
  endpoints,
  setEndpoints,
  setError,
  onChanged,
}: {
  endpoints: WebhookEndpoint[];
  setEndpoints: (
    update: (rows: WebhookEndpoint[]) => WebhookEndpoint[],
  ) => void;
  setError: (message: string) => void;
  onChanged: () => void;
}) {
  const [adding, setAdding] = useState(false);

  async function setEnabled(endpoint: WebhookEndpoint, enabled: boolean) {
    try {
      const updated = await api.updateWebhook(endpoint.id, { enabled });
      setEndpoints((rows) =>
        rows.map((row) => (row.id === updated.id ? updated : row)),
      );
    } catch (caught) {
      setError(describeError(caught, "Could not update the webhook"));
    }
  }

  async function toggleEvent(endpoint: WebhookEndpoint, event: WebhookEvent) {
    const next = endpoint.events.includes(event)
      ? endpoint.events.filter((item) => item !== event)
      : [...endpoint.events, event];
    try {
      const updated = await api.updateWebhook(endpoint.id, { events: next });
      setEndpoints((rows) =>
        rows.map((row) => (row.id === updated.id ? updated : row)),
      );
    } catch (caught) {
      setError(describeError(caught, "Could not update the webhook"));
    }
  }

  async function remove(endpoint: WebhookEndpoint) {
    try {
      await api.deleteWebhook(endpoint.id);
      setEndpoints((rows) => rows.filter((row) => row.id !== endpoint.id));
      onChanged();
    } catch (caught) {
      setError(describeError(caught, "Could not delete the webhook"));
    }
  }

  return (
    <section className="mcp-card">
      <header className="mcp-card-head">
        <div className="mcp-card-title">
          <strong>Webhooks</strong>
        </div>
        <div className="mcp-card-actions">
          {!adding && (
            <button className="primary-button" onClick={() => setAdding(true)}>
              <Plus size={15} /> Add endpoint
            </button>
          )}
        </div>
      </header>
      <p className="field-hint">
        Workspace events — runs finishing, approvals parking, monitors
        tripping — are POSTed to each enabled URL. Payloads carry ids and
        titles only, never message content. Each delivery is signed:{" "}
        <code>X-Grain-Signature: t=&lt;unix&gt;,v1=&lt;hex&gt;</code> where{" "}
        <code>v1</code> is HMAC-SHA256 of{" "}
        <code>{"<t>.<raw body>"}</code> under your secret — verify it
        constant-time and reject a stale <code>t</code> to stop replays.
      </p>

      {adding && (
        <AddEndpointForm
          onCancel={() => setAdding(false)}
          onCreated={(endpoint) => {
            setEndpoints((rows) => [...rows, endpoint]);
            setAdding(false);
          }}
          setError={setError}
        />
      )}

      {endpoints.length === 0 && !adding ? (
        <p className="section-note">No endpoints yet — no events leave the workspace.</p>
      ) : (
        <div className="mcp-list">
          {endpoints.map((endpoint) => (
            <section key={endpoint.id} className="mcp-card">
              <header className="mcp-card-head">
                <div className="mcp-card-title">
                  <strong>{endpoint.name || endpoint.url}</strong>
                  {!endpoint.enabled && <span className="status-pill">disabled</span>}
                </div>
                <div className="mcp-card-actions">
                  <button
                    className="ghost-button"
                    onClick={() => void setEnabled(endpoint, !endpoint.enabled)}
                  >
                    {endpoint.enabled ? <X size={14} /> : <Check size={14} />}
                    {endpoint.enabled ? "Disable" : "Enable"}
                  </button>
                  <button
                    className="icon-button"
                    onClick={() => void remove(endpoint)}
                    aria-label={`Delete webhook ${endpoint.name || endpoint.url}`}
                    title="Delete webhook"
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              </header>
              <div className="mcp-card-meta">
                {endpoint.url}
                {endpoint.has_secret && (
                  <span className="secret-pill">secret stored</span>
                )}
              </div>
              <ul className="mcp-tools">
                {WEBHOOK_EVENTS.map(({ event, label }) => (
                  <li key={event}>
                    <label className="mcp-tool">
                      <input
                        type="checkbox"
                        checked={endpoint.events.includes(event)}
                        onChange={() => void toggleEvent(endpoint, event)}
                      />
                      <code>{event}</code>
                      <span>{label}</span>
                    </label>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}
    </section>
  );
}

function AddEndpointForm({
  onCreated,
  onCancel,
  setError,
}: {
  onCreated: (endpoint: WebhookEndpoint) => void;
  onCancel: () => void;
  setError: (message: string) => void;
}) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [events, setEvents] = useState<WebhookEvent[]>(["run.completed"]);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const created = await api.createWebhook({
        name: name.trim(),
        url: url.trim(),
        events,
        secret,
      });
      onCreated(created);
    } catch (caught) {
      setError(describeError(caught, "Could not create the webhook"));
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
            placeholder="e.g. Ops channel bridge"
          />
        </label>
        <label>
          URL (HTTPS)
          <input
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="https://example.com/hooks/grain"
            required
          />
        </label>
      </div>
      <label>
        Signing secret (optional — stored encrypted, never shown again)
        <input
          value={secret}
          onChange={(event) => setSecret(event.target.value)}
          type="password"
          autoComplete="off"
        />
      </label>
      <ul className="mcp-tools">
        {WEBHOOK_EVENTS.map(({ event: hook, label }) => (
          <li key={hook}>
            <label className="mcp-tool">
              <input
                type="checkbox"
                checked={events.includes(hook)}
                onChange={() =>
                  setEvents((current) =>
                    current.includes(hook)
                      ? current.filter((item) => item !== hook)
                      : [...current, hook],
                  )
                }
              />
              <code>{hook}</code>
              <span>{label}</span>
            </label>
          </li>
        ))}
      </ul>
      <div className="mcp-form-actions">
        <button type="button" className="ghost-button" onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className="primary-button" disabled={busy}>
          Add endpoint
        </button>
      </div>
    </form>
  );
}

function DeliveriesSection({
  deliveries,
  endpoints,
  onRedeliver,
}: {
  deliveries: WebhookDelivery[];
  endpoints: WebhookEndpoint[];
  onRedeliver: (deliveryId: string) => void;
}) {
  const names = new Map(
    endpoints.map((endpoint) => [endpoint.id, endpoint.name || endpoint.url]),
  );
  const recent = deliveriesFor(deliveries);
  return (
    <section className="mcp-card">
      <header className="mcp-card-head">
        <div className="mcp-card-title">
          <strong>Recent deliveries</strong>
        </div>
      </header>
      {recent.length === 0 ? (
        <p className="section-note">Nothing has been sent yet.</p>
      ) : (
        <ul className="share-link-list">
          {recent.map((delivery) => (
            <li key={delivery.id}>
              <div>
                <span className={`status-pill ${deliveryTone(delivery)}`}>
                  {delivery.status}
                </span>
                <span className="share-link-meta">
                  {eventLabel(delivery.event)} →{" "}
                  {names.get(delivery.endpoint_id) ?? "removed endpoint"} ·{" "}
                  {deliveryLabel(delivery)}
                </span>
              </div>
              {delivery.status === "failed" && (
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => onRedeliver(delivery.id)}
                >
                  <RotateCcw size={12} /> Redeliver
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
