"use client";

import type { DocumentSummary, Folder } from "@workspace/api-client";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

export type FolderHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setFolders: Dispatch<SetStateAction<Folder[]>>;
  setDocuments: Dispatch<SetStateAction<DocumentSummary[]>>;
};

/**
 * Filing. Every one of these refetches the folder list from the server rather
 * than patching the local array, because a move changes rows the caller did not
 * name — a folder's descendants come with it, and the tree is small enough that
 * a round trip is cheaper than a second, subtly different copy of the rules.
 */
export function createFolderHandlers({
  setError,
  setFolders,
  setDocuments,
}: FolderHandlerDeps) {
  async function createFolder(name: string, parentId: string) {
    setError("");
    try {
      await api.createFolder(name, parentId);
      setFolders(await api.listFolders());
    } catch (caught) {
      setError(describeError(caught, "Could not create that folder"));
    }
  }

  async function renameFolder(folder: Folder, name: string) {
    setError("");
    try {
      await api.updateFolder(folder.id, { name });
      setFolders(await api.listFolders());
    } catch (caught) {
      setError(describeError(caught, "Could not rename that folder"));
    }
  }

  async function moveFolder(folder: Folder, parentId: string) {
    setError("");
    try {
      await api.updateFolder(folder.id, { parent_id: parentId });
      setFolders(await api.listFolders());
    } catch (caught) {
      setError(describeError(caught, "Could not move that folder"));
    }
  }

  /**
   * No confirm() here, and that is the decision rather than an omission.
   *
   * The server refuses to delete a folder that still holds files — it never
   * cascades — so the only delete that can succeed is one that destroys
   * nothing. A dialog in front of it would be guarding an action with no
   * consequence, and would train the user to dismiss the dialogs that do guard
   * something (deleting a *document* still asks, because that one is final).
   * The refusal arrives as the server's own sentence, which names the count.
   */
  async function removeFolder(folder: Folder) {
    setError("");
    try {
      await api.deleteFolder(folder.id);
      setFolders(await api.listFolders());
    } catch (caught) {
      setError(describeError(caught, "Could not delete that folder"));
    }
  }

  async function moveDocument(document: DocumentSummary, folderId: string) {
    setError("");
    try {
      await api.moveDocument(document.id, folderId);
      setDocuments(await api.listDocuments());
    } catch (caught) {
      setError(describeError(caught, "Could not move that file"));
    }
  }

  return { createFolder, renameFolder, moveFolder, removeFolder, moveDocument };
}
