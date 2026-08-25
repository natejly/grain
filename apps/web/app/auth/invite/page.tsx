import { InviteRoute } from "@/components/auth/invite-route";
import { SessionProvider } from "@/components/auth/session-provider";

export const metadata = { title: "Join a workspace · Grain" };

export default function InvitePage() {
  return (
    <SessionProvider>
      <InviteRoute />
    </SessionProvider>
  );
}
