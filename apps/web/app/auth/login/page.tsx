import { LoginRoute } from "@/components/auth/login-route";
import { SessionProvider } from "@/components/auth/session-provider";

export const metadata = { title: "Sign in · Jasmine" };

export default function LoginPage() {
  return (
    <SessionProvider>
      <LoginRoute />
    </SessionProvider>
  );
}
