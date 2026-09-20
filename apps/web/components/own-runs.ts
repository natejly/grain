/**
 * Runs this browser tab started, wherever they were started from.
 *
 * The memory.updated toast needs to tell "your run taught it something" from
 * a teammate's, and the event's payload carries only a run_id — workspace
 * events are workspace-wide and carry no actor. The ids used to be collected
 * by wrapping the PRIMARY chat's `setActiveRun`, which silently missed every
 * other surface a run can start from: an extra split pane and the panel
 * beside a document/project/dashboard each hold their own `useState` run and
 * never touched the wrapper.
 *
 * So the registry lives at the one seam every client-started run already
 * passes through — `followRun` in handlers/thread.ts — and at module scope
 * rather than in the shell's hook, because "started in this tab" is a fact
 * about the session, not about any one pane. Ids are added as runs start and
 * never pruned: by the time extraction lands the run has settled, a late
 * event must still match its run, and a Set of uuid strings per session is
 * noise. Uuids also make cross-workspace collisions a non-issue after a
 * workspace switch remounts the shell.
 */
const ownRunIds = new Set<string>();

/** Remember a run this client just started. Called from the turn engine. */
export function registerOwnRun(runId: string): void {
  if (runId) ownRunIds.add(runId);
}

/** Was this run started from this tab — any pane, any panel? */
export function isOwnRun(runId: string): boolean {
  return ownRunIds.has(runId);
}

/** Tests only: the module-scope Set outlives a test's render otherwise. */
export function resetOwnRuns(): void {
  ownRunIds.clear();
}
