"use client";

import type { DocumentSummary, Folder } from "@workspace/api-client";
import {
  ChevronDown,
  ChevronRight,
  FileText,
  FolderPlus,
  Folder as FolderIcon,
  MoreHorizontal,
} from "lucide-react";
import { useState } from "react";
import { DisclosureMenu } from "../disclosure-menu";
import {
  ROOT,
  buildTree,
  contentsLabel,
  moveTargets,
  type FolderNode,
} from "./folder-tree";
import { DOCUMENT_KIND_LABELS } from "./shared";

/**
 * The Files sidebar: a folder tree with the documents inside it.
 *
 * Moving is a menu rather than a drag. Drag-and-drop is the obvious gesture and
 * it is the one that cannot be done: it has no keyboard equivalent worth the
 * name, it gives a screen reader nothing to announce, and on a touch screen it
 * fights the scroll. "Move to…" lists every legal destination by full path, so
 * the same action works with a mouse, a keyboard and a finger — and a
 * destination that would be refused by the server is never offered.
 */
export type FolderOps = {
  createFolder: (name: string, parentId: string) => Promise<void>;
  renameFolder: (folder: Folder, name: string) => Promise<void>;
  moveFolder: (folder: Folder, parentId: string) => Promise<void>;
  removeFolder: (folder: Folder) => Promise<void>;
  moveDocument: (document: DocumentSummary, folderId: string) => Promise<void>;
};

export type FileTreeProps = {
  folders: Folder[];
  documents: DocumentSummary[];
  activeId: string;
  openDocument: (documentId: string) => Promise<void>;
  ops: FolderOps;
  /** Called with the folder the user was pointing at when they asked for a file. */
  onNewDocument: (folderId: string) => void;
  /** Documents with an agent write parked on them, marked with a dot so the
      queue is visible from the tree rather than only inside each document. */
  pendingIds?: Set<string>;
};

function FileRow({
  file,
  folders,
  activeId,
  openDocument,
  ops,
  indent,
  pendingIds,
}: {
  file: DocumentSummary;
  folders: Folder[];
  activeId: string;
  openDocument: (documentId: string) => Promise<void>;
  ops: FolderOps;
  indent: number;
  pendingIds?: Set<string>;
}) {
  return (
    <li className="tree-row" style={{ paddingInlineStart: `${indent * 12}px` }}>
      <button
        className={activeId === file.id ? "doc-item active" : "doc-item"}
        onClick={() => void openDocument(file.id)}
      >
        <FileText size={14} />
        <span className="doc-title">{file.title}</span>
        {pendingIds?.has(file.id) && (
          <span className="tree-dot" aria-label="Has proposed changes" />
        )}
        <span className="doc-kind">{DOCUMENT_KIND_LABELS[file.kind]}</span>
      </button>
      <DisclosureMenu
        id={`file-menu-${file.id}`}
        // Named with the file, because a row of identical "More" buttons is a
        // screen reader reading "more, more, more".
        triggerLabel={`Move ${file.title}`}
        trigger={<MoreHorizontal size={14} />}
        triggerClassName="icon-button tree-menu-trigger"
        menuLabel={`Move ${file.title}`}
      >
        {(close) => (
          <>
            {moveTargets(folders, "", file.folder_id).length === 0 && (
              <p className="disclosure-note">Nowhere else to put this yet.</p>
            )}
            {moveTargets(folders, "", file.folder_id).map((target) => (
              <button
                key={target.id || "root"}
                className="disclosure-option"
                onClick={() => {
                  close();
                  void ops.moveDocument(file, target.id);
                }}
              >
                <FolderIcon size={13} />
                <span className="disclosure-option-name">{target.label}</span>
              </button>
            ))}
          </>
        )}
      </DisclosureMenu>
    </li>
  );
}

