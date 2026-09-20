// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * The save-conflict state machine, both halves.
 *
 * The handler half turns the server's 409 into a *value* — the one choke
 * point Save and ⌘S share returns the conflict instead of throwing, and it
 * must not also raise the generic error toast: the banner is the error
 * surface, and two surfaces for one refusal is how users learn to ignore one.
 *
 * The view half is the loop the banner promises: the refused draft stays on
 * screen exactly as typed, "Overwrite anyway" resends against the head the
 * banner showed (winning over that version or re-arming with a newer one),
 * and "Reload theirs" swaps to their text through the same sync effect a
 * fresh open uses.
 */

const apiSaveDocument = vi.fn();
const apiListDocuments = vi.fn();
const apiListDocumentVersions = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    saveDocument: (...a: unknown[]) => apiSaveDocument(...a),
    listDocuments: (...a: unknown[]) => apiListDocuments(...a),
    listDocumentVersions: (...a: unknown[]) => apiListDocumentVersions(...a),
  },
}));

import { ApiError, type WorkspaceDocument } from "@workspace/api-client";
import { createDocumentHandlers } from "../components/handlers/documents";
import { DocumentsView } from "../components/views/documents";

const CONFLICT_DETAIL = {
  code: "document_version_conflict",
  message: "This document was saved after you loaded it",
  head_version_id: "v-head",
  updated_at: "2026-09-20T10:00:00Z",
  saved_by: "user-2",
};

function handlers(overrides: { setError?: ReturnType<typeof vi.fn> } = {}) {
  const setError = overrides.setError ?? vi.fn();
  return {
    setError,
    handlers: createDocumentHandlers({
      setError,
      setDocuments: vi.fn(),
      setActiveDocument: vi.fn(),
      setDocumentVersions: vi.fn(),
      setPendingEdits: vi.fn(),
      refreshArtifacts: vi.fn().mockResolvedValue(undefined),
      activeDocumentRef: { current: null },
    }),
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the handler's saveDocument", () => {
  it("returns the mapped conflict on the document 409 without raising the toast", async () => {
    apiSaveDocument.mockRejectedValue(
      new ApiError(CONFLICT_DETAIL.message, 409, CONFLICT_DETAIL),
    );
    const { setError, handlers: h } = handlers();

    const result = await h.saveDocument("doc-1", "mine", "v-base");

    expect(result).toEqual({
      headVersionId: "v-head",
      updatedAt: "2026-09-20T10:00:00Z",
      savedBy: "user-2",
    });
    // Called exactly once, to clear — never with a message. The banner is
    // the error surface for a conflict.
    expect(setError.mock.calls).toEqual([[""]]);
  });

  it("passes the base through to the API and says 'saved' on success", async () => {
    apiSaveDocument.mockResolvedValue({ id: "doc-1", content: "mine" });
    apiListDocuments.mockResolvedValue([]);
    apiListDocumentVersions.mockResolvedValue([]);
    const { handlers: h } = handlers();

    expect(await h.saveDocument("doc-1", "mine", "v-base")).toBe("saved");
    expect(apiSaveDocument).toHaveBeenCalledWith("doc-1", "mine", "v-base");
  });

  it("keeps every other failure on the generic error path, as 'failed'", async () => {
    // A 409 wearing a different code (a folder clash, a future route) must
    // not render as a version conflict the banner cannot resolve — and it
    // must not come back as the success value either: 'failed' is what lets
    // the view keep the dirty draft instead of relabelling it "Saved".
    apiSaveDocument.mockRejectedValue(
      new ApiError("Something else refused", 409, { code: "other_conflict" }),
    );
    const { setError, handlers: h } = handlers();

    expect(await h.saveDocument("doc-1", "mine", "v-base")).toBe("failed");
    expect(setError).toHaveBeenLastCalledWith("Something else refused");
  });

  it("still says 'saved' when only the bookkeeping listings hiccup", async () => {
    // The write landed — the document object carries the new head. A failed
    // list refresh after it must not be reported as an unsaved draft.
    apiSaveDocument.mockResolvedValue({ id: "doc-1", content: "mine" });
    apiListDocuments.mockRejectedValue(new Error("listing blipped"));
    apiListDocumentVersions.mockResolvedValue([]);
    const { handlers: h } = handlers();

    expect(await h.saveDocument("doc-1", "mine", "v-base")).toBe("saved");
  });
});

