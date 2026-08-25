"use client";

import {
  Code2,
  FileCode,
  FilePlus,
  FolderOpen,
  MessageSquare,
  Plus,
  Save,
  Trash2,
} from "lucide-react";
import type {
  Citation,
  GeneratedApp,
  ProjectFile,
  ProjectKind,
  ProjectSummary,
  Source,
  WorkspaceProject,
} from "@workspace/api-client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { PaneToggle, useCollapsiblePane } from "../collapsible-pane";
import { LatexPreview } from "../latex-compiler";
import { ProjectPreview } from "../project-bundler";
import { useSubjectThread } from "../use-subject-thread";
import { FavoriteStar, type FavoritesApi } from "./favorites";
import { SubjectChatPanel } from "./subject-chat";

// Re-exported rather than redeclared: a locally-duplicated copy silently drifts
// the moment the API grows a field, which is exactly how `kind` went missing.
export type { ProjectFile, ProjectKind, ProjectSummary, WorkspaceProject };

export type ProjectsViewProps = {
  projects: ProjectSummary[];
  active: WorkspaceProject | null;
  openProject: (projectId: string) => Promise<void>;
  createProject: (name: string, description: string, kind: ProjectKind) => Promise<void>;
  saveFile: (projectId: string, path: string, content: string) => Promise<void>;
  removeFile: (projectId: string, path: string) => Promise<void>;
  /** Point the preview at another file — the only way to preview an .html page. */
  setEntry: (projectId: string, path: string) => Promise<void>;
  removeProject: (project: ProjectSummary) => Promise<void>;
  /** The shell's one favorites list; without it the header offers no star. */
  favorites?: FavoritesApi;
  /** What the side chat needs to be the same chat as the rail's. */
  chat?: ProjectChatDeps;
};

/**
 * Everything the panel beside the project needs from the shell. Optional as a
 * bundle rather than field by field, so a caller either wires the panel or does
 * not have one — there is no half-wired state where the composer sends into
 * nothing.
 */
export type ProjectChatDeps = {
  agentId?: string;
  sources: Source[];
  apps: GeneratedApp[];
  openCitation: (citation: Citation) => Promise<void>;
  /** Re-read the open project after an approved write lands. */
  reloadProject: () => Promise<void>;
  /** `DEV_UNRESTRICTED_AGENT` is on, so the panel wears the warning. */
  unrestricted?: boolean;
};

function directoryOf(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut === -1 ? "" : path.slice(0, cut);
}

