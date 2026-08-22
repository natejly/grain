"use client";

import { Store, X } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { describeError, slugify } from "./shared";

/**
 * Snapshot a skill, workflow, or agent into the gallery — one drawer for all
 * three, opened from the page where each thing lives.
 *
 * Publishing at workspace visibility is open to any member (what transfers is
 * something the workspace could already read); the organization option is
 * owner-gated server-side, and a member picking it gets the 403's reason, not
 * a silent downgrade. The same goes for a secret-bearing body or a taken slug:
 * the server refuses with a reason and the drawer surfaces it verbatim.
 *
 * The changelog only matters when the slug already has a listing (a republish
 * appends a version); on a first publish the server ignores it.
 */
export type PublishDrawerProps = {
  kind: "skill" | "workflow" | "agent";
  sourceId: string;
  defaults: { slug: string; title: string; description: string };
  setError: (message: string) => void;
  onClose: () => void;
};

export function PublishDrawer({
  kind,
  sourceId,
  defaults,
  setError,
  onClose,
}: PublishDrawerProps) {
  const [slug, setSlug] = useState(slugify(defaults.slug));
  const [title, setTitle] = useState(defaults.title);
  const [description, setDescription] = useState(defaults.description);
  const [authorName, setAuthorName] = useState("");
  const [changelog, setChangelog] = useState("");
  const [visibility, setVisibility] = useState<"workspace" | "org">("workspace");
  const [busy, setBusy] = useState(false);
  const [publishedVersion, setPublishedVersion] = useState<number | null>(null);

  const submit = async () => {
    setBusy(true);
    try {
      const listing = await api.publishListing({
        kind,
        source_id: sourceId,
        slug,
        title,
        description,
        author_name: authorName,
        changelog,
        visibility,
      });
      setPublishedVersion(listing.latest_version);
    } catch (caught) {
      setError(describeError(caught, `Could not publish the ${kind}`));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="drawer-scrim" onClick={onClose}>
      <div className="agent-drawer" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-header">
          <div>
            <span>Publish to the gallery</span>
            <strong>{defaults.title}</strong>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            <X size={15} />
          </button>
        </div>

        <div className="mcp-form agent-form">
          {publishedVersion !== null ? (
            <>
              <p className="mcp-card-meta" role="status">
                Published as <code className="skill-slug">{slug}</code> (v
                {publishedVersion}). Installs are copies, so later edits here
                change nothing already installed until you publish again.
              </p>
              <div className="agent-editor-actions">
                <button className="primary-button" onClick={onClose}>
                  Done
                </button>
              </div>
            </>
          ) : (
            <>
              <label>
                Gallery slug
                <input
                  value={slug}
                  onChange={(event) => setSlug(slugify(event.target.value))}
                  maxLength={80}
                  aria-label="Listing slug"
                />
              </label>
              <label>
                Title
                <input
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  maxLength={160}
                  aria-label="Listing title"
                />
              </label>
              <label>
                Description
                <input
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                  maxLength={500}
                  placeholder="What installing this gets someone"
                  aria-label="Listing description"
                />
              </label>
              <label>
                Byline
                <input
                  value={authorName}
                  onChange={(event) => setAuthorName(event.target.value)}
                  maxLength={120}
                  placeholder="Shown as “by …” on the card"
                  aria-label="Listing byline"
                />
              </label>
              <label>
                Changelog
                <input
                  value={changelog}
                  onChange={(event) => setChangelog(event.target.value)}
                  maxLength={2000}
                  placeholder="Required only when updating an existing listing"
                  aria-label="Listing changelog"
                />
              </label>
              <label>
                Visible to
                <select
                  value={visibility}
                  onChange={(event) =>
                    setVisibility(event.target.value as "workspace" | "org")
                  }
                  aria-label="Listing visibility"
                >
                  <option value="workspace">This workspace</option>
                  {/* Owner-gated server-side; a member picking it gets the
                      403's reason, not a silent downgrade. */}
                  <option value="org">The whole organization</option>
                </select>
              </label>
              <div className="agent-editor-actions">
                <button className="ghost-button" onClick={onClose}>
                  Cancel
                </button>
                <button
                  className="primary-button"
                  onClick={() => void submit()}
                  disabled={busy || slug.trim().length === 0}
                >
                  <Store size={14} />
                  {busy ? "Publishing…" : "Publish"}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
