// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getDocument = vi.fn();
const saveDocument = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    getDocument: (...a: unknown[]) => getDocument(...a),
    saveDocument: (...a: unknown[]) => saveDocument(...a),
  },
}));

import { AttachmentPane } from "../components/views/attachment-pane";

/**
 * The editor half of "edit files inside a chat".
 *
 * It loads by id rather than taking content as a prop, and that is the part
 * worth pinning: the agent edits these files too, so the text on screen has to
 * be what the document says now and not what it said when the thread opened.
 */

const DOC = {
  id: "doc-1",
  title: "notes.md",
  kind: "markdown" as const,
  content: "First draft.",
  folder_id: "",
  updated_at: "2026-08-27T00:00:00Z",
};

function pane(overrides: Record<string, unknown> = {}) {
  return createElement(AttachmentPane, {
    documentId: "doc-1",
    filename: "notes.md",
    onClose: () => undefined,
    ...overrides,
  });
}

beforeEach(() => {
  getDocument.mockReset().mockResolvedValue(DOC);
  saveDocument.mockReset().mockResolvedValue({ ...DOC, content: "Edited." });
});

afterEach(cleanup);

describe("the attached-file editor", () => {
  it("loads the document's current text by id", async () => {
    render(pane());
    await waitFor(() =>
      expect(screen.getByLabelText("Contents of notes.md")).toHaveProperty(
        "value",
        "First draft.",
      ),
    );
    expect(getDocument).toHaveBeenCalledWith("doc-1");
  });

  it("cannot save until something changed, then saves through the documents route", async () => {
    render(pane());
    const area = await screen.findByLabelText("Contents of notes.md");

    // "Saved" and disabled is the honest resting state: there is nothing to
    // write, and a live Save would suggest the pane had unflushed work.
    const button = screen.getByRole("button", { name: /Saved/ });
    expect(button).toHaveProperty("disabled", true);

    fireEvent.change(area, { target: { value: "Edited." } });
    const save = screen.getByRole("button", { name: /Save/ });
    expect(save).toHaveProperty("disabled", false);

    fireEvent.click(save);
    await waitFor(() => expect(saveDocument).toHaveBeenCalledWith("doc-1", "Edited."));
    // Back to clean, so a second click cannot re-post the same body.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Saved/ })).toHaveProperty(
        "disabled",
        true,
      ),
    );
  });

  it("reports a failed open instead of showing an empty file", async () => {
    getDocument.mockRejectedValue(new Error("gone"));
    render(pane());
    // An empty textarea would invite the user to "fix" it and save the blank
    // over a document that is actually fine.
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.queryByLabelText("Contents of notes.md")).toBeNull();
  });

  it("reports a failed save and keeps the draft dirty", async () => {
    saveDocument.mockRejectedValue(new Error("nope"));
    render(pane());
    const area = await screen.findByLabelText("Contents of notes.md");
    fireEvent.change(area, { target: { value: "Edited." } });
    fireEvent.click(screen.getByRole("button", { name: /Save/ }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    // Still offering Save: the work is not on the server, and saying "Saved"
    // would be the one lie that loses it.
    expect(screen.getByRole("button", { name: /Save/ })).toHaveProperty(
      "disabled",
      false,
    );
  });
});
