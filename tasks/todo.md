# Program: close the QM feature gap (deployment deferred)

Built incrementally in the feat/session-79 worktree, each increment verified
(ruff/mypy/pytest via the worktree venv + tsc/lint/vitest/build) before the next.

## Wave 1 — Foundations (unblock the rest)
- [ ] **Multi-harness execution** — abstract the agent loop behind a `Harness`
      interface (`services/harness/`), a provider/harness registry, and
      `HARNESS`/provider config. Keep OpenAI as one harness; add a second
      (Anthropic) to prove the seam. Unblocks per-turn controls + multi-provider.
- [ ] **Shared scopes (multiplayer)** — extend workspace/membership into shared
      collaborative scopes alongside personal ones: scope-scoped files/memory/
      sessions, membership + permissions, a scope switcher. Foundation for shared skills.

## Wave 2 — On the foundations
- [ ] **Per-turn UI controls** — model selector, effort selector (low…max), Fast
      mode toggle in the composer; threaded through the run request.
- [ ] **Shared skills** — a `Skill` entity (authored markdown + optional args),
      scope-owned and shareable (admin-approved), content-hash versioned, with a
      slash-command skill picker in the composer.
- [ ] **Sandbox tools** — tool descriptors (name/hints/egress/approval), per-tool
      egress allowlist, per-tool approval tightening, registered into the tool registry.

## Wave 3 — Cross-cutting
- [ ] **Automation** — personal crons (task vs message) plus event watches /
      monitors / wake triggers, on the existing scheduler.
- [ ] **Security** — prompt-injection classifier (built-in + pluggable screen),
      three postures (Strict/Auto/Dangerous), sandbox egress-authorization.
- [ ] **Observability** — capture TTFT/latency/tokens per run; admin analytics
      (latency percentiles, throughput, retention DAU/WAU/MAU, live runs, errors).

## Wave 4 — Surfaces
- [ ] **Multi-pane shell** — concurrent sessions in a docking/split layout.
- [ ] **Portal / onboarding** — SSO front door, playground (anonymous try-it),
      email one-time-link auth broker.

## Deferred (per request)
- Deployment CLI / portable directory / Fly+AWS targets, drift verification, OIDC deploy trust.
