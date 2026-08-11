# Agent Creator — custom agents (system prompt + provisioned tools) in chat and workflows

Plan: ~/.claude/plans/steady-shimmying-walrus.md (approved 2026-08-11)
Branch: feat/agent-creator (worktree). Contracts + workflows/agents-router landed on
feat/agentic-workspace as 24ab394 + 71941ee; everything since lives here.

## Todo

- [x] Baseline recorded; coordinated shared-tree split with peer sessions, then moved to this worktree
- [x] Phase 1: migration 0031_agent_profiles + Agent model columns + schemas (committed 24ab394)
- [x] Phase 2: llm_tools registry_families + build_registry(allowed=) (24ab394); agent_loop AgentDirectives/resolve_directives wired at run_agent_turn/_continue/_advance (worktree)
- [x] Phase 3: app/api/agents.py CRUD + 409 guards (71941ee); GET /api/tools; main.py registration
- [x] Phase 4: NodeSpec.agent, validate_graph(agents=) + _check_agents, compiler/router threading, executor per-node agent + _default_agent (71941ee)
- [x] Backend tests: test_agents_api.py, test_agent_directives.py, workflow compiler/executor cases, tenant-isolation route cases + db.get review
- [x] Phase 5: api-client AgentInfo/ToolInfo types + methods; WorkflowGraphNode.agent (`when` already existed)
- [x] Phase 6: shared.ts View, navigation.ts Chat-group entry, agents.tsx view, workspace.tsx block, CSS
- [x] Phase 7: chat composer AgentSelect (+ reset on "Agent is not available"); workflow node "Runs as" picker via updateWorkflow({graph})
- [x] Gates: ruff, mypy, tsc, eslint, vitest (302), pnpm build, alembic 0031 upgrade x2 on scratch DB, openapi.json regenerated
- [x] Full pytest re-run after final edits: 1558 passed, 1 skipped, 3 xfailed
- [x] Boot dev stack (worktree ports 8020/3020) and verify at the seam
- [x] Commit worktree work on feat/agent-creator (b3e9161)
- [ ] Merge feat/agent-creator into feat/agentic-workspace once the fieldnote session's
      approval-modes work lands (expected conflicts: models.py, schemas.py, main.py,
      agent_loop.py, api-client index.ts — all additive on both sides; alembic must end
      0031_agent_profiles → 0032_approval_modes with one head)

## Review

Landed across three commits: 24ab394 (contracts: Agent columns, migration 0031,
registry_families + build_registry(allowed=)), 71941ee (workflow NodeSpec.agent +
validate/executor + /api/agents router), b3e9161 (loop directives, GET /api/tools,
api-client, Agents view, chat selector, workflow Runs-as picker, tests).

Verified end to end against a real model (gpt-5.5): an authored "Haiku Bot" with
instructions + allowed_tools=["search_sources"] answered chat in haiku with run.agent_id
set; a workflow agent node assigned to it succeeded with arguments {"agent": "Haiku Bot"}
and a haiku output; a graph naming a bogus agent id was refused at save with
agent_unknown; screenshots confirm the Agents view, the composer select (Default /
Research partner / Haiku Bot), and the canvas "Runs as" picker.

Design notes for future sessions: allowed_tools_json "" = all tools, "[]" = none (repo
"" = unset convention); the subset is a pure intersection under ToolPolicy, applied at
build_registry; resolve_directives ignores Agent.enabled so parked runs resume with the
directives they started with; per-node agent is persisted onto the backing run before
each turn (that is what makes park/resume agree); the compiler never emits agent ids.
Deliberate v1 exclusions: per-agent model override, per-conversation sticky selection.

Multi-session coordination: this session shared the tree with two others; the split was
negotiated by message, contracts were committed early to make clobbers recoverable, and
the tree moved to per-session worktrees mid-build (this one: Dashbored-agent-creator).
