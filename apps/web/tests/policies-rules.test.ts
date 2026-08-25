import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The Rules ledger: the audit surface for standing tool grants.
 *
 * Three things are worth pinning against a mock. The rows must say what a
 * grant *means* — the registry description and the scope in plain words —
 * because "send_email · workflow" is exactly the shorthand that made grants
 * unauditable. Revoke must send the full identifying triple (tool, scope,
 * shared): the API keys rows on all three, and dropping `shared` would delete
 * the caller's personal row while the workspace-wide one stands, with the UI
 * reporting success. And a member's revoke on a shared row is disabled — a
 * courtesy over the server's 403, same contract as the organization panel.
 */

const listToolPolicies = vi.fn();
const listTools = vi.fn();
const deleteToolPolicy = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    listToolPolicies: (...a: unknown[]) => listToolPolicies(...a),
    listTools: (...a: unknown[]) => listTools(...a),
    deleteToolPolicy: (...a: unknown[]) => deleteToolPolicy(...a),
  },
}));

// The component reads who the caller is (for "You" and "set by you") and their
// role (for the shared-row revoke) from the session, not from a prop.
let sessionRole = "owner";
vi.mock("../components/auth/session-provider", () => ({
  useSession: () => ({
    status: "authenticated",
    session: {
      user_id: "user-self",
      user_name: "Self",
      user_email: "self@example.com",
      email_verified: true,
      workspace_id: "ws1",
      workspace_name: "Workspace",
      role: sessionRole,
      csrf_token: "",
    },
    refresh: vi.fn(),
    adopt: vi.fn(),
    signOut: vi.fn(),
  }),
}));

import { RulesTable } from "../components/views/policies";

const NOW = new Date().toISOString();

const PERSONAL_CHAT_ALLOW = {
  tool_name: "send_email",
  policy: "allow",
  scope: "chat",
  shared: false,
  created_at: NOW,
  updated_at: NOW,
  created_by: "user-self",
};

const SHARED_WORKFLOW_DENY = {
  tool_name: "send_email",
  policy: "deny",
  scope: "workflow",
  shared: true,
  created_at: NOW,
  updated_at: NOW,
  created_by: "someone-else-entirely",
};

const TOOLS = [
  { name: "send_email", description: "Send an email from the workspace", read_only: false, family: "core" },
];

beforeEach(() => {
  vi.clearAllMocks();
  sessionRole = "owner";
  listToolPolicies.mockResolvedValue([PERSONAL_CHAT_ALLOW, SHARED_WORKFLOW_DENY]);
  listTools.mockResolvedValue(TOOLS);
});
afterEach(cleanup);

async function mount() {
  render(createElement(RulesTable));
  await waitFor(() =>
    expect(screen.getAllByText("send_email").length).toBeGreaterThan(0),
  );
}

describe("RulesTable", () => {
  it("says what each grant means: description, scope words, and who it covers", async () => {
    await mount();
    // The registry description, so a row is judgeable without knowing the
    // machine name; scope in plain words, never the enum's.
    expect(screen.getAllByText("Send an email from the workspace")).toHaveLength(2);
    const personal = screen.getByText("while chatting").closest("tr");
    expect(personal?.textContent).toContain("You");
    expect(personal?.textContent).toContain("allow");
    const shared = screen.getByText("unattended runs").closest("tr");
    expect(shared?.textContent).toContain("Everyone");
    expect(shared?.textContent).toContain("deny");
    // An unresolvable grantor id is shortened, never invented: there is no
    // member-readable roster to turn it into a name.
    expect(shared?.textContent).toContain("set by someone-");
  });

  it("revokes with the full triple, and refetches rather than trusting itself", async () => {
    await mount();
    deleteToolPolicy.mockResolvedValue(undefined);
    listToolPolicies.mockResolvedValue([SHARED_WORKFLOW_DENY]);
    fireEvent.click(
      screen.getByLabelText("Revoke the personal chat rule for send_email"),
    );
    // All three of (tool, scope, shared): the API keys rows on the triple, and
    // omitting `shared` deletes the wrong row while reporting success.
    await waitFor(() =>
      expect(deleteToolPolicy).toHaveBeenCalledWith("send_email", "chat", {
        shared: false,
      }),
    );
    // The list shrank because the server said so — the table refetched.
    await waitFor(() => expect(listToolPolicies).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText("while chatting")).toBeNull());
  });

  it("disables a member's revoke on a shared row, and says why", async () => {
    sessionRole = "member";
    await mount();
    const button = screen.getByLabelText(
      "Revoke the shared workflow rule for send_email",
    );
    expect(button).toHaveProperty("disabled", true);
    expect(button.getAttribute("title")).toContain("owner");
    // Their own personal grant is still theirs to take back.
    expect(
      screen.getByLabelText("Revoke the personal chat rule for send_email"),
    ).toHaveProperty("disabled", false);
  });

  it("explains what a standing grant is when there are none", async () => {
    listToolPolicies.mockResolvedValue([]);
    render(createElement(RulesTable));
    await waitFor(() =>
      expect(screen.getByText(/No standing rules/).textContent).toContain(
        "Always allow",
      ),
    );
  });
});
