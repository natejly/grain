# Plain-text documents: no preview pane, no approval friction

ChatGPT-canvas-style behaviour for `kind == "text"` documents.

## Plan

- [x] Frontend: `documents.tsx` — for text docs render only the source pane
      (no `.document-preview`), single-column `.document-panes`
- [x] CSS: `globals.css` — `.document-panes-single` collapses the grid,
      drops the source pane's right border
- [x] Backend: `agent_loop.py` — `Verdict.default_ask` provenance flag
      (tool's own default ask, chat scope, ask_writes/guardian mode, no
      policy row, no force_ask, org silent); at the park site, auto-approve
      `edit_document`/`create_document` calls that target a plain-text
      document, attributed via `decided_by = mode:plain_text_document`
- [x] Backend: helper resolving whether a call targets a plain-text doc
      (create: `kind == "text"`; edit: resolve target incl. open-document
      fallback, require `kind == "text"`; unresolvable → park, fail closed)
- [x] Tests: backend — text edit auto-applies under ask_writes; markdown
      still parks; deny row still denies; standing ask row still parks;
      ask_all still parks; create kind=text auto, default create parks
- [x] Tests: e2e `document-chat.spec.ts` — text doc has no preview pane
- [x] Run vitest + pytest (PYTHONPATH pinned to worktree), verify

## Review

- Editor: a `kind == "text"` document renders one full-width source pane;
  `DocumentBody` keeps its text branch for read-only surfaces, but the
  editor never mounts a preview for text. Share page untouched (its plain
  rendering is the read-only view of the document, which is correct).
- Policy: the carve-out lives at the park site in `_drain_pending`, keyed
  on the new `Verdict.default_ask` provenance flag, so it can only soften
  the tool's own default `ask` — a standing ask/deny row, an org ceiling,
  `force_ask`, `ask_all` (incl. the injection escalation), plan mode, and
  workflow scope all behave exactly as before. Attribution is honest:
  `approved_by_mode == "plain_text_document"`, and the chat UI's existing
  "Auto-approved" chip renders it.
- Verified: 7 new backend tests green; approval/guardian/safe-mode/plan/
  org-scope/doc-pending/artifacts suites green (152 tests); all 901 web
  vitest tests green; `pnpm typecheck` clean; `pnpm lint` 0 errors; both
  document e2e specs green against the real stack (the markdown hunk
  review still parks; the text doc shows no preview pane).
