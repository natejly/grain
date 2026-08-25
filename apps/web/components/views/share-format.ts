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
