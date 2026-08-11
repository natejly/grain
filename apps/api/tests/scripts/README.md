# Model scripts for the test suite

`MODEL_PROVIDER=scripted` is the only test double left in the app: there is no
no-key product mode, so every test that needs a model needs a script. `agent.json`
is the one the whole `apps/api/tests` session runs against — `conftest.py` points
`SCRIPTED_MODEL_SCRIPT` at it before importing the app, and `Settings` is
`lru_cache`d, so one file has to serve every test in the run.

Entries are matched by case-insensitive substring against the prompt, and are
facet-aware: `steps` scripts the chat turn, `memories` scripts what the model
extracts from the exchange afterwards. A prompt no entry covers still answers —
the double quotes the retrieved passages — so only tests that assert on the
*model's* own output need an entry here. See `app/services/scripted_model.py`.

The `probe_*` tools named in `steps` are not app tools. They are the fake
registry `test_workflow_executor.py` installs, so an entry that calls one is
scripting a *workflow* agent node — `file the probe summary` exists to make one
reach a write and park on an approval.

`apps/web/e2e/agent-script.json` is the browser suite's separate script.
