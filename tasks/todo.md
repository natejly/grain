# Reachability: sandbox charts, and the citation verdict

Two capabilities exist server-side and cannot be reached from the app.

## 1. No chart the agent draws is visible anywhere

`services/sandbox/outputs.py` stores every figure as a workspace `Source` and
returns `{id, kind, mime, bytes, width, height}` — no `data`, no `url`. The web
`SandboxArtifact` type still describes the *old* inline-base64 contract, so
`artifactSource()` returns `""` and `Artifacts` renders `null`. Commit 04aa7b8
added `GET /api/sources/{id}/content`; nothing calls it.

- [x] Descriptor carries `url` (the API path), so the client has an address.
- [x] `SandboxArtifact` in api-client matches what the server actually sends.
- [x] `api.sourceContent(id)` fetches the bytes **through the API client**, not
      through `<img src>`: see "Why not `<img src>`" below.
- [x] `SourceImage` component: blob -> object URL -> `<img alt>`; revokes on
      unmount; says so out loud when the fetch fails.
- [x] Sandbox panel renders it.
- [x] Chat renders it: `ToolResult.artifacts` -> `agent_tool_calls.artifacts_json`
      -> `AgentToolCallOut.artifacts` -> `tool.completed` payload -> tool card.
- [x] Sources view can open a stored file (`sandbox-png-1.png` is a `Source` with
      `status="stored"`, which the web union did not even have a label for).
- [x] `views/projects.tsx`: checked, no change. A `ProjectFile.content` is a
      `Text` column of `str` end to end and no tool writes a sandbox output back
      into a project; the preview pane is fed only `{path, content}` strings.

### Why not `<img src="https://api…/api/sources/{id}/content">`
Two reasons, one of them deterministic:
1. An `<img>` request carries no `X-Workspace-Id`. `auth._resolve_workspace`
   then falls back to the caller's *oldest* membership, so for a user in two
   workspaces every image in the newer one 404s. Not a maybe — a bug.
2. The cookie is `SameSite=None`, but a cross-**site** subresource cookie is
   blocked outright by Safari and being removed by Chrome. Nothing in the app
   would tell the user why the picture vanished.
The e2e suite cannot detect either: `127.0.0.1:3010` and `127.0.0.1:8010` are
the same *site*, so the cookie is same-site there whatever production does.
Fetching through the client (`credentials: "include"` + `X-Workspace-Id`, which
already works for every other call) has neither problem and widens no CORS.

## 2. `run.citations` is emitted and nobody listens

- [x] Persist the report on the message it is about (`messages.citation_report_json`),
      so the verdict survives a reload. Fields are exactly `CitationReport.to_dict()`
      plus `summary` — nothing invented.
- [x] Handle `run.citations` in the stream so the verdict lands with the answer.
- [x] Render it under the assistant message: fabricated `[n]` and malformed
      markers are loud, a clean check is quiet, and nothing is shown when no
      passages were supplied.
- [x] `tool.failed` (runs.py:553) is handled, and both it and `tool.started`
      now name the tool, so a throwing tool is no longer a bare `run.failed`.

## Verification
- [x] ruff / mypy / pytest / openapi export
- [x] tsc / lint / vitest / build
- [x] playwright twice, consecutively
- [x] screenshots looked at, and `naturalWidth` asserted rather than presence

## Review

### There were three locks on the door, not one
The audit named the missing `url`. Fixing that produced an `<img>` with an alt
string and no picture, because the web `SandboxArtifact` type still described an
inline-base64 contract the server had abandoned — `data` and `url` both optional,
both absent, `artifactSource()` returning `""`, nothing failing anywhere. Fixing
*that* produced a broken-image icon, because `apps/web/next.config.ts` carried
`img-src 'self' data:` and blocked the blob: URL, with the only complaint in the
browser console. The same file already had `frame-src 'self' blob:` added for the
identical bug in the LaTeX preview. Note that `img-src 'self'` would not have
permitted the API origin either, so the naive `<img src>` was never going to work
even setting the cookie and header problems aside.

### What was added beyond the brief, and why
- **`sandbox_executions.artifacts_json`.** Without it the console could only show
  a figure in the seconds after it was drawn; a reload emptied the panel. That is
  the same invisibility arriving a minute later. It also *removed* state — the
  panel's per-execution artifact map is gone, because the row carries them.
- **An open affordance in Sources, and a label for `status="stored"`.** Every
  sandbox figure lands there and the status rendered as the raw word "stored".
- **`tool.started` also names its tool**, since the same lookup answered both and
  the chat's status line was already reaching for `tool_name`.

### Deferred, deliberately
- **`views/projects.tsx` needs no change.** A `ProjectFile.content` is a `Text`
  column of `str` end to end, no tool writes a sandbox output back into a
  project, and the preview pane is fed only `{path, content}` strings. A binary
  artifact cannot reach it without a new storage decision that this task is not.
- **`SandboxRunOut.artifacts` is now redundant** with `execution.artifacts`. Left
  in place: it is a published API field, and a test asserts the two agree.
- **The `chart` descriptor field is carried but not rendered.** Re-drawing a
  structured chart in the app's own theme is a feature, not a reachability fix.

### Honest note on what the browser suite can and cannot prove
It proves the images decode, that the verdict renders and survives a reload, and
that both work in dark. It **cannot** prove the cross-site cookie story either
way: `127.0.0.1:3010` and `127.0.0.1:8010` are the same *site*, so the local
stack would have passed a naive `<img src>` that fails in Safari today. That part
rests on the reasoning above, not on a green run.
