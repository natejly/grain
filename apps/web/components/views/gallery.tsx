"use client";

import type {
  Listing,
  ListingDetail,
  ToolInfo,
  WorkflowGraph,
} from "@workspace/api-client";
import { Download, Pin, RefreshCw, Search, Store, Users, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { describeError, formatRelative } from "./shared";
import { WorkflowGraphView } from "./workflow-graph";

/**
 * The marketplace's browse-and-install surface, for all three kinds.
 *
 * Rules the drawer enforces rather than suggests:
 *
 * - The Install button does not enable until the listing's FULL payload has
 *   been fetched and rendered in front of the installer. A listing's body is
 *   instructions the model will follow; installing unread instructions is not
 *   consent, so the preview is a hard gate, not an optional affordance.
 * - An agent's tool grants go through the scope sheet: requested tools are
 *   listed against the local registry, write-capable ones UNCHECKED by
 *   default, and only the confirmed subset becomes the installed grant. The
 *   copy still lands disabled — re-arming is a separate act in the editor.
 * - The requires-vs-installed checklist renders BEFORE install, so "this
 *   workflow needs a tool this workspace does not have" is something the
 *   installer reads, not something a run discovers.
 */

type GalleryViewProps = {
  setError: (message: string) => void;
};

const KIND_TABS = [
  { key: "all", label: "All" },
  { key: "skill", label: "Skills" },
  { key: "workflow", label: "Workflows" },
  { key: "agent", label: "Agents" },
] as const;

type KindFilter = (typeof KIND_TABS)[number]["key"];

export function GalleryView({ setError }: GalleryViewProps) {
  const [listings, setListings] = useState<Listing[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<KindFilter>("all");
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
    return listings.filter((listing) => {
      if (kind !== "all" && listing.kind !== kind) return false;
      if (!needle) return true;
      return [listing.title, listing.slug, listing.description, listing.author_name]
        .join("\n")
        .toLowerCase()
        .includes(needle);
    });
  }, [listings, query, kind]);

  return (
    <div className="content-page">
      <div className="page-heading">
        <div>
          <h1>Gallery</h1>
          <p>
            Published skills, workflows and agents. Installing makes an inert,
            editable copy.
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

      <div className="mcp-card-actions" role="tablist" aria-label="Listing kinds">
        {KIND_TABS.map((tab) => (
          <button
            key={tab.key}
            role="tab"
            aria-selected={kind === tab.key}
            className={kind === tab.key ? "primary-button" : "ghost-button"}
            onClick={() => setKind(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {loaded && listings.length === 0 ? (
        <div className="empty-state">
          <p>
            Nothing published yet. Publish from a skill, workflow or agent&rsquo;s
            own page.
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
                  {listing.install_state === "installed" && (
                    <span className="status-pill">installed</span>
                  )}
                  {listing.install_state === "update_available" && (
                    <span className="status-pill">update available</span>
                  )}
                  {listing.install_state === "diverged" && (
                    <span className="status-pill">edited locally</span>
                  )}
                  {listing.pinned && <span className="status-pill">pinned</span>}
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
              <p>Nothing here matches.</p>
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

/** The parsed halves of a payload the drawer can render, by kind. */
function payloadString(detail: ListingDetail | null, field: string): string {
  const value = detail?.payload[field];
  return typeof value === "string" ? value : "";
}

/** The agent payload's requested tool names, or null for "the whole registry". */
function requestedTools(detail: ListingDetail): string[] | null {
  const raw = payloadString(detail, "allowed_tools_json");
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === "string") : [];
  } catch {
    return [];
  }
}

function unresolvedTools(detail: ListingDetail): string[] {
  const raw = detail.payload.unresolved_tools;
  return Array.isArray(raw) ? raw.filter((v): v is string => typeof v === "string") : [];
}

function workflowGraph(detail: ListingDetail): WorkflowGraph | null {
  const raw = payloadString(detail, "graph_json");
  if (!raw) return null;
  try {
    return JSON.parse(raw) as WorkflowGraph;
  } catch {
    return null;
  }
}

function ListingDrawer({ listingId, setError, onClose, onInstalled }: ListingDrawerProps) {
  const [detail, setDetail] = useState<ListingDetail | null>(null);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [installing, setInstalling] = useState(false);
  const [installed, setInstalled] = useState<{ name: string; warnings: string[] } | null>(
    null,
  );
  /** The scope sheet's state: tool name -> confirmed. Agents only. */
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({});
  const [updating, setUpdating] = useState(false);
  const [updated, setUpdated] = useState<{ name: string; warnings: string[] } | null>(
    null,
  );
  /** Consent for replacing a diverged (locally edited) copy. */
  const [overwrite, setOverwrite] = useState(false);
  const [pinBusy, setPinBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [fetched, registry] = await Promise.all([
          api.getListing(listingId),
          api.listTools(),
        ]);
        if (cancelled) return;
        setDetail(fetched);
        setTools(registry);
        if (fetched.kind === "agent") {
          const local = new Map(registry.map((tool) => [tool.name, tool]));
          const requested = requestedTools(fetched) ?? registry.map((t) => t.name);
          // Write-capable tools arrive UNCHECKED: capability is confirmed per
          // tool by the installer, not inherited from the publisher's grant.
          setConfirmed(
            Object.fromEntries(
              requested.map((name) => [name, local.get(name)?.read_only === true]),
            ),
          );
        }
      } catch (caught) {
        setError(describeError(caught, "Could not load the listing"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [listingId, setError]);

  const install = async () => {
    if (detail === null) return;
    setInstalling(true);
    try {
      const body =
        detail.kind === "agent"
          ? {
              allowed_tools: Object.entries(confirmed)
                .filter(([, keep]) => keep)
                .map(([name]) => name),
            }
          : undefined;
      const result = await api.installListing(listingId, body);
      setInstalled({ name: result.name, warnings: result.warnings });
      onInstalled();
    } catch (caught) {
      setError(describeError(caught, "Could not install this listing"));
    } finally {
      setInstalling(false);
    }
  };

  const applyUpdate = async () => {
    if (detail === null) return;
    setUpdating(true);
    try {
      const result = await api.updateListingInstall(
        listingId,
        overwrite ? { confirm_overwrite: true } : undefined,
      );
      setUpdated({ name: result.name, warnings: result.warnings });
      setDetail(await api.getListing(listingId));
      onInstalled();
    } catch (caught) {
      setError(describeError(caught, "Could not update your copy"));
    } finally {
      setUpdating(false);
    }
  };

  const togglePin = async () => {
    if (detail === null) return;
    setPinBusy(true);
    try {
      const row = await api.pinListing(listingId, !detail.pinned);
      setDetail({ ...detail, pinned: row.pinned, install_state: row.install_state });
      onInstalled();
    } catch (caught) {
      setError(describeError(caught, "Could not change the pin"));
    } finally {
      setPinBusy(false);
    }
  };

  const localNames = useMemo(() => new Set(tools.map((tool) => tool.name)), [tools]);
  const graph = detail?.kind === "workflow" ? workflowGraph(detail) : null;
  const workflowNeeds = useMemo(() => {
    if (!graph) return [];
    return [
      ...new Set(
        graph.nodes
          .filter((node) => node.kind === "tool" && node.tool)
          .map((node) => node.tool as string),
      ),
    ];
  }, [graph]);

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
                <code className="skill-slug">{detail.slug}</code> · {detail.kind} · v
                {detail.latest_version}
                {detail.author_name && <> · by {detail.author_name}</>}
                {detail.visibility === "org" && detail.publisher_workspace && (
                  <> · from {detail.publisher_workspace}</>
                )}
                {" · "}
                {detail.install_count}{" "}
                {detail.install_count === 1 ? "install" : "installs"}
                {detail.install_state === "installed" && <> · installed</>}
                {detail.install_state === "update_available" && (
                  <> · update available</>
                )}
                {detail.install_state === "diverged" && <> · edited locally</>}
                {detail.pinned && <> · pinned</>}
              </div>
              {payloadString(detail, "description") && (
                <p>{payloadString(detail, "description")}</p>
              )}

              {/* The consent surface: everything the install would copy,
                  verbatim, before any button. */}
              {detail.kind === "skill" && (
                <fieldset className="agent-provisioning">
                  <legend>What this skill tells the model to do</legend>
                  <p className="agent-instructions">{payloadString(detail, "body")}</p>
                </fieldset>
              )}

              {detail.kind === "agent" && (
                <>
                  <fieldset className="agent-provisioning">
                    <legend>This agent&apos;s instructions</legend>
                    <p className="agent-instructions">
                      {payloadString(detail, "instructions") ||
                        "(stock instructions — no custom prompt)"}
                    </p>
                  </fieldset>
                  <fieldset className="agent-provisioning">
                    <legend>Tools it asks for — confirm each grant</legend>
                    {requestedTools(detail) === null && (
                      <p className="mcp-card-meta">
                        Published with access to the publisher&apos;s whole
                        registry; here, only what you check below is granted.
                      </p>
                    )}
                    {Object.keys(confirmed).length === 0 && (
                      <p className="mcp-card-meta">No tools requested.</p>
                    )}
                    {Object.keys(confirmed)
                      .sort()
                      .map((name) => {
                        const tool = tools.find((t) => t.name === name);
                        const present = localNames.has(name);
                        return (
                          <label key={name} className="mcp-tool">
                            <input
                              type="checkbox"
                              checked={confirmed[name] === true}
                              disabled={!present}
                              onChange={(event) =>
                                setConfirmed((state) => ({
                                  ...state,
                                  [name]: event.target.checked,
                                }))
                              }
                            />
                            <span>
                              {name}
                              {tool && !tool.read_only && (
                                <span className="status-pill"> writes</span>
                              )}
                              {!present && (
                                <span className="status-pill"> missing here</span>
                              )}
                            </span>
                          </label>
                        );
                      })}
                    {unresolvedTools(detail).length > 0 && (
                      <p className="mcp-card-meta">
                        Not published with the listing (workspace-specific for
                        the publisher): {unresolvedTools(detail).join(", ")}
                      </p>
                    )}
                  </fieldset>
                </>
              )}

              {detail.kind === "workflow" && (
                <>
                  {payloadString(detail, "source_prompt") && (
                    <fieldset className="agent-provisioning">
                      <legend>Asked for as</legend>
                      <p className="agent-instructions">
                        {payloadString(detail, "source_prompt")}
                      </p>
                    </fieldset>
                  )}
                  <fieldset className="agent-provisioning">
                    <legend>Needs these tools</legend>
                    {workflowNeeds.length === 0 && (
                      <p className="mcp-card-meta">No tool steps.</p>
                    )}
                    <ul className="skill-version-list">
                      {workflowNeeds.map((name) => (
                        <li key={name}>
                          <span>
                            {name}
                            <span className="status-pill">
                              {" "}
                              {localNames.has(name) ? "available" : "missing here"}
                            </span>
                          </span>
                        </li>
                      ))}
                    </ul>
                  </fieldset>
                  {graph && (
                    <fieldset className="agent-provisioning">
                      <legend>What it does</legend>
                      <WorkflowGraphView graph={graph} />
                    </fieldset>
                  )}
                </>
              )}

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

              {installed !== null ? (
                <div role="status">
                  <p className="mcp-card-meta">
                    Installed as <code className="skill-slug">{installed.name}</code>
                    {detail.kind === "skill" &&
                      " — your own private copy, on the Skills page."}
                    {detail.kind === "agent" &&
                      " — disabled until you enable it in the agent editor."}
                    {detail.kind === "workflow" &&
                      " — a manual draft on the Workflows page."}
                  </p>
                  {installed.warnings.map((warning) => (
                    <p key={warning} className="mcp-card-meta">
                      ⚠ {warning}
                    </p>
                  ))}
                </div>
              ) : updated !== null ? (
                <div role="status">
                  <p className="mcp-card-meta">
                    Updated your copy{" "}
                    <code className="skill-slug">{updated.name}</code> to v
                    {detail.latest_version}. Your settings — sharing, enablement,
                    tool grants, triggers — were left as you had them.
                  </p>
                  {updated.warnings.map((warning) => (
                    <p key={warning} className="mcp-card-meta">
                      ⚠ {warning}
                    </p>
                  ))}
                </div>
              ) : (
                <>
                  {detail.install_state === "diverged" && (
                    <label className="mcp-tool">
                      <input
                        type="checkbox"
                        checked={overwrite}
                        onChange={(event) => setOverwrite(event.target.checked)}
                      />
                      <span>
                        Replace my locally edited copy with the published version
                      </span>
                    </label>
                  )}
                  <div className="agent-editor-actions">
                    <button className="ghost-button" onClick={onClose}>
                      Close
                    </button>
                    {detail.install_state !== "" && (
                      <button
                        className="ghost-button"
                        onClick={() => void togglePin()}
                        disabled={pinBusy}
                      >
                        <Pin size={14} />
                        {detail.pinned ? "Unpin" : "Pin at this version"}
                      </button>
                    )}
                    {(detail.install_state === "update_available" ||
                      detail.install_state === "diverged") && (
                      <button
                        className="primary-button"
                        onClick={() => void applyUpdate()}
                        // A diverged copy is only replaceable once the
                        // overwrite consent above is checked.
                        disabled={
                          updating ||
                          (detail.install_state === "diverged" && !overwrite)
                        }
                      >
                        <RefreshCw size={14} />
                        {updating
                          ? "Updating…"
                          : `Update to v${detail.latest_version}`}
                      </button>
                    )}
                    <button
                      className={
                        detail.install_state === ""
                          ? "primary-button"
                          : "ghost-button"
                      }
                      onClick={() => void install()}
                      // Enabled only once the full payload above is on screen.
                      disabled={installing}
                    >
                      <Download size={14} />
                      {installing
                        ? "Installing…"
                        : detail.install_state === ""
                          ? "Install a copy"
                          : "Reinstall a fresh copy"}
                    </button>
                  </div>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
