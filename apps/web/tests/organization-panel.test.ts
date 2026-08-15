import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { act, createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The organization panel: the tier a workspace owner is subject to.
 *
 * Two things here are worth a test and the rest is markup.
 *
 * The first is the `null` / `[]` distinction on the allow-lists. On the wire
 * `null` means *unbounded* and `[]` means *nothing is permitted*, and this panel
 * is the place that turns the "Model policy" choice into one or the other. Getting
 * it backwards would mean an admin selecting "Unrestricted" quietly bans every
 * model — a catastrophic read of a gesture that looks like relaxing.
 *
 * The second is that a non-admin's controls are disabled. That is a courtesy
 * rather than a control — every write is re-checked at the API, and
 * `tests/test_org_scope.py` is where a workspace owner's 403 is pinned — but a
 * console that offers a button which always fails is its own kind of wrong.
 */

const getOrg = vi.fn();
const listOrgPolicies = vi.fn();
const setOrgConfig = vi.fn();
const setOrgPolicy = vi.fn();
const clearOrgPolicy = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    getOrg: (...a: unknown[]) => getOrg(...a),
    listOrgPolicies: (...a: unknown[]) => listOrgPolicies(...a),
    setOrgConfig: (...a: unknown[]) => setOrgConfig(...a),
    setOrgPolicy: (...a: unknown[]) => setOrgPolicy(...a),
    clearOrgPolicy: (...a: unknown[]) => clearOrgPolicy(...a),
  },
}));

import { OrganizationPanel } from "../components/views/organization";

const ORG = {
  id: "org1",
  name: "Acme",
  allowed_harnesses: null,
  allowed_models: null,
  effective_harnesses: ["openai"],
  effective_models: ["gpt-5.5", "gpt-5-nano"],
  your_role: "admin",
  created_at: new Date().toISOString(),
};

const POLICY = {
  tool_name: "send_email",
  policy: "deny",
  scope: "workflow",
  updated_at: new Date().toISOString(),
};

beforeEach(() => {
  vi.clearAllMocks();
  getOrg.mockResolvedValue(ORG);
  listOrgPolicies.mockResolvedValue([POLICY]);
});
afterEach(cleanup);

async function mount(setError = vi.fn()) {
  render(createElement(OrganizationPanel, { setError }));
  await waitFor(() => expect(screen.getByText("Acme")).toBeTruthy());
}

describe("OrganizationPanel", () => {
  it("shows the ceilings the organization has set", async () => {
    await mount();
    // Scoped to the table: "workflow" and "deny" are also `<option>` labels in
    // the add form below it, so a bare text query would pass on the form alone.
    const row = screen.getByText("send_email").closest("tr");
    expect(row?.textContent).toContain("workflow");
    expect(row?.textContent).toContain("deny");
  });

  it("clears the bound rather than sending an empty list", async () => {
    await mount();
    setOrgConfig.mockResolvedValue(ORG);
    // It starts at "Unrestricted" because `allowed_models` is null, so
    // submitting must say "no bound" — never `[]`, which permits nothing at all.
    const mode = screen.getByLabelText("Model policy") as HTMLSelectElement;
    expect(mode.value).toBe("any");
    act(() => {
      fireEvent.submit(screen.getByRole("button", { name: "Save bounds" }));
    });
    await waitFor(() =>
      expect(setOrgConfig).toHaveBeenCalledWith({ clear_model_bound: true }),
    );
  });

  it("sends the list when the admin restricts to one", async () => {
    await mount();
    setOrgConfig.mockResolvedValue({ ...ORG, allowed_models: ["gpt-5-nano"] });
    act(() => {
      fireEvent.change(screen.getByLabelText("Model policy"), {
        target: { value: "list" },
      });
    });
    act(() => {
      fireEvent.change(screen.getByLabelText("Allowed models"), {
        target: { value: "gpt-5-nano, " },
      });
    });
    act(() => {
      fireEvent.submit(screen.getByRole("button", { name: "Save bounds" }));
    });
    // The trailing comma is a typing artefact, not an empty model name.
    await waitFor(() =>
      expect(setOrgConfig).toHaveBeenCalledWith({ allowed_models: ["gpt-5-nano"] }),
    );
  });

  it("sets a ceiling and replaces the row for the same tool and scope", async () => {
    await mount();
    setOrgPolicy.mockResolvedValue({ ...POLICY, policy: "ask" });
    act(() => {
      fireEvent.change(screen.getByLabelText("Tool"), {
        target: { value: "send_email" },
      });
    });
    act(() => {
      fireEvent.change(screen.getByLabelText("Scope"), {
        target: { value: "workflow" },
      });
    });
    act(() => {
      fireEvent.change(screen.getByLabelText("Ceiling"), { target: { value: "ask" } });
    });
    act(() => {
      fireEvent.submit(screen.getByRole("button", { name: "Set ceiling" }));
    });
    await waitFor(() =>
      expect(setOrgPolicy).toHaveBeenCalledWith("send_email", "ask", "workflow"),
    );
    // One row, not two: (tool, scope) is the key the API upserts on, and a panel
    // that showed both would be showing a state the database cannot hold.
    await waitFor(() => expect(screen.getAllByText("send_email")).toHaveLength(1));
  });

  it("gives a member the posture and none of the controls", async () => {
    getOrg.mockResolvedValue({ ...ORG, your_role: "member" });
    await mount();
    // The rule is visible — that is the whole reason reads are not admin-gated.
    expect(screen.getByText("send_email")).toBeTruthy();
    expect(
      screen.getByLabelText("Clear the workflow ceiling for send_email"),
    ).toHaveProperty("disabled", true);
    expect(screen.getByLabelText("Model policy")).toHaveProperty("disabled", true);
    expect(screen.queryByRole("button", { name: "Save bounds" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Set ceiling" })).toBeNull();
  });
});
