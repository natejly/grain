/**
 * Where to send somebody after they sign in, when the page that sent them here
 * asked for somewhere other than "/".
 *
 * The only caller that needs this is the invitation flow: an invitee who is not
 * signed in has to sign in *and then still be holding the invitation*, and the
 * token lives in the URL they were sent. Without a way back, accepting means
 * finding the email again.
 *
 * A redirect target read out of the query string is an open redirect unless it
 * is fenced, so this returns "/" for anything that is not a plain same-origin
 * path. Rejected on purpose:
 *
 *   "https://evil.test/x"  an absolute URL — the classic phishing hand-off
 *   "//evil.test/x"        protocol-relative, which browsers treat as absolute
 *   "/\\evil.test"         backslash, which some parsers fold into "//"
 *   "javascript:…"         no scheme reaches a location assignment from here
 *
 * Allowing only a leading single "/" is a whitelist rather than a blacklist:
 * anything the checks below do not understand ends up at "/", which is the
 * page the user was going to anyway.
 */
export const DEFAULT_NEXT = "/";

export function safeNextPath(raw: string | null | undefined): string {
  if (!raw || !raw.startsWith("/")) return DEFAULT_NEXT;
  if (raw.startsWith("//") || raw.startsWith("/\\")) return DEFAULT_NEXT;
  return raw;
}

/** Read the redirect target out of a query string, already fenced. */
export function nextPathFrom(search: string): string {
  return safeNextPath(new URLSearchParams(search).get("next"));
}

/** A sign-in URL that comes back to `path` afterwards. */
export function signInUrlFor(path: string): string {
  const target = safeNextPath(path);
  return target === DEFAULT_NEXT
    ? "/auth/login"
    : `/auth/login?next=${encodeURIComponent(target)}`;
}
