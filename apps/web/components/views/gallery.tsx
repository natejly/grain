"use client";

import type { Listing, ListingDetail } from "@workspace/api-client";
import { Download, Search, Store, Users, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { describeError, formatRelative } from "./shared";

/**
 * The marketplace's browse-and-install surface.
 *
 * Two rules the drawer enforces rather than suggests:
 *
 * - The Install button does not enable until the listing's FULL payload has
 *   been fetched and rendered in front of the installer. A listing's body is
 *   instructions the model will follow; installing unread instructions is not
 *   consent, so the preview is a hard gate, not an optional affordance.
 * - What arrives is a copy, and the drawer says so: the installed skill is
 *   local, private (`shared=false`), and the installer's to edit or delete.
 *
 * Self-contained like SkillsView: the list is fetched when the page opens,
 * never at workspace load.
 */

type GalleryViewProps = {
  setError: (message: string) => void;
};

export function GalleryView({ setError }: GalleryViewProps) {
  const [listings, setListings] = useState<Listing[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [query, setQuery] = useState("");
  /** The id whose detail drawer is open, or null. */
  const [openId, setOpenId] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setListings(await api.listListings());
      setLoaded(true);
    } catch (caught) {
      setError(describeError(caught, "Could not load the gallery"));
    }
  }, [setError]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return listings;
    return listings.filter((listing) =>
      [listing.title, listing.slug, listing.description, listing.author_name]
        .join("\n")
        .toLowerCase()
        .includes(needle),
    );
  }, [listings, query]);

  return (
    <div className="content-page">
      <div className="page-heading">
        <div>
          <h1>Gallery</h1>
          <p>
            Skills your workspace published. Installing copies one into your
            workspace as your own private skill.
          </p>
        </div>
        <label className="memory-search">
          <Search size={14} aria-hidden />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search the gallery"
            aria-label="Search the gallery"
          />
        </label>
      </div>

      {loaded && listings.length === 0 ? (
        <div className="empty-state">
          <p>
            Nothing published yet. Publish a skill from the Skills page and it
            will appear here for the whole workspace.
          </p>
        </div>
      ) : (
        <div className="mcp-list">
          {visible.map((listing) => (
            <section key={listing.id} className="mcp-card ready">
              <header className="mcp-card-head">
                <div className="mcp-card-title">
                  <Store size={15} aria-hidden />
                  <strong>{listing.title}</strong>
                  <code className="skill-slug">{listing.slug}</code>
                  <span className="status-pill">{listing.kind}</span>
                  <span className="status-pill">v{listing.latest_version}</span>
                  {listing.visibility === "org" && (
                    <span className="status-pill">
                      <Users size={11} aria-hidden /> org
                    </span>
                  )}
                  {listing.mine && <span className="status-pill">yours</span>}
                </div>
                <div className="mcp-card-actions">
                  <button
                    className="ghost-button"
                    onClick={() => setOpenId(listing.id)}
                  >
                    View &amp; install
                  </button>
                </div>
              </header>
              <div className="mcp-card-meta">
                {listing.author_name && <>by {listing.author_name} · </>}
                {listing.install_count}{" "}
                {listing.install_count === 1 ? "install" : "installs"} · published{" "}
                {formatRelative(listing.updated_at)}
              </div>
              {listing.description && (
                <p className="agent-instructions">{listing.description}</p>
              )}
            </section>
          ))}
          {loaded && visible.length === 0 && (
            <div className="empty-state">
              <p>Nothing in the gallery matches “{query.trim()}”.</p>
            </div>
          )}
        </div>
      )}

      {openId !== null && (
        <ListingDrawer
          listingId={openId}
          setError={setError}
          onClose={() => setOpenId(null)}
          onInstalled={() => void reload()}
        />
      )}
    </div>
  );
}

type ListingDrawerProps = {
  listingId: string;
  setError: (message: string) => void;
  onClose: () => void;
  onInstalled: () => void;
};

/**
 * The detail drawer: the whole payload, the version trail, and — only below
 * all of that — the Install button. `detail` being loaded is what enables the
 * button, so the full body is on screen before installing is possible at all.
 */
function ListingDrawer({ listingId, setError, onClose, onInstalled }: ListingDrawerProps) {
  const [detail, setDetail] = useState<ListingDetail | null>(null);
  const [installing, setInstalling] = useState(false);
  const [installedAs, setInstalledAs] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const fetched = await api.getListing(listingId);
        if (!cancelled) setDetail(fetched);
      } catch (caught) {
        setError(describeError(caught, "Could not load the listing"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [listingId, setError]);

  const install = async () => {
    setInstalling(true);
    try {
      const result = await api.installListing(listingId);
      setInstalledAs(result.name);
      onInstalled();
    } catch (caught) {
      setError(describeError(caught, "Could not install this listing"));
    } finally {
      setInstalling(false);
    }
  };

  const body = typeof detail?.payload.body === "string" ? detail.payload.body : "";
  const description =
    typeof detail?.payload.description === "string" ? detail.payload.description : "";

  return (
    <div className="drawer-scrim" onClick={onClose}>
      <div className="agent-drawer" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-header">
          <div>
            <span>Gallery listing</span>
            <strong>{detail?.title ?? "Loading…"}</strong>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            <X size={15} />
          </button>
        </div>

        <div className="mcp-form agent-form">
          {detail === null ? (
            <p className="mcp-card-meta">Loading the full listing…</p>
          ) : (
            <>
              <div className="mcp-card-meta">
                <code className="skill-slug">{detail.slug}</code> ·{" "}
                {detail.kind} · v{detail.latest_version}
                {detail.author_name && <> · by {detail.author_name}</>}
                {" · "}
                {detail.install_count}{" "}
                {detail.install_count === 1 ? "install" : "installs"}
              </div>
              {description && <p>{description}</p>}

              {/* The consent surface: everything the install would copy,
                  verbatim, before any button. */}
              <fieldset className="agent-provisioning">
                <legend>What this skill tells the model to do</legend>
                <p className="agent-instructions">{body}</p>
              </fieldset>

              <fieldset className="agent-provisioning">
                <legend>Versions</legend>
                <ul className="skill-version-list">
                  {detail.versions.map((version) => (
                    <li key={version.id}>
                      <span>
                        v{version.version}
                        {version.changelog && <> · {version.changelog}</>} ·{" "}
                        {formatRelative(version.created_at)}
                      </span>
                    </li>
                  ))}
                </ul>
              </fieldset>

              {installedAs !== null ? (
                <p className="mcp-card-meta" role="status">
                  Installed as <code className="skill-slug">/{installedAs}</code> —
                  your own private copy, on the Skills page.
                </p>
              ) : (
                <div className="agent-editor-actions">
                  <button className="ghost-button" onClick={onClose}>
                    Close
                  </button>
                  <button
                    className="primary-button"
                    onClick={() => void install()}
                    // Enabled only once the full payload above is on screen.
                    disabled={detail === null || installing}
                  >
                    <Download size={14} />
                    {installing ? "Installing…" : "Install a copy"}
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
