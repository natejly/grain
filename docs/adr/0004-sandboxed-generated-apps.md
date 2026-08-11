# 0004 — Sandboxed generated apps

## Status

Accepted. Supersedes the generated-app half of ADR 0003; its analytics half
stands. Amended by ADR 0005, which adds a server-side execution sandbox beside
this rendering one.

## Context

ADR 0003 chose static declarative snapshots ("no supplied HTML or JavaScript is
executed") because arbitrary code execution with workspace authority was an
unacceptable risk. The product now needs LLM-generated mini-apps ("vibecoded"
frontends over the user's data), which requires executing generated HTML/JS
somewhere.

## Decision

Generated code executes only in a boundary that carries no workspace authority:

- One self-contained HTML file per release, stored immutably in
  `AppRelease.manifest_json` (schema_version 2, `kind: "code"`), SHA-256
  content-hashed and capped at 256 KB, with a defense-in-depth lint that
  rejects external `src`/`href` references and `http-equiv` overrides.
- Served as a document from dedicated frame routes
  (`/api/apps/{id}/releases/{id}/frame`, `/published/apps/{slug}/frame`) with
  `Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline';
  style-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action
  'none'; base-uri 'none'; frame-ancestors <web origins>`.
- Embedded via `<iframe sandbox="allow-scripts">` without `allow-same-origin`:
  the code runs in an opaque origin with no cookies and no parent DOM access,
  and the CSP removes all network access.
- Data crosses one postMessage protocol (`jasmine:init/ready/query/result`).
  The host also answers the pre-rename `fieldnote:*` namespace, because a
  published release freezes its runtime into the stored HTML and every
  snapshot cut before the rename speaks the old names permanently; the
  injected runtime likewise aliases `window.fieldnote` to `window.jasmine`.
  The host validates `event.source`, checks requested datasets against the
  release's declared `data_bindings`, and forwards queries to the existing
  typed DatasetQuery engine — the server remains the enforcement point.
- Published code apps ship with query-result snapshots baked into the manifest
  at generation time; live queries are workspace-preview only.
- Publishing stays an explicit, owner-only human action; generation only ever
  creates drafts.

## Consequences

- `'unsafe-inline'` script is required inside the frame; acceptable because the
  origin is opaque and network-less — exfiltration and session theft have no
  channel.
- The postMessage host (`apps/web/components/sandbox-frame.tsx`) is the new
  security-critical surface and must stay small and validated.
- Residual risks: CPU-burn inside the frame (user closes the tab), UI spoofing
  inside the frame (mitigated by the host-rendered "sandboxed" badge outside
  it), and prompt-injected dataset content steering codegen (only schemas and
  capped sample rows reach the prompt).
