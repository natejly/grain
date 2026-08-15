import { InviteRoute } from "@/components/auth/invite-route";
import { SessionProvider } from "@/components/auth/session-provider";

export const metadata = { title: "Join a workspace · Jasmine" };

export default function InvitePage() {
  return (
    <SessionProvider>
      <InviteRoute />
    </SessionProvider>
  );
}
