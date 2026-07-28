# Lessons

- Never call `setView(...)`/navigation state setters *after* an awaited chain in
  an async handler — by the time the awaits resolve, the user may have navigated
  and the setter clobbers their view. Set view state synchronously at the start
  of the interaction. (Found via the e2e race in `uploadFiles()`: adding two API
  calls to `refreshExpansion` made the post-await `setView("sources")` land
  after the test clicked into Dashboards.)
- A postMessage handshake between a host page and an iframe must be
  order-independent in BOTH directions. `SandboxFrame` only sent data in reply
  to the frame's "ready" ping, which works when React creates the iframe
  (listener first) but not on a server-rendered page, where the frame can load
  and ping before hydration — published code apps silently rendered "Waiting for
  data…". Fix was two-sided: host posts init on mount + load + ready, and the
  injected runtime makes `onData` a setter that replays already-delivered
  snapshots. Rule: never assume the other side is listening yet; make delivery
  idempotent and replayable.
- "It won't build/run" reports deserve reproduction before code changes: here
  typecheck/lint/build/boot were all green, and the real fix was DX (one-command
  `make dev` + an API-down banner), not code.
- This repo verifies with: `make lint` (ruff+mypy+eslint+tsc), pytest suite,
  `pnpm test`, `pnpm build`, `pnpm test:e2e`, plus alembic upgrade on a scratch
  `DATABASE_URL` for schema changes. Run all of them before calling a phase done.
- `pip install garminconnect` on the py3.9 venv resolves to 0.2.8 (newer needs
  3.10+); pin `<0.3` and avoid `client.login()` (stdin MFA prompt) — call
  `client.garth.login(email, password, prompt_mfa=<raiser>)` instead.
