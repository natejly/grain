# Security audit + rate limiting (bg/security-audit, 2026-08-25)

## Plan
- [x] Recon: auth architecture, existing limiter (auth-only, per-IP fixed window)
- [ ] Parallel audit: public surface, rate-limit gaps, injection/SSRF, secrets/crypto, frontend, IDOR
- [ ] Triage findings by severity; confirm each against code before fixing
- [ ] Implement general rate limiting:
  - [ ] Reuse `services/auth/ratelimit.RateLimiter` as the engine
  - [ ] Tiered limits (public token endpoints, expensive/LLM endpoints, general write, bearer-token guessing)
  - [ ] Settings knobs following `auth_rate_limit_*` naming; 429 + Retry-After
  - [ ] Tests; ensure existing suite unaffected (reset between tests)
- [ ] Fix confirmed high-severity audit findings (minimal-impact changes)
- [ ] Run api tests + web tests + lint; commit and push

## Review

Six parallel audits ran (public surface, rate-limit gaps, injection/SSRF, secrets/crypto,
frontend, IDOR). IDOR came back clean; SSRF/SQLi/command-exec are well-guarded. Confirmed
findings and what was done:

### Fixed
- **HIGH — LaTeX path traversal → arbitrary host file write** (`services/projects/compile.py`).
  Per-file `path` was staged as `tmpdir / path` with no containment check. Now every path is
  normalized through `store.normalize_path` (rejects absolute/`..`/backslash) and `_write_files`
  re-asserts containment against the resolved root. Tests added.
- **Rate limiting (the ask)** — new `app/api/ratelimit.py`: reuses the auth `RateLimiter` engine,
  adds per-identity (`rate_limit`), per-token (`token_rate_limit`), and per-IP (`public_rate_limit`)
  dependencies with three tiers (heavy / mint / public) configured in `config.py`. Applied to
  20 high-cost routes: chat send+edit, workflow run/compile/tick, graph rebuild, app generate,
  sandbox create/run, source ingest, latex compile, api-token + share-link mint, and the public
  doors (shared/{token}, published apps, hooks, MCP, inbound email). Coverage tripwire test
  (`test_rate_limit.py`) fails if any of them loses its limiter. 429 carries Retry-After.
  Autouse conftest fixture resets the limiter between tests.
- **MEDIUM — sandbox secrets on docker argv** (`services/sandbox/container_provider.py`).
  Was `-e KEY=value` (world-readable via `ps`/`/proc/cmdline`); now name-only `-e KEY` with the
  value supplied through the docker CLI's own environment (owner-readable only). Test updated.
- **MEDIUM/HIGH — stored XSS via "Open original"** (`web/components/views/sources.tsx`).
  Blob nav opened SVG/HTML as same-origin active documents. New `viewableBlob` allowlists inert
  types (PDF, raster) and re-wraps everything else as octet-stream. Test added.
- **MEDIUM — published app frame CSP** (`api/generated_apps.py`). Added `sandbox allow-scripts`
  so a direct top-level visit gets an opaque origin, not the API origin.
- **LOW — localhost CORS survives to prod** (`config.py`). New `_guard_web_origin` refuses a
  localhost-only WEB_ORIGIN outside dev/test. Test added.
- **LOW — non-ASCII Authorization 500s** (workflows tick, inbound email). Compare as bytes.

### Noted, not changed (larger scope, recommend as follow-ups)
- Share links never expire (`expires_at` supported by service, not passed by route).
- Inbound email has no per-message provider signature / replay protection (only a static bearer).
- Fernet key has no rotation path (single key; consider MultiFernet).
- Outbound webhook HMAC signs body only (no timestamp → replayable). `/health` unauthenticated DB hit.
- Pre-existing: `api_tokens.router` is included twice in `main.py`; `services/harness/__init__.py`
  has a pre-existing mypy dict-item error (both untouched by this branch).

### Verification
- Full API suite: exit 0, ~2500 tests, 0 failures. Web suite: 742 passed. Web typecheck + lint clean.
- ruff clean on all changed files; mypy clean on the 3 changed non-test modules.
