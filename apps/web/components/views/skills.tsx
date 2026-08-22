"use client";

import type { Skill, SkillArg, SkillVersion } from "@workspace/api-client";
import { History, Pencil, Plus, Share2, Sparkles, Store, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { describeError, slugify } from "./shared";

/**
 * Author the skills the workspace invokes: a name, an instruction body, and —
 * optionally — a small set of typed args the body substitutes and a `shared`
 * flag that lets other members reach it.
 *
 * The panel owns its own data rather than joining the workspace hook, like
 * AgentsView: nobody who is chatting needs the full skill list at page load, and
 * the composer's slash picker fetches the visible list for itself.
 *
 * Two rights arrive already resolved on each row and the UI obeys them rather
 * than re-deriving the rule: `can_edit` is false for a shared skill a member did
 * not author (they may run it, not rewrite it), and `can_share` is false for a
 * member who is not an owner (they may keep a private skill, not publish one).
 * Disabling the control is kinder than a 403 after the click.
 */

type SkillsViewProps = {
  setError: (message: string) => void;
};

export function SkillsView({ setError }: SkillsViewProps) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loaded, setLoaded] = useState(false);
  /** null = closed; "" = creating; an id = editing that skill. */
  const [editing, setEditing] = useState<string | null>(null);
  /** The skill whose publish drawer is open, or null. */
  const [publishing, setPublishing] = useState<Skill | null>(null);

  const reload = useCallback(async () => {
    try {
      setSkills(await api.listSkills());
      setLoaded(true);
    } catch (caught) {
      setError(describeError(caught, "Could not load skills"));
    }
  }, [setError]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const remove = async (skill: Skill) => {
    if (!window.confirm(`Delete “${skill.title}”?`)) return;
    try {
      await api.deleteSkill(skill.id);
      await reload();
    } catch (caught) {
      setError(describeError(caught, "Could not delete the skill"));
    }
  };

  const open = editing === "" ? null : skills.find((row) => row.id === editing);

  return (
    <div className="content-page">
      <div className="page-heading">
        <h1>Skills</h1>
        <button className="primary-button" onClick={() => setEditing("")}>
          <Plus size={14} />
          New skill
        </button>
      </div>

      {loaded && skills.length === 0 ? (
        <div className="empty-state">
          <p>No skills yet. A skill is an instruction you author once and invoke with “/” in chat.</p>
        </div>
      ) : (
        <div className="mcp-list">
          {skills.map((skill) => (
            <section key={skill.id} className="mcp-card ready">
              <header className="mcp-card-head">
                <div className="mcp-card-title">
                  <Sparkles size={15} aria-hidden />
                  <strong>{skill.title}</strong>
                  <code className="skill-slug">/{skill.name}</code>
                  <span className="status-pill">v{skill.version}</span>
                  {skill.shared && (
                    <span className="status-pill">
                      <Share2 size={11} aria-hidden /> shared
                    </span>
                  )}
                  {skill.args.length > 0 && (
                    <span className="status-pill">
                      {skill.args.length} {skill.args.length === 1 ? "arg" : "args"}
                    </span>
                  )}
                </div>
                <div className="mcp-card-actions">
                  <button className="ghost-button" onClick={() => setEditing(skill.id)}>
                    <Pencil size={14} />
                    {skill.can_edit ? "Edit" : "View"}
                  </button>
                  <button
                    className="ghost-button"
                    onClick={() => setPublishing(skill)}
                    aria-label={`Publish ${skill.title} to the gallery`}
                  >
                    <Store size={14} />
                    Publish
                  </button>
                  {skill.can_edit && (
                    <button
                      className="ghost-button"
                      onClick={() => void remove(skill)}
                      aria-label={`Delete ${skill.title}`}
                    >
                      <Trash2 size={14} />
                      Delete
                    </button>
                  )}
                </div>
              </header>
              {skill.description && (
                <div className="mcp-card-meta">{skill.description}</div>
              )}
              <p className="agent-instructions">{skill.body}</p>
            </section>
          ))}
        </div>
      )}

      {editing !== null && (
        <SkillEditor
          skill={open ?? null}
          setError={setError}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void reload();
          }}
        />
      )}

      {publishing !== null && (
        <PublishDrawer
          skill={publishing}
          setError={setError}
          onClose={() => setPublishing(null)}
        />
      )}
    </div>
  );
}

type PublishDrawerProps = {
  skill: Skill;
  setError: (message: string) => void;
  onClose: () => void;
};

/**
 * Snapshot a skill into the gallery. Publishing is open to any member at
 * workspace visibility — sharing an instruction block with people who can
 * already see your shared skills is not an escalation — and the server is
 * what refuses a secret-bearing body or a taken slug, with a reason the
 * drawer surfaces verbatim.
 *
 * The changelog field only matters when the slug already has a listing (a
 * republish appends a version); on a first publish the server ignores it.
 */