function FolderRow({
  node,
  folders,
  activeId,
  openDocument,
  ops,
  onNewDocument,
  expanded,
  toggle,
  pendingIds,
}: {
  node: FolderNode;
  folders: Folder[];
  activeId: string;
  openDocument: (documentId: string) => Promise<void>;
  ops: FolderOps;
  onNewDocument: (folderId: string) => void;
  expanded: Set<string>;
  toggle: (folderId: string) => void;
  pendingIds?: Set<string>;
}) {
  const open = expanded.has(node.folder.id);
  const contents = contentsLabel(node);
  const targets = moveTargets(folders, node.folder.id, node.folder.parent_id);
  // Renaming happens in the row, not in a window.prompt(). A native prompt
  // cannot be styled, cannot be cancelled with a click, and — the reason it
  // matters here — gives no clue which of five identically-named rows is being
  // renamed once the dialog has covered them all.
  const [renaming, setRenaming] = useState("");

  if (renaming) {
    return (
      <li className="tree-folder">
        <form
          className="documents-new tree-rename"
          aria-label={`Rename ${node.folder.name}`}
          style={{ marginInlineStart: `${(node.depth - 1) * 12}px` }}
          onSubmit={async (event) => {
            event.preventDefault();
            const next = renaming.trim();
            setRenaming("");
            if (next && next !== node.folder.name) {
              await ops.renameFolder(node.folder, next);
            }
          }}
        >
          <input
            value={renaming}
            aria-label="Folder name"
            autoFocus
            onChange={(event) => setRenaming(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") setRenaming("");
            }}
          />
          <button type="submit" className="primary-button">
            Rename
          </button>
        </form>
      </li>
    );
  }

  return (
    <li className="tree-folder">
      <div className="tree-row" style={{ paddingInlineStart: `${(node.depth - 1) * 12}px` }}>
        <button
          className="folder-item"
          aria-expanded={open}
          onClick={() => toggle(node.folder.id)}
        >
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          <FolderIcon size={14} />
          <span className="doc-title">{node.folder.name}</span>
          {/* The count is the reason a delete gets refused, so it is on the row
              rather than only in the error that mentions it. */}
          {contents && <span className="doc-kind">{contents}</span>}
        </button>
        <DisclosureMenu
          id={`folder-menu-${node.folder.id}`}
          triggerLabel={`Actions for ${node.folder.name}`}
          trigger={<MoreHorizontal size={14} />}
          triggerClassName="icon-button tree-menu-trigger"
          menuLabel={`Actions for ${node.folder.name}`}
        >
          {(close) => (
            <>
              <button
                className="disclosure-option"
                onClick={() => {
                  close();
                  onNewDocument(node.folder.id);
                }}
              >
                <FileText size={13} />
                <span className="disclosure-option-name">New file here</span>
              </button>
              <button
                className="disclosure-option"
                onClick={() => {
                  close();
                  setRenaming(node.folder.name);
                }}
              >
                <FolderIcon size={13} />
                <span className="disclosure-option-name">Rename</span>
              </button>
              <button
                className="disclosure-option"
                onClick={() => {
                  close();
                  void ops.removeFolder(node.folder);
                }}
              >
                <FolderIcon size={13} />
                <span className="disclosure-option-name">Delete folder</span>
              </button>
              {targets.length > 0 && <p className="disclosure-note">Move to</p>}
              {targets.map((target) => (
                <button
                  key={target.id || "root"}
                  className="disclosure-option"
                  onClick={() => {
                    close();
                    void ops.moveFolder(node.folder, target.id);
                  }}
                >
                  <FolderIcon size={13} />
                  <span className="disclosure-option-name">{target.label}</span>
                </button>
              ))}
            </>
          )}
        </DisclosureMenu>
      </div>

      {open && (
        <ul className="tree-children">
          {node.folders.map((child) => (
            <FolderRow
              key={child.folder.id}
              node={child}
              folders={folders}
              activeId={activeId}
              openDocument={openDocument}
              ops={ops}
              onNewDocument={onNewDocument}
              expanded={expanded}
              toggle={toggle}
              pendingIds={pendingIds}
            />
          ))}
          {node.files.map((file) => (
            <FileRow
              key={file.id}
              file={file}
              folders={folders}
              activeId={activeId}
              openDocument={openDocument}
              ops={ops}
              indent={node.depth}
              pendingIds={pendingIds}
            />
          ))}
          {node.total === 0 && node.folders.length === 0 && (
            <li
              className="tree-empty"
              style={{ paddingInlineStart: `${node.depth * 12}px` }}
            >
              Empty
            </li>
          )}
        </ul>
      )}
    </li>
  );
}

export function FileTree({
  folders,
  documents,
  activeId,
  openDocument,
  ops,
  onNewDocument,
  pendingIds,
}: FileTreeProps) {
  // Collapsed by default, and remembered per mount. A tree that reopens itself
  // fully on every refetch throws the user back to the top of a long list.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");

  function toggle(folderId: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(folderId)) next.add(folderId);
      return next;
    });
  }

  const tree = buildTree(folders, documents);

  return (
    <div className="file-tree">
      <div className="file-tree-head">
        <button
          className="ghost-button"
          onClick={() => setCreating((value) => !value)}
          aria-expanded={creating}
        >
          <FolderPlus size={14} /> New folder
        </button>
      </div>

      {creating && (
        <form
          className="documents-new"
          aria-label="New folder"
          onSubmit={async (event) => {
            event.preventDefault();
            if (!name.trim()) return;
            await ops.createFolder(name.trim(), ROOT);
            setName("");
            setCreating(false);
          }}
        >
          <input
            value={name}
            aria-label="Folder name"
            autoFocus
            onChange={(event) => setName(event.target.value)}
          />
          <button type="submit" className="primary-button" disabled={!name.trim()}>
            Create folder
          </button>
        </form>
      )}

      {folders.length === 0 && documents.length === 0 ? (
        <p className="documents-empty">No files yet.</p>
      ) : (
        <ul className="tree-root">
          {tree.folders.map((node) => (
            <FolderRow
              key={node.folder.id}
              node={node}
              folders={folders}
              activeId={activeId}
              openDocument={openDocument}
              ops={ops}
              onNewDocument={onNewDocument}
              expanded={expanded}
              toggle={toggle}
              pendingIds={pendingIds}
            />
          ))}
          {tree.files.map((file) => (
            <FileRow
              key={file.id}
              file={file}
              folders={folders}
              activeId={activeId}
              openDocument={openDocument}
              ops={ops}
              indent={0}
              pendingIds={pendingIds}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
