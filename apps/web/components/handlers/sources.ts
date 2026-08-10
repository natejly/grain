"use client";

import type { Source } from "@workspace/api-client";
import type { Dispatch, RefObject, SetStateAction } from "react";
import { api } from "../api";
import { describeError, type View } from "../views/shared";

export type SourceHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setView: Dispatch<SetStateAction<View>>;
  setUploading: Dispatch<SetStateAction<boolean>>;
  setDragging: Dispatch<SetStateAction<boolean>>;
  refreshSecondary: () => Promise<void>;
  refreshExpansion: () => Promise<void>;
  fileInputRef: RefObject<HTMLInputElement | null>;
};

export function createSourceHandlers({
  setError,
  setView,
  setUploading,
  setDragging,
  refreshSecondary,
  refreshExpansion,
  fileInputRef,
}: SourceHandlerDeps) {
  async function uploadFiles(files: FileList | File[]) {
    const file = Array.from(files)[0];
    if (!file) return;
    setUploading(true);
    setError("");
    // Switch immediately so later refreshes never clobber user navigation.
    setView("sources");
    try {
      await api.uploadSource(file);
      await refreshSecondary();
      await refreshExpansion();
    } catch (caught) {
      setError(describeError(caught, "Upload failed"));
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
