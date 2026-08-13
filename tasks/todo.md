# Agent chat sidebar for projects and dashboards

Extend the document-thread pattern to two more subjects, scope each thread's
tools to its subject, and add a development-only unrestricted mode that cannot
be switched on outside development.

## 1. Polymorphic subject (backend)
- [ ] Migration 0038: `conversations.subject_kind` + `subject_id` replace
      `document_id` (backfill kind='document'); `runs.subject_focus` for the
      file the project editor has open. Verify chain from an empty DB, both ways.
- [ ] `services/subjects.py`: one module owning subject resolution, per-subject
      context injection, and the per-subject tool table.
- [ ] `services/conversations.py`: `for_subject`/`for_subject_ids`, and the
      visibility/predicate clauses keyed on `subject_id != ""`.
- [ ] `ToolContext` gains `project_id` / `dashboard_id` beside `document_id`.
- [ ] Get-or-create routes for project and dashboard; delete-with-subject on
      the project and dashboard delete routes.

## 2. Context injection per subject
- [ ] document: unchanged wording (user's text, never instructions).
- [ ] project: file tree + the open file (`run.subject_focus`, entry file when
      unset). Never the whole filesystem.
- [ ] dashboard: spec + bindings. Never query results.
- [ ] The screen sees whichever text was injected, not just documents.

## 3. Per-subject tool scoping
- [ ] One table: shared read families + the subject's own family.
- [ ] Composed with the agent's provisioned subset via `build_registry(allowed=)`
      — intersection, before any policy question.
- [ ] Tests: scoped-out tool absent from the registry, and still absent under
      `auto_writes`. Mutation-check the ordering.

## 4. Development-only unrestricted mode
- [ ] `DEV_UNRESTRICTED_AGENT`, guarded by a `@model_validator` that raises
      outside development/test.
- [ ] On: subject scoping bypassed, nothing parks. A `deny` row still denies.
- [ ] Never reaches workflow scope (both existing locks untouched).
- [ ] Visible in the existing bypass indicator, not a second quieter one.
- [ ] Mutation-check the production guard.

## 5. Real-time editing
- [ ] Project chat sidebar; preview rebuilds from *applied* files only.
- [ ] Dashboard chat sidebar; `update_dashboard` tool (does not exist today).

## 6. HTML entry points
- [ ] `.html` entry renders through the same locked frame, CSP unchanged.

## Gates
- [ ] ruff + mypy + pytest (x2) + openapi export
- [ ] tsc + eslint + vitest + build
- [ ] playwright (x2)

## Review
(filled in at the end)
