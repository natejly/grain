import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The retrieval contract panel: which embedding contract search is reading.
 *
 * Three things here are worth a test and the rest is markup.
 *
 * The first is that `pending` and `unembedded` are not the same number. Rows on
 * an older contract are outstanding migration work; rows nothing has ever
 * embedded are equally absent from every generation and are not a migration's
 * fault. Adding them together would make a finished migration look stuck forever.
 *
 * The second is the bytes-per-vector arithmetic, because it is the entire
 * argument for a narrower vector and a reader should not have to do it in their
 * head — and because float16 and float32 differ by exactly the factor that makes
 * the argument.
 *
 * The third is that a 403 renders nothing rather than raising a banner. The
 * endpoint is org-admin-only, so a workspace owner seeing this screen gets one
 * routinely; it means "not yours", not "something is broken".
 */

const listRetrievalContract = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    listRetrievalContract: (...a: unknown[]) => listRetrievalContract(...a),
  },
}));

import { ApiError } from "@workspace/api-client";
import { RetrievalContractPanel } from "../components/views/retrieval-contract";

const ACTIVE = {
  id: "gen-active",
  model: "text-embedding-3-small",
  revision: "text-embedding-3-small",
  dimensions: 1536,
  storage_dtype: "float32",
  normalization: "l2",
  input_format: "v1",
  dense_floor: 0.3,
  status: "active",
  note: "",
  created_at: new Date().toISOString(),
  activated_at: new Date().toISOString(),
  coverage: [
    { table: "chunks", covered: 1200, pending: 40, unembedded: 7 },
    { table: "memory_items", covered: 300, pending: 0, unembedded: 2 },
  ],
};

const BUILDING = {
  ...ACTIVE,
  id: "gen-building",
  dimensions: 256,
  storage_dtype: "float16",
  dense_floor: 0.3535,
  status: "building",
  activated_at: null,
  coverage: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  listRetrievalContract.mockResolvedValue([ACTIVE, BUILDING]);
});
afterEach(cleanup);

async function mount(setError = vi.fn()) {
  render(createElement(RetrievalContractPanel, { setError }));
  await waitFor(() => expect(screen.getByText("Retrieval contract")).toBeTruthy());
}

describe("RetrievalContractPanel", () => {
  it("counts outstanding migration work without counting never-embedded rows", async () => {
    await mount();
    // 40 + 0 pending, and the 9 rows nothing ever embedded stay out of it.
    expect(screen.getByText("40")).toBeTruthy();
    expect(screen.getByText("1,500")).toBeTruthy();
  });

  it("shows what each contract costs per vector", async () => {
    await mount();
    // 1536 float32 = 6144 B; 256 float16 = 512 B, the twelvefold difference that
    // is the reason to migrate at all.
    expect(screen.getByText(/float32 · 6144 B/)).toBeTruthy();
    expect(screen.getByText(/float16 · 512 B/)).toBeTruthy();
  });

  it("says a migration is in progress while rows are on an older contract", async () => {
    await mount();
    expect(screen.getByText(/A migration is in progress/)).toBeTruthy();
    // Singular, because one generation is building. The count and its label are
    // separate elements, so the label is what is asserted.
    expect(screen.getByText("build in progress")).toBeTruthy();
    expect(screen.getByText("building")).toBeTruthy();
  });

  it("renders nothing, and raises nothing, when the caller is not an org admin", async () => {
    const setError = vi.fn();
    listRetrievalContract.mockRejectedValue(new ApiError("Forbidden", 403));
    const { container } = render(
      createElement(RetrievalContractPanel, { setError }),
    );
    await waitFor(() => expect(listRetrievalContract).toHaveBeenCalled());
    expect(container.textContent).toBe("");
    expect(setError).not.toHaveBeenCalled();
  });

  it("reports a real failure", async () => {
    const setError = vi.fn();
    listRetrievalContract.mockRejectedValue(new ApiError("boom", 500));
    render(createElement(RetrievalContractPanel, { setError }));
    await waitFor(() => expect(setError).toHaveBeenCalled());
  });
});
