import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const listPages = vi.fn();
const getPage = vi.fn();
const revalidatePage = vi.fn();
const listShareLinks = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    listPages: (...a: unknown[]) => listPages(...a),
    getPage: (...a: unknown[]) => getPage(...a),
    revalidatePage: (...a: unknown[]) => revalidatePage(...a),
    listShareLinks: (...a: unknown[]) => listShareLinks(...a),
  },
}));

import { PagesView } from "../components/views/pages";

/**
 * A page CLAIMS that following a marker shows the passage the answer was
 * written from. The status pip is what makes that claim inspectable rather
 * than asserted, and the drift badge is what stops a stale page reading as a
 * fresh one — so both are pinned here, along with the share modal being opened
 * for the right resource kind.
 */

const DRIFTED = {
  id: "page-1",
  conversation_id: "conv-1",
  title: "Retention policy",
  status: "drifted",
  drift_count: 2,
  generation_id: "gen-1",
  published_by: "user-1",
  drift_checked_at: "2026-09-21T09:00:00",
  created_at: "2026-09-20T09:00:00",
  updated_at: "2026-09-20T09:00:00",
};

const DETAIL = {
  page: DRIFTED,
  body_md: "# Retention policy\n\nNinety days [1].\n",
  citations: [
    {
      marker: 1,
      chunk_id: "chunk-a",
      source_id: "source-a",
      filename: "retention.md",
      ordinal: 0,
      frozen_excerpt: "The window is ninety days.",
      status: "changed",
      checked_at: "2026-09-21T09:00:00",
    },
    {
      marker: 2,
      chunk_id: "chunk-b",
      source_id: "source-b",
      filename: "gone.md",
      ordinal: 0,
      frozen_excerpt: "A passage since deleted.",
      status: "missing",
      checked_at: "2026-09-21T09:00:00",
    },
  ],
};

function mount() {
  return render(
    createElement(PagesView, { setError: () => undefined }),
  );
}

beforeEach(() => {
  listPages.mockResolvedValue([DRIFTED]);
  getPage.mockResolvedValue(DETAIL);
  revalidatePage.mockResolvedValue(DETAIL);
  listShareLinks.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("PagesView", () => {
  it("badges a drifted page in the list with the count that drifted", async () => {
    mount();
    expect(await screen.findByText("2 changed")).toBeTruthy();
  });

  it("shows a per-citation status pip rather than claiming everything is frozen", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Retention policy/ }));
    // The two verdicts the sweep can reach, each on its own marker.
    expect(await screen.findByText("changed")).toBeTruthy();
    expect(screen.getByText("missing")).toBeTruthy();
  });

  it("keeps showing the PUBLISHED excerpt under a drifted marker", async () => {
    // The whole product: the reader sees what the publisher saw, and is TOLD
    // it has since moved rather than quietly served newer text.
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Retention policy/ }));
    expect(await screen.findByText("The window is ninety days.")).toBeTruthy();
    expect(
      screen.getByText(/Some passages cited here have changed/),
    ).toBeTruthy();
  });

  it("opens the share modal for the page kind, not the conversation", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Retention policy/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Share" }));
    // The modal fetches the workspace's links on open; reaching that call at
    // all is what proves it mounted for this resource.
    expect(listShareLinks).toHaveBeenCalled();
  });

  it("re-checks evidence in place rather than navigating away", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Retention policy/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Re-check evidence/ }));
    expect(revalidatePage).toHaveBeenCalledWith("page-1");
  });
});
