import type { ShareLink, ShareLinkKind } from "@workspace/api-client";

/**
 * The pure half of the share-link modal: what state a link is in, what URL it
 * lives at, and which links belong to the thing being shared. React-free so
 * `tests/share-format.test.ts` can pin the semantics without a DOM.
 */

export type ShareLinkState = "active" | "revoked" | "expired";

/**
 * Revoked beats expired, deliberately: a link that was revoked and later aged
 * past its expiry was *stopped by a person*, and the list should keep saying
 * so — "expired" reads as "nobody did anything", which would be a lie.
 */
export function shareLinkState(
  link: Pick<ShareLink, "revoked_at" | "expires_at">,
  now: Date = new Date(),
): ShareLinkState {
  if (link.revoked_at) return "revoked";
  if (link.expires_at && new Date(link.expires_at) <= now) return "expired";
  return "active";
}

/** The label the status tag wears. */
export function shareLinkStateLabel(state: ShareLinkState): string {
  if (state === "active") return "Anyone with the link can view";
  if (state === "revoked") return "Revoked";
  return "Expired";
}

/**
 * The full public URL. `url_path` comes from the server ("/share/{token}");
 * the origin is wherever this web app is served, because the /share page is
 * ours. A trailing slash on the origin must not double up.
 */
export function shareUrl(origin: string, urlPath: string): string {
  return origin.replace(/\/+$/, "") + urlPath;
}

/**
 * The modal's expiry choices: a number of days as a string ("7"), or "" for a
 * link that lives until revoked. A short menu, not a date picker — "roughly
 * how long should this link outlive the conversation it was pasted into?" is
 * the question, and day-granularity answers it.
 */
export const SHARE_EXPIRY_CHOICES: { value: string; label: string }[] = [
  { value: "", label: "Never expires" },
  { value: "1", label: "Expires in 1 day" },
  { value: "7", label: "Expires in 7 days" },
  { value: "30", label: "Expires in 30 days" },
];

/**
 * The create request's `expires_at` for a chosen number of days — an ISO
 * instant the server normalizes to UTC — or undefined for "never".
 */
export function expiresAtFrom(days: string, now: Date = new Date()): string | undefined {
  if (!days) return undefined;
  return new Date(now.getTime() + Number(days) * 86_400_000).toISOString();
}

/**
 * The list row's expiry note: "expires <local date>" while the link is alive,
 * "" otherwise — a revoked or already-expired row's state tag says enough.
 */
export function expiryLabel(
  link: Pick<ShareLink, "revoked_at" | "expires_at">,
  now: Date = new Date(),
): string {
  if (!link.expires_at || shareLinkState(link, now) !== "active") return "";
  return `expires ${new Date(link.expires_at).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  })}`;
}

/**
 * The links about one resource, newest first. The API returns the whole
 * workspace's list (that is the honest scope of the resource), so the modal —
 * which is always about one dashboard or document — filters here.
 */
export function linksFor(
  links: ShareLink[],
  kind: ShareLinkKind,
  resourceId: string,
): ShareLink[] {
  return links
    .filter(
      (link) => link.resource_kind === kind && link.resource_id === resourceId,
    )
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
}
