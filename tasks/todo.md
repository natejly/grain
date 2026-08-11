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
- [ ] Full pytest re-run after final edits (in flight)
- [ ] Boot dev stack and verify at the seam (create agent → chat as it → assign to workflow node → run)
- [ ] Commit worktree work on feat/agent-creator; coordinate merge with fieldnote session

## Review

(to be filled at completion)
