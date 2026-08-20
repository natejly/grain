import { LoginLinkRoute } from "@/components/auth/login-link-route";
import { SessionProvider } from "@/components/auth/session-provider";

export const metadata = { title: "Sign in · Grain" };

export default function LoginLinkPage() {
  return (
    <SessionProvider>
      <LoginLinkRoute />
    </SessionProvider>
  );
}
