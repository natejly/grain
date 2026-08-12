import { LoginLinkRoute } from "@/components/auth/login-link-route";
import { SessionProvider } from "@/components/auth/session-provider";

export const metadata = { title: "Sign in · Jasmine" };

export default function LoginLinkPage() {
  return (
    <SessionProvider>
      <LoginLinkRoute />
    </SessionProvider>
  );
}
