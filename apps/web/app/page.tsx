import { AuthGate } from "@/components/auth/auth-gate";
import { SessionProvider } from "@/components/auth/session-provider";
import { Workspace } from "@/components/workspace";
import { WorkspaceSelection } from "@/components/workspace-selection";

export default function Home() {
  // <Workspace /> is an element the gate decides whether to render, so a
  // signed-out visitor never mounts it — no chrome, no requests, no flash.
  // WorkspaceSelection sits between the two so it can resolve *which* workspace
  // before the shell fires a single scoped request, and remount it on a switch.
  return (
    <SessionProvider>
      <AuthGate>
        <WorkspaceSelection>
          <Workspace />
        </WorkspaceSelection>
      </AuthGate>
    </SessionProvider>
  );
}
