"use client";

import { Code2, FilePlus, FolderOpen, Plus, Save, Trash2 } from "lucide-react";
import type {
  ProjectFile,
  ProjectKind,
  ProjectSummary,
  WorkspaceProject,
} from "@workspace/api-client";
import { useEffect, useMemo, useState } from "react";
import { LatexPreview } from "../latex-compiler";
import { ProjectPreview } from "../project-bundler";

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
  removeProject: (project: ProjectSummary) => Promise<void>;
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
  removeProject,
}: ProjectsViewProps) {
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newKind, setNewKind] = useState<ProjectKind>("web");
  const [newPath, setNewPath] = useState("");
  const [addingFile, setAddingFile] = useState(false);
  const [selected, setSelected] = useState("");
  // Unsaved edits, per path. Keeping them here rather than in one textarea
  // buffer lets the preview compile what you are typing across several files.
  const [drafts, setDrafts] = useState<Record<string, string>>({});

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
    <div className="projects-layout">
      <aside className="projects-sidebar">
        <div className="projects-sidebar-head">
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
              placeholder="Project name"
              autoFocus
            />
            <button type="submit" className="primary-button">
              Create
            </button>
            <span className="field-hint">
              {newKind === "latex"
                ? "Starts from a document that already compiles. TeX Live runs in " +
                  "your browser with a core package set — tikz and beamer are not " +
                  "included."
                : "Starts from a React file that already renders. Only react and " +
                  "react-dom are available — there is no package install."}
            </span>
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
                  placeholder="components/Chart.tsx"
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
              <button className="primary-button" disabled={!dirty} onClick={() => void save()}>
                <Save size={14} /> {dirty ? "Save" : "Saved"}
              </button>
              {current && current.path !== active.entry_path && (
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
    </div>
  );
}