function PublishDrawer({ skill, setError, onClose }: PublishDrawerProps) {
  const [slug, setSlug] = useState(skill.name);
  const [title, setTitle] = useState(skill.title);
  const [description, setDescription] = useState(skill.description);
  const [authorName, setAuthorName] = useState("");
  const [changelog, setChangelog] = useState("");
  const [busy, setBusy] = useState(false);
  const [publishedVersion, setPublishedVersion] = useState<number | null>(null);

  const submit = async () => {
    setBusy(true);
    try {
      const listing = await api.publishListing({
        kind: "skill",
        source_id: skill.id,
        slug,
        title,
        description,
        author_name: authorName,
        changelog,
      });
      setPublishedVersion(listing.latest_version);
    } catch (caught) {
      setError(describeError(caught, "Could not publish the skill"));
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
            <strong>{skill.title}</strong>
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
                {publishedVersion}). The workspace can install it from the
                Gallery; installs are copies, so later edits here change nothing
                already installed until you publish again.
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

type SkillEditorProps = {
  /** null = creating a new skill. */
  skill: Skill | null;
  setError: (message: string) => void;
  onClose: () => void;
  onSaved: () => void;
};

/** One arg row as the editor holds it: `choices` is a comma-joined string here
 * and split back into an array on save, which is all a small type declaration
 * needs. */
type ArgDraft = {
  name: string;
  type: SkillArg["type"];
  label: string;
  required: boolean;
  default: string;
  choices: string;
};

function toDraft(arg: SkillArg): ArgDraft {
  return {
    name: arg.name,
    type: arg.type,
    label: arg.label,
    required: arg.required,
    default: arg.default == null ? "" : String(arg.default),
    choices: arg.choices.map((choice) => String(choice)).join(", "),
  };
}

function fromDraft(draft: ArgDraft): SkillArg {
  const choices = draft.choices
    .split(",")
    .map((choice) => choice.trim())
    .filter(Boolean);
  return {
    name: slugify(draft.name).replace(/-/g, "_") || draft.name.trim(),
    type: draft.type,
    label: draft.label,
    description: "",
    required: draft.required,
    default: draft.default === "" ? null : draft.default,
    choices,
  };
}

/**
 * The drawer. Its state lives here, inside the component that only renders while
 * the drawer is open, so closing it resets a half-filled form for free.
 *
 * A skill's `name` is its slug and its identity — the "/name" the composer types
 * and the API key that is unique per workspace — so it is fixed once created:
 * editable while creating, read-only thereafter, which is why SkillUpdate has no
 * `name` field to send.
 */
function SkillEditor({ skill, setError, onClose, onSaved }: SkillEditorProps) {
  const readOnly = skill !== null && !skill.can_edit;
  const [name, setName] = useState(skill?.name ?? "");
  const [title, setTitle] = useState(skill?.title ?? "");
  const [description, setDescription] = useState(skill?.description ?? "");
  const [body, setBody] = useState(skill?.body ?? "");
  const [args, setArgs] = useState<ArgDraft[]>(() => (skill?.args ?? []).map(toDraft));
  const [shared, setShared] = useState(skill?.shared ?? false);
  const [saving, setSaving] = useState(false);
  const [versions, setVersions] = useState<SkillVersion[]>([]);
  const [showVersions, setShowVersions] = useState(false);

  const canShare = skill === null ? true : skill.can_share;

  const loadVersions = async () => {
    if (!skill) return;
    setShowVersions((value) => !value);
    if (versions.length > 0) return;
    try {
      setVersions(await api.listSkillVersions(skill.id));
    } catch (caught) {
      setError(describeError(caught, "Could not load versions"));
    }
  };

  const restore = async (version: SkillVersion) => {
    if (!skill) return;
    try {
      await api.restoreSkillVersion(skill.id, version.id);
      onSaved();
    } catch (caught) {
      setError(describeError(caught, "Could not restore that version"));
    }
  };

  const addArg = () =>
    setArgs((rows) => [
      ...rows,
      { name: "", type: "string", label: "", required: true, default: "", choices: "" },
    ]);

  const updateArg = (index: number, patch: Partial<ArgDraft>) =>
    setArgs((rows) => rows.map((row, position) => (position === index ? { ...row, ...patch } : row)));

  const removeArg = (index: number) =>
    setArgs((rows) => rows.filter((_, position) => position !== index));

  const save = async () => {
    setSaving(true);
    try {
      const declared = args
        .filter((row) => row.name.trim().length > 0)
        .map(fromDraft);
      if (skill === null) {
        await api.createSkill({
          name,
          title,
          description,
          body,
          args: declared,
          // Only ask to share when allowed; the server ignores it for a member,
          // but not sending it keeps the request honest.
          ...(canShare ? { shared } : {}),
        });
      } else {
        await api.updateSkill(skill.id, {
          title,
          description,
          body,
          args: declared,
          ...(canShare ? { shared } : {}),
        });
      }
      onSaved();
    } catch (caught) {
      setError(describeError(caught, "Could not save the skill"));
    } finally {
      setSaving(false);
    }
  };

  const ready =
    name.trim().length > 0 && title.trim().length > 0 && body.trim().length > 0;

  return (
    <div className="drawer-scrim" onClick={onClose}>
      <div className="agent-drawer" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-header">
          <div>
            <span>
              {skill === null ? "New skill" : readOnly ? "Skill" : "Edit skill"}
            </span>
            <strong>{title.trim() || "Untitled skill"}</strong>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            <X size={15} />
          </button>
        </div>

        <div className="mcp-form agent-form">
          <label>
            Name
            <input
              value={name}
              onChange={(event) => setName(slugify(event.target.value))}
              maxLength={80}
              // The slug is identity; it is set at creation and never rewritten.
              disabled={skill !== null}
              placeholder="summarise-thread"
              aria-label="Skill name"
            />
          </label>
          <label>
            Title
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              maxLength={160}
              disabled={readOnly}
              aria-label="Skill title"
            />
          </label>
          <label>
            Description
            <input
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              maxLength={500}
              disabled={readOnly}
              aria-label="Skill description"
            />
          </label>
          <label>
            Instructions
            <textarea
              value={body}
              onChange={(event) => setBody(event.target.value)}
              rows={8}
              maxLength={20000}
              disabled={readOnly}
              placeholder="What the model should do this turn. Reference an arg with {{ name }}."
              aria-label="Skill body"
            />
          </label>

          <fieldset className="agent-provisioning">
            <legend>Arguments</legend>
            {args.length === 0 && (
              <p className="mcp-card-meta">No arguments. The body is used as written.</p>
            )}
            {args.map((arg, index) => (
              <div key={index} className="skill-arg-row">
                <input
                  value={arg.name}
                  onChange={(event) => updateArg(index, { name: event.target.value })}
                  placeholder="name"
                  disabled={readOnly}
                  aria-label={`Argument ${index + 1} name`}
                />
                <select
                  value={arg.type}
                  onChange={(event) =>
                    updateArg(index, { type: event.target.value as SkillArg["type"] })
                  }
                  disabled={readOnly}
                  aria-label={`Argument ${index + 1} type`}
                >
                  <option value="string">string</option>
                  <option value="number">number</option>
                  <option value="integer">integer</option>
                  <option value="boolean">boolean</option>
                </select>
                <input
                  value={arg.default}
                  onChange={(event) => updateArg(index, { default: event.target.value })}
                  placeholder="default"
                  disabled={readOnly}
                  aria-label={`Argument ${index + 1} default`}
                />
                <input
                  value={arg.choices}
                  onChange={(event) => updateArg(index, { choices: event.target.value })}
                  placeholder="choices (a, b, c)"
                  disabled={readOnly}
                  aria-label={`Argument ${index + 1} choices`}
                />
                <label className="skill-arg-required">
                  <input
                    type="checkbox"
                    checked={arg.required}
                    onChange={(event) => updateArg(index, { required: event.target.checked })}
                    disabled={readOnly}
                  />
                  required
                </label>
                {!readOnly && (
                  <button
                    type="button"
                    className="icon-button"
                    onClick={() => removeArg(index)}
                    aria-label={`Remove argument ${index + 1}`}
                  >
                    <X size={14} />
                  </button>
                )}
              </div>
            ))}
            {!readOnly && (
              <button type="button" className="ghost-button" onClick={addArg}>
                <Plus size={14} /> Add argument
              </button>
            )}
          </fieldset>

          <label className="mcp-tool skill-shared">
            <input
              type="checkbox"
              checked={shared}
              onChange={(event) => setShared(event.target.checked)}
              disabled={readOnly || !canShare}
            />
            <span>
              Shared with the workspace
              {!canShare && " — only an owner or admin can share a skill"}
            </span>
          </label>

          {skill !== null && (
            <div className="skill-versions">
              <button type="button" className="ghost-button" onClick={() => void loadVersions()}>
                <History size={14} /> {showVersions ? "Hide" : "Show"} versions
              </button>
              {showVersions && (
                <ul className="skill-version-list">
                  {versions.map((version) => (
                    <li key={version.id}>
                      <span>
                        v{version.version} · {version.title}
                      </span>
                      {skill.can_edit && (
                        <button
                          type="button"
                          className="ghost-button"
                          onClick={() => void restore(version)}
                        >
                          Restore
                        </button>
                      )}
                    </li>
                  ))}
                  {versions.length === 0 && (
                    <li className="mcp-card-meta">No earlier versions.</li>
                  )}
                </ul>
              )}
            </div>
          )}

          {!readOnly && (
            <div className="agent-editor-actions">
              <button className="ghost-button" onClick={onClose}>
                Cancel
              </button>
              <button
                className="primary-button"
                onClick={() => void save()}
                disabled={!ready || saving}
              >
                {saving ? "Saving…" : skill === null ? "Create skill" : "Save changes"}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