function baseName(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

/** Group flat paths under their directory so the tree reads like a filesystem. */
function tree(files: ProjectFile[]): [string, ProjectFile[]][] {
  const groups = new Map<string, ProjectFile[]>();
  for (const file of files) {
    const directory = directoryOf(file.path);
    const bucket = groups.get(directory);
    if (bucket) bucket.push(file);
    else groups.set(directory, [file]);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
}

export function ProjectsView({
  projects,
  active,
  openProject,
  createProject,
  saveFile,
  removeFile,
  setEntry,
  removeProject,
  favorites,
  chat,
}: ProjectsViewProps) {
  const [creating, setCreating] = useState(false);
  const [treeCollapsed, toggleTree] = useCollapsiblePane("projects-sidebar");
  const [newName, setNewName] = useState("");
  const [newKind, setNewKind] = useState<ProjectKind>("web");
  const [newPath, setNewPath] = useState("");
  const [addingFile, setAddingFile] = useState(false);
  const [selected, setSelected] = useState("");
  // Unsaved edits, per path. Keeping them here rather than in one textarea
  // buffer lets the preview compile what you are typing across several files.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [showChat, setShowChat] = useState(false);

  useEffect(() => {
    setDrafts({});
    setSelected(active?.entry_path ?? "");
  }, [active?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const files = useMemo(() => active?.files ?? [], [active]);
  const current = files.find((file) => file.path === selected) ?? null;
  const draft = current ? (drafts[current.path] ?? current.content) : "";
  const dirty = current ? drafts[current.path] !== undefined : false;

  // The preview compiles drafts over saved content, so it tracks the editor
  // rather than the last save.
  const previewFiles = useMemo(
    () => files.map((file) => ({ path: file.path, content: drafts[file.path] ?? file.content })),
    [files, drafts],
  );

  // The panel's own run finished: re-read the project so an approved write
  // shows up in the tree, the editor and the preview. Nothing else in the shell
  // is refreshed from here — a thread scoped to one project is not a reason to
  // refetch six collections.
  const onRunSettled = useCallback(async () => {
    await chat?.reloadProject().catch(() => undefined);
  }, [chat]);

  const thread = useSubjectThread({
    kind: "project",
    subjectId: chat && active ? active.id : "",
    agentId: chat?.agentId,
    // Which file "this function" means. Sent per message and stored on the run,
    // so a write parked for approval resumes against the file it was asked
    // about rather than whatever is open when the reviewer gets to it.
    focus: selected,
    onRunSettled,
  });

  async function save() {
    if (!active || !current || !dirty) return;
    await saveFile(active.id, current.path, drafts[current.path]);
    setDrafts((previous) => {
      const next = { ...previous };
      delete next[current.path];
      return next;
    });
  }

  return (
    <div
      className={[
        "projects-layout",
        showChat && chat ? "with-chat" : "",
        treeCollapsed ? "list-collapsed" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <aside
        id="projects-file-tree"
        className={treeCollapsed ? "projects-sidebar collapsed" : "projects-sidebar"}
      >
        {/* Same arrangement as the documents list: the toggle is the one thing
            the collapsed strip keeps, so it can never hide itself. */}
        <div className="projects-sidebar-head">
          <PaneToggle
            subject="project list"
            collapsed={treeCollapsed}
            toggle={toggleTree}
            controls="projects-file-tree"
          />
          <span>Projects</span>
          <button
            className="icon-button"
            onClick={() => setCreating((value) => !value)}
            aria-label="New project"
          >
            <Plus size={16} />
          </button>
        </div>

        {creating && (
          <form
            className="projects-new"
            onSubmit={async (event) => {
              event.preventDefault();
              if (!newName.trim()) return;
              await createProject(newName.trim(), "", newKind);
              setNewName("");
              setNewKind("web");
              setCreating(false);
            }}
          >
            <select
              value={newKind}
              onChange={(event) => setNewKind(event.target.value as ProjectKind)}
              aria-label="Project kind"
            >
              <option value="web">Web app (React + TypeScript)</option>
              <option value="latex">LaTeX document</option>
            </select>
            <input
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              aria-label="Project name"
              autoFocus
            />
            <button type="submit" className="primary-button">
              Create
            </button>
          </form>
        )}

        {projects.length === 0 ? (
          <p className="projects-empty">
            No projects yet.
          </p>
        ) : (
          <ul className="projects-items">
            {projects.map((project) => (
              <li key={project.id}>
                <button
                  className={
                    active?.id === project.id ? "project-item active" : "project-item"
                  }
                  onClick={() => void openProject(project.id)}
                >
                  <Code2 size={14} />
                  <span className="project-item-name">{project.name}</span>
                  <span className="project-item-meta">{project.file_count}</span>
                </button>
              </li>
            ))}
          </ul>
        )}

        {active && (
          <div className="project-tree">
            <div className="project-tree-head">
              <span>Files</span>
              <button
                className="icon-button"
                onClick={() => setAddingFile((value) => !value)}
                aria-label="New file"
              >
                <FilePlus size={15} />
              </button>
            </div>
            {addingFile && (
              <form
                className="project-new-file"
                onSubmit={async (event) => {
                  event.preventDefault();
                  const path = newPath.trim();
                  if (!path) return;
                  await saveFile(active.id, path, "");
                  setSelected(path);
                  setNewPath("");
                  setAddingFile(false);
                }}
              >
                <input
                  value={newPath}
                  onChange={(event) => setNewPath(event.target.value)}
                  aria-label="File path"
                  autoFocus
                />
              </form>
            )}
            {tree(files).map(([directory, group]) => (
              <div key={directory || "/"} className="project-tree-group">
                {directory && (
                  <div className="project-tree-dir">
                    <FolderOpen size={12} /> {directory}
                  </div>
                )}
                <ul>
                  {group.map((file) => (
                    <li key={file.path}>
                      <button
                        className={
                          selected === file.path ? "project-file active" : "project-file"
                        }
                        onClick={() => setSelected(file.path)}
                      >
                        <span className="project-file-name">{baseName(file.path)}</span>
                        {file.path === active.entry_path && (
                          <span className="project-file-tag">entry</span>
                        )}
                        {drafts[file.path] !== undefined && (
                          <span className="project-file-dot" aria-label="unsaved" />
                        )}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </aside>

      {active ? (
        <section className="project-editor">
          <header className="project-editor-head">
            <div>
              <h2>{active.name}</h2>
              <span className="project-path">{selected || "no file selected"}</span>
            </div>
            <div className="project-editor-actions">
              {favorites && (
                <FavoriteStar
                  kind="project"
                  targetId={active.id}
                  label={active.name}
                  favorites={favorites}
                />
              )}
              <button className="primary-button" disabled={!dirty} onClick={() => void save()}>
                <Save size={14} /> {dirty ? "Save" : "Saved"}
              </button>
              {current && current.path !== active.entry_path && (
                <>
                  {/* The only way to preview a hand-written .html page: without
                      it a project could hold one and nothing could ever ask the
                      frame to render it. */}
                  <button
                    className="ghost-button"
                    onClick={() => void setEntry(active.id, current.path)}
                  >
                    <FileCode size={14} /> Set as entry
                  </button>
                  <button
                    className="icon-button"
                    onClick={async () => {
                      await removeFile(active.id, current.path);
                      setSelected(active.entry_path);
                    }}
                    aria-label={`Delete ${current.path}`}
                  >
                    <Trash2 size={15} />
                  </button>
                </>
              )}
              {chat && (
                <button
                  className={showChat ? "ghost-button active" : "ghost-button"}
                  onClick={() => setShowChat((value) => !value)}
                  aria-pressed={showChat}
                >
                  <MessageSquare size={14} /> Chat
                </button>
              )}
              <button
                className="ghost-button"
                onClick={() =>
                  void removeProject({
                    id: active.id,
                    name: active.name,
                    description: active.description,
                    kind: active.kind,
                    entry_path: active.entry_path,
                    file_count: files.length,
                    total_bytes: active.total_bytes,
                    updated_at: active.updated_at,
                  })
                }
              >
                <Trash2 size={14} /> Delete project
              </button>
            </div>
          </header>
          {current ? (
            <textarea
              className="project-source"
              value={draft}
              spellCheck={false}
              onChange={(event) => {
                const value = event.target.value;
                setDrafts((previous) => ({ ...previous, [current.path]: value }));
              }}
              // Cmd/Ctrl+S is the reflex in any editor; without it the button is
              // the only way to save and typing feels lossy.
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "s") {
                  event.preventDefault();
                  void save();
                }
              }}
            />
          ) : (
            <div className="empty-state">
              <Code2 size={20} />
              <p>Pick a file to edit.</p>
            </div>
          )}
        </section>
      ) : (
        <section className="project-editor empty">
          <div className="empty-state">
            <Code2 size={22} />
            <p>Select a project.</p>
          </div>
        </section>
      )}

      <section className="project-preview-pane">
        {active && files.length > 0 ? (
          active.kind === "latex" ? (
            <LatexPreview
              files={previewFiles}
              entryPath={active.entry_path}
              downloadName={`${active.name}.pdf`}
            />
          ) : (
            <ProjectPreview files={previewFiles} entryPath={active.entry_path} />
          )
        ) : (
          <div className="project-preview-empty">Nothing to preview yet.</div>
        )}
      </section>

      {showChat && chat && active && (
        <SubjectChatPanel
          className="project-chat"
          heading="Chat about this project"
          label={`Chat about ${active.name}`}
          close={() => setShowChat(false)}
          thread={thread}
          sources={chat.sources}
          apps={chat.apps}
          openCitation={chat.openCitation}
          unrestricted={chat.unrestricted}
        />
      )}
    </div>
  );
}
