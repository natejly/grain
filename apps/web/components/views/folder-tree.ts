import type { DocumentSummary, Folder } from "@workspace/api-client";

/**
 * The server sends a flat list of folders and a flat list of files. The tree is
 * built here, in one place, and it is a plain module rather than a component
 * because every rule worth getting right is a rule about *data*: what happens
 * to a file filed in a folder that no longer exists, which folders a folder may
 * be moved into, what the delete refusal is talking about.
 *
 * ROOT is "", matching the server. A folder's parent and a file's folder are the
 * same kind of value, so "the top level" has exactly one spelling everywhere.
 */
export const ROOT = "";

export type FolderNode = {
  folder: Folder;
  /** Direct children, folders before files, each already in name order. */
  folders: FolderNode[];
  files: DocumentSummary[];
  /** Files here and below. What the delete refusal is counting. */
  total: number;
  /** 1 for a top-level folder. Drives the indent, and nothing else. */
  depth: number;
};

export type FileTree = {
  folders: FolderNode[];
  /** Files at the top level — including any orphaned by a vanished folder. */
  files: DocumentSummary[];
};

function byName(a: Folder, b: Folder): number {
  return a.name.localeCompare(b.name) || a.id.localeCompare(b.id);
}

/**
 * Build the tree.
 *
 * Two things it refuses to lose. A file whose `folder_id` names no folder we
 * were given surfaces at the top level rather than vanishing — the alternative
 * is a document that exists, is listed by the API, and appears nowhere on
 * screen, which is the failure mode this whole view exists to prevent. And a
 * folder whose parent is missing is likewise re-rooted, so a partial fetch
 * degrades to a flatter tree instead of an empty one.
 */
export function buildTree(folders: Folder[], files: DocumentSummary[]): FileTree {
  const known = new Set(folders.map((folder) => folder.id));

  // Where each folder actually hangs, which is not always where it says. A
  // parent we were not given, and a folder that is its own parent, both mean
  // the top level rather than "drop this row".
  const parentOf = new Map<string, string>();
  for (const folder of folders) {
    const claimed = folder.parent_id;
    parentOf.set(
      folder.id,
      known.has(claimed) && claimed !== folder.id ? claimed : ROOT,
    );
  }
  // Break any cycle by re-rooting the first folder that cannot walk up to the
  // top. The server refuses to create one, so this only fires on data that is
  // already wrong — and the alternative is that both folders in the loop, and
  // everything inside them, render nowhere at all.
  for (const folder of folders) {
    const visited = new Set<string>();
    let cursor = parentOf.get(folder.id) ?? ROOT;
    while (cursor && !visited.has(cursor)) {
      visited.add(cursor);
      cursor = parentOf.get(cursor) ?? ROOT;
    }
    if (cursor !== ROOT) parentOf.set(folder.id, ROOT);
  }

  const children = new Map<string, Folder[]>();
  for (const folder of [...folders].sort(byName)) {
    const parent = parentOf.get(folder.id) ?? ROOT;
    children.set(parent, [...(children.get(parent) ?? []), folder]);
  }

  const filed = new Map<string, DocumentSummary[]>();
  for (const file of files) {
    const key = known.has(file.folder_id) ? file.folder_id : ROOT;
    filed.set(key, [...(filed.get(key) ?? []), file]);
  }

  function build(folder: Folder, depth: number): FolderNode {
    const nested = (children.get(folder.id) ?? []).map((child) =>
      build(child, depth + 1),
    );
    const here = filed.get(folder.id) ?? [];
    return {
      folder,
      folders: nested,
      files: here,
      total: here.length + nested.reduce((sum, child) => sum + child.total, 0),
      depth,
    };
  }

  return {
    folders: (children.get(ROOT) ?? []).map((folder) => build(folder, 1)),
    files: filed.get(ROOT) ?? [],
  };
}

/** Every folder id at or below `folderId`, itself included. */
export function subtreeIds(folders: Folder[], folderId: string): string[] {
  const children = new Map<string, string[]>();
  for (const folder of folders) {
    children.set(folder.parent_id, [...(children.get(folder.parent_id) ?? []), folder.id]);
  }
  const out: string[] = [];
  const queue = [folderId];
  while (queue.length > 0 && out.length <= folders.length) {
    const id = queue.pop() as string;
    if (out.includes(id)) continue;
    out.push(id);
    queue.push(...(children.get(id) ?? []));
  }
  return out;
}

export type MoveTarget = {
  id: string;
  /** Full path, so two folders called "Notes" are distinguishable in a menu. */
  label: string;
};

/**
 * Where a folder may be moved.
 *
 * Its own subtree is excluded — a folder cannot contain itself, and the server
 * refuses it — and so is its current parent, because "move it to where it
 * already is" is an option that does nothing. Offering either would mean the
 * menu lists moves that fail, which teaches a user to distrust the menu.
 *
 * Pass `moving: ""` to get the list for a *file*, which has no subtree and can
 * go anywhere.
 */
export function moveTargets(
  folders: Folder[],
  moving: string,
  currentParent: string,
): MoveTarget[] {
  const blocked = new Set(moving ? subtreeIds(folders, moving) : []);
  const targets = folders
    .filter((folder) => !blocked.has(folder.id) && folder.id !== currentParent)
    .map((folder) => ({ id: folder.id, label: folderPath(folders, folder.id) }))
    .sort((a, b) => a.label.localeCompare(b.label));
  // The top level is a destination like any other, and it is the only one that
  // is not a row in the folder list.
  return currentParent === ROOT
    ? targets
    : [{ id: ROOT, label: "Top level" }, ...targets];
}

/** "Research / Interviews / 2025" — the name alone is ambiguous across a tree. */
export function folderPath(folders: Folder[], folderId: string): string {
  const byId = new Map(folders.map((folder) => [folder.id, folder]));
  const parts: string[] = [];
  let cursor = folderId;
  while (cursor && parts.length <= folders.length) {
    const folder = byId.get(cursor);
    if (!folder) break;
    parts.unshift(folder.name);
    cursor = folder.parent_id;
  }
  return parts.join(" / ");
}

/**
 * What a folder's row says about its contents, or "" when it is empty.
 *
 * Spelled out rather than shown as a bare number because the count is the
 * reason a delete will be refused, and "3" beside a folder name does not
 * prepare anyone for "still holds 3 files".
 */
export function contentsLabel(node: FolderNode): string {
  if (node.total === 0) return "";
  return `${node.total} file${node.total === 1 ? "" : "s"}`;
}
