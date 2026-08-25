"use client";

import type { Source } from "@workspace/api-client";
import type { Dispatch, RefObject, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

export type SourceHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setUploading: Dispatch<SetStateAction<boolean>>;
  setDragging: Dispatch<SetStateAction<boolean>>;
  refreshSecondary: () => Promise<void>;
  refreshExpansion: () => Promise<void>;
  fileInputRef: RefObject<HTMLInputElement | null>;
};

export function createSourceHandlers({
  setError,
  setUploading,
  setDragging,
  refreshSecondary,
  refreshExpansion,
  fileInputRef,
}: SourceHandlerDeps) {
  /**
   * Upload one file into workspace knowledge; returns the Source row so the
   * caller can chain on it (the attach popover turns a tabular upload into a
   * dataset), or null when the upload failed — the error is already shown.
   */
  async function uploadFiles(files: FileList | File[]): Promise<Source | null> {
    const file = Array.from(files)[0];
    if (!file) return null;
    setUploading(true);
    setError("");
    // Deliberately no setView here. Attaching used to teleport the user to the
    // Sources page mid-conversation; the upload lands in Sources either way,
    // and the surface that asked for it — the composer's attach popover, the
    // Sources page itself — is where the user stays.
    try {
      const uploaded = await api.uploadSource(file);
      await refreshSecondary();
      await refreshExpansion();
      return uploaded;
    } catch (caught) {
      setError(describeError(caught, "Upload failed"));
      return null;
    } finally {
      setUploading(false);
      setDragging(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function removeSource(source: Source) {
    if (!window.confirm(`Delete “${source.filename}” and all indexed passages?`)) return;
    setError("");
    try {
      await api.deleteSource(source.id);
      await refreshSecondary();
      await refreshExpansion();
    } catch (caught) {
      setError(describeError(caught, "Could not delete source"));
    }
  }

  return { uploadFiles, removeSource };
}
