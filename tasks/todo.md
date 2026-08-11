# Reachability audit — finish the small items, then re-sweep

## Part 1

- [ ] 1. Delete `GET /api/apps/{id}/preview` + `api.previewApp` + `AppPreviewOut`/`AppPreview`
      and the three isolation-test route-table entries. Zero callers; the editor
      renders through the `/frame` iframe.
- [ ] 2. Point the Activity approvals panel at `AgentToolCall` (the real path).
      Also fix `pendingApprovals` in `use-workspace.ts`, which feeds the nav
      badge off the same legacy list — so the badge is permanently 0 too.
- [ ] 3. `User.email_verified_at`: claim is STALE. It is read at
      `apps/api/app/api/auth.py:670` (Google-link takeover gate) and at `:173`
      into `AuthSessionOut.email_verified`, which IS in the client type. Do not
      enforce at login; surface the state so it is observable.
- [ ] 4. Wire CSV/JSON re-upload to `createDatasetVersion`. Today the auto-create
      effect hits the 409 name conflict and swallows it, so re-upload is a silent
      no-op, not "a second unrelated dataset". Update ADR 0003.

## Part 2 — re-sweep (report only)

- [ ] routes with no client caller
- [ ] emitted-but-unconsumed RunEvent types
- [ ] inert-by-default settings
- [ ] columns written but never read
- [ ] doc drift

## Gates

ruff, mypy(108), pytest(1399/1/3), export_openapi, tsc, lint, vitest(251),
build, playwright(42) x2.

## Review

All four Part 1 items done; Part 2 swept and reported, not fixed.

1. Deleted. Route, `AppPreviewOut`, `AppPreview`, `previewApp`, three isolation
   route-table entries, and the OpenAPI block. `test_tenant_isolation` asserts
   routes↔cases both ways, so the deletion is verified rather than asserted.
2. Activity now reads `AgentToolCall` through the same `decideAgentCall` chat and
   Workflows use, reusing `ProposalDiff`. Also fixed `pendingApprovals`, which
   fed the Settings badge off the same dead list — the badge could never light.
   Dropped the frontend's now-unused legacy state and its two per-load fetches.
3. Claim was stale: the column IS read, at `auth.py` (Google account-linking
   gate) and into `AuthSessionOut.email_verified`, which is in the client type.
   Kept and did NOT enforce at login; reasoning recorded at `_issue_login`. The
   real gap was that nothing rendered it — the identity chip now does.
4. Wired re-upload to `createDatasetVersion`. The audit's "second unrelated
   dataset" was wrong: `POST /api/datasets` 409s on the name and the browser
   swallowed it, so re-upload was a silent no-op. New API test pins the whole
   sequence including that queries then read the correction. ADR 0003 gained a
   postscript naming the two things that append to the chain.

Also corrected README's approvals demo, which my change to (2) invalidated.

Gates: ruff clean, mypy 108, pytest 1398/1 skipped/3 xfailed (1399 baseline − 2
deleted route cases + 1 new test), openapi exported, tsc/lint clean, vitest 253
(251 + 2 CSS guards), build clean, playwright 42 twice. Injected-failure check:
reverting the panel failed exactly one test — this one — on both new assertions
independently.

Deferred, with sizing, in the report: the legacy Tool/ToolCall backend subsystem
(reaches the run executor and ~10 test files), and every Part 2 finding.
