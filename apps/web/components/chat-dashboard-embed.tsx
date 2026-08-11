"use client";

import type { DatasetQueryResult, GeneratedApp } from "@workspace/api-client";
import { ExternalLink } from "lucide-react";
import { memo, useMemo } from "react";
import { api } from "./api";
import { SandboxFrame } from "./sandbox-frame";

/**
 * A published dashboard, inside the conversation that produced it.
 *
 * The frame itself is the one from ADR 0004 and is not relaxed here: opaque
 * origin, `sandbox="allow-scripts"`, no network, every byte over the validated
 * postMessage channel. What is different is the *lifecycle*. A standalone page
 * mounts this once. A transcript re-renders on every streamed token, refetches
 * its lists when a run ends, and will one day virtualise — and an iframe that
 * remounts reloads its document, which is a white flash mid-answer, and an
 * iframe whose init props change identity re-posts and re-draws. Neither is
 * visible in a test that only asks whether the element exists.
 *
 * Four things keep it still, and each closes one of those:
 *
 * 1. **The element's key is the app id**, never the array index and never
 *    anything derived from message text. React preserves an iframe's DOM node
 *    — and therefore its loaded document — only while type, position and key
 *    hold, so this is what makes the difference between re-rendering and
 *    reloading.
 * 2. **The reference list is memoised down to a string of ids** before it is
 *    turned back into objects. `messages` gets a new array on every delta, so
 *    a list derived straight from it would be a new array every time; folding
 *    to `"a,b"` first means the memo only breaks when the *set* changes.
 * 3. **Only apps this workspace already lists, published, with a release,
 *    embed.** A half-streamed `[chart](/apps/reven` matches no known slug, so
 *    the frame appears once — when the link is complete — rather than mounting
 *    against a wrong slug and then mounting again against the right one.
 * 4. **The embed re-renders only when its release changes.** Every workspace
 *    refetch mints new `GeneratedApp` objects, so `memo`'s shallow compare
 *    would let all of them through; the comparator below asks about the id,
 *    the current release and the name instead. That also keeps `snapshots`
 *    stable, which matters because `SandboxFrame` re-posts its init whenever
 *    that prop changes identity and the idiom used elsewhere,
 *    `manifest.snapshots || {}`, mints a fresh object on every render.
 *
 * And one deliberate omission: no `api` and no `bindings` are passed, so an
 * embedded frame renders from the snapshot frozen into its release and cannot
 * issue live queries. A transcript scrolled past twenty embeds must not fire
 * twenty dataset queries, and a number in a *past* answer should be the number
 * that answer was written about.
 */

const REFERENCE = /\/apps\/([a-z0-9][a-z0-9-]*)/gi;

/** Slugs a message names, filtered to apps that can actually be embedded. */
export function referencedSlugs(content: string, apps: GeneratedApp[]): string[] {
  if (!content.includes("/apps/")) return [];
  const embeddable = new Set(
    apps
      .filter((app) => app.visibility === "public" && app.current_release_id)
      .map((app) => app.slug),
  );
  const found: string[] = [];
  for (const match of content.matchAll(REFERENCE)) {
    const slug = match[1].toLowerCase();
    if (embeddable.has(slug) && !found.includes(slug)) found.push(slug);
  }
  return found;
}

/** One frozen empty object, so a release with no snapshots is still stable. */
const EMPTY_SNAPSHOTS: Record<string, DatasetQueryResult> = {};

const EmbeddedDashboard = memo(
  function EmbeddedDashboard({ app }: { app: GeneratedApp }) {
    const snapshots = useMemo(
      () =>
        app.releases.find((release) => release.id === app.current_release_id)
          ?.manifest.snapshots ?? EMPTY_SNAPSHOTS,
      [app.releases, app.current_release_id],
    );
    return (
      <figure className="chat-dashboard">
        <figcaption>
          <strong>{app.name}</strong>
          <a href={`/apps/${app.slug}`} target="_blank" rel="noreferrer">
            Open full size
            <ExternalLink size={12} aria-hidden="true" />
          </a>
        </figcaption>
        <SandboxFrame
          src={api.publishedFrameUrl(app.slug)}
          title={`${app.name} dashboard`}
          snapshots={snapshots}
          className="sandbox-frame chat-embed"
        />
      </figure>
    );
  },
  (before, after) =>
    before.app.id === after.app.id &&
    before.app.slug === after.app.slug &&
    before.app.name === after.app.name &&
    before.app.current_release_id === after.app.current_release_id,
);

export function ChatDashboardEmbeds({
  content,
  apps,
}: {
  content: string;
  apps: GeneratedApp[];
}) {
  // Step one of the two-stage memo: a *string*, so identity tracks the set of
  // referenced apps rather than the message that mentioned them.
  const slugs = useMemo(() => referencedSlugs(content, apps).join(","), [content, apps]);

  const embeds = useMemo(
    () =>
      slugs
        ? slugs
            .split(",")
            .map((slug) => apps.find((app) => app.slug === slug))
            .filter((app): app is GeneratedApp => Boolean(app))
        : [],
    [slugs, apps],
  );

  if (embeds.length === 0) return null;
  return (
    <div className="chat-dashboards">
      {embeds.map((app) => (
        <EmbeddedDashboard key={app.id} app={app} />
      ))}
    </div>
  );
}