// --- The view's state machine ----------------------------------------------

const DOCUMENT: WorkspaceDocument = {
  id: "doc-1",
  title: "Launch Runbook",
  kind: "markdown",
  content: "Their words.",
  folder_id: "",
  updated_at: "2026-01-01T00:00:00Z",
  head_version_id: "v-base",
};

const noop = async () => undefined;

function view(props: {
  saveDocument: (
    documentId: string,
    content: string,
    baseVersionId?: string,
  ) => Promise<
    { headVersionId: string; updatedAt: string; savedBy: string } | "saved" | "failed"
  >;
  openDocument?: (documentId: string) => Promise<boolean | void>;
  active?: WorkspaceDocument;
}) {
  return createElement(DocumentsView, {
    documents: [],
    folders: [],
    folderOps: {
      createFolder: noop,
      renameFolder: noop,
      moveFolder: noop,
      removeFolder: noop,
      moveDocument: noop,
    },
    active: props.active ?? DOCUMENT,
    versions: [],
    openDocument: props.openDocument ?? noop,
    createDocument: noop,
    saveDocument: props.saveDocument,
    restoreVersion: noop,
    removeDocument: noop,
  });
}

function typeAndSave() {
  fireEvent.change(screen.getByLabelText("Document source"), {
    target: { value: "My words." },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
}

const CONFLICT = {
  headVersionId: "v-head",
  updatedAt: "2026-09-20T10:00:00Z",
  savedBy: "user-2",
};

describe("the editor's conflict banner", () => {
  it("sends the loaded head as the base and keeps the refused draft on screen", async () => {
    const saveDocument = vi.fn().mockResolvedValue(CONFLICT);
    render(view({ saveDocument }));

    typeAndSave();

    expect(
      await screen.findByText(/This document changed while you were editing/),
    ).toBeTruthy();
    expect(saveDocument).toHaveBeenCalledWith("doc-1", "My words.", "v-base");
    // The draft and its dirtiness survive the refusal — the banner offers
    // choices about the words, so the words must still be there.
    const source = screen.getByLabelText("Document source") as HTMLTextAreaElement;
    expect(source.value).toBe("My words.");
    expect(screen.getByRole("button", { name: "Save" })).toBeTruthy();
  });

  it("resends against the refused head on Overwrite anyway, and clears when it lands", async () => {
    const saveDocument = vi
      .fn()
      .mockResolvedValueOnce(CONFLICT)
      .mockResolvedValueOnce("saved");
    render(view({ saveDocument }));
    typeAndSave();
    await screen.findByText(/This document changed while you were editing/);

    fireEvent.click(screen.getByRole("button", { name: "Overwrite anyway" }));

    // The base is the head the banner just showed — winning over exactly
    // that version, never blindly over whatever landed since.
    expect(await screen.findByRole("button", { name: "Saved" })).toBeTruthy();
    expect(saveDocument).toHaveBeenLastCalledWith("doc-1", "My words.", "v-head");
    expect(screen.queryByText(/This document changed/)).toBeNull();
  });

  it("re-arms with the newer conflict when a third save landed meanwhile", async () => {
    const third = { ...CONFLICT, headVersionId: "v-newer" };
    const saveDocument = vi
      .fn()
      .mockResolvedValueOnce(CONFLICT)
      .mockResolvedValueOnce(third)
      .mockResolvedValue("saved");
    render(view({ saveDocument }));
    typeAndSave();
    await screen.findByText(/This document changed while you were editing/);

    fireEvent.click(screen.getByRole("button", { name: "Overwrite anyway" }));

    // Still in conflict, now against the newer head; the loop is bounded by
    // human clicks, not by silent retries.
    expect(
      await screen.findByRole("button", { name: "Overwrite anyway" }),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Overwrite anyway" }));
    expect(saveDocument).toHaveBeenLastCalledWith("doc-1", "My words.", "v-newer");
  });

  it("hands the screen back to theirs on Reload theirs", async () => {
    const saveDocument = vi.fn().mockResolvedValue(CONFLICT);
    const openDocument = vi.fn().mockResolvedValue(undefined);
    render(view({ saveDocument, openDocument }));
    typeAndSave();
    await screen.findByText(/This document changed while you were editing/);

    fireEvent.click(screen.getByRole("button", { name: /Reload theirs/ }));

    expect(openDocument).toHaveBeenCalledWith("doc-1");
    // The banner clears once the reload resolves; the refreshed document's
    // updated_at then drives the same sync effect a fresh open uses.
    expect(await screen.findByLabelText("Document source")).toBeTruthy();
    expect(screen.queryByText(/This document changed/)).toBeNull();
  });

  it("shows no banner when the save simply lands", async () => {
    const saveDocument = vi.fn().mockResolvedValue("saved");
    render(view({ saveDocument }));

    typeAndSave();

    expect(await screen.findByRole("button", { name: "Saved" })).toBeTruthy();
    expect(screen.queryByText(/This document changed/)).toBeNull();
  });

  it("keeps the dirty draft when a plain save fails outright", async () => {
    // The regression: 'failed' used to be indistinguishable from success, so
    // a network blip relabelled the button "Saved" over an unsaved draft.
    const saveDocument = vi.fn().mockResolvedValue("failed");
    render(view({ saveDocument }));

    typeAndSave();

    await waitFor(() => expect(saveDocument).toHaveBeenCalledTimes(1));
    const source = screen.getByLabelText("Document source") as HTMLTextAreaElement;
    expect(source.value).toBe("My words.");
    // Still dirty: the button offers Save, enabled, and never says Saved.
    const save = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(false);
    expect(screen.queryByRole("button", { name: "Saved" })).toBeNull();
  });

  it("keeps the banner AND the dirty draft when Overwrite anyway fails", async () => {
    // The user is fighting to not lose work — the resend blowing up (offline,
    // a 500, over-length) must change nothing: banner up, draft dirty, no
    // "Saved" lie. Only the handler's error toast says what went wrong.
    const saveDocument = vi
      .fn()
      .mockResolvedValueOnce(CONFLICT)
      .mockResolvedValueOnce("failed");
    render(view({ saveDocument }));
    typeAndSave();
    await screen.findByText(/This document changed while you were editing/);

    fireEvent.click(screen.getByRole("button", { name: "Overwrite anyway" }));

    await waitFor(() => expect(saveDocument).toHaveBeenCalledTimes(2));
    // The banner is still up, offering the same two ways out.
    expect(screen.getByText(/This document changed while you were editing/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Overwrite anyway" })).toBeTruthy();
    // The draft is exactly as typed and the pane still knows it is unsaved.
    const source = screen.getByLabelText("Document source") as HTMLTextAreaElement;
    expect(source.value).toBe("My words.");
    expect(screen.queryByRole("button", { name: "Saved" })).toBeNull();
  });

  it("keeps the banner when Reload theirs cannot actually reload", async () => {
    const saveDocument = vi.fn().mockResolvedValue(CONFLICT);
    // openDocument resolving false is the handler saying the fetch failed —
    // their version never arrived, so the choice is still on the table.
    const openDocument = vi.fn().mockResolvedValue(false);
    render(view({ saveDocument, openDocument }));
    typeAndSave();
    await screen.findByText(/This document changed while you were editing/);

    fireEvent.click(screen.getByRole("button", { name: /Reload theirs/ }));

    await waitFor(() => expect(openDocument).toHaveBeenCalledWith("doc-1"));
    expect(screen.getByText(/This document changed while you were editing/)).toBeTruthy();
  });
});
