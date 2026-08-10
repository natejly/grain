import { AuthGate } from "@/components/auth/auth-gate";
import { SessionProvider } from "@/components/auth/session-provider";
import { Workspace } from "@/components/workspace";

export default function Home() {
  // <Workspace /> is an element the gate decides whether to render, so a
  // signed-out visitor never mounts it — no chrome, no requests, no flash.
  return (
    <SessionProvider>
      <AuthGate>
        <Workspace />
      </AuthGate>
    </SessionProvider>
  );
}
