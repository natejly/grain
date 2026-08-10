import { redirect } from "next/navigation";

/**
 * One login screen, two spellings. The account emails link to /auth/login, so
 * that is the canonical route; this exists because /login is what people type.
 */
export default function LoginAlias() {
  redirect("/auth/login");
}
