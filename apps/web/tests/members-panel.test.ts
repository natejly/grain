import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { act, createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The membership panels: the roster and the invitation queue.
 *
 * What is worth testing here is not the markup but the two places the panel
 * could quietly do the wrong thing — showing a raw invitation link after the
 * one moment it exists, and swallowing the API's refusals (the last-owner 409,
 * the already-a-member 409) behind a generic message. Both are asserted below.
 *
 * Permissions are deliberately *not* tested here: nothing in this component is
 * a permission check. Every route behind it is owner-only at the API, and
 * `tests/test_workspace_invites.py` is where a member's 403 is pinned.
 */

const setAdminMemberRole = vi.fn();
const removeAdminMember = vi.fn();
const createInvite = vi.fn();
const revokeInvite = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    setAdminMemberRole: (...a: unknown[]) => setAdminMemberRole(...a),
    removeAdminMember: (...a: unknown[]) => removeAdminMember(...a),
    createInvite: (...a: unknown[]) => createInvite(...a),
    revokeInvite: (...a: unknown[]) => revokeInvite(...a),
  },
}));

import { ApiError } from "@workspace/api-client";
import { InvitesPanel, MembersPanel } from "../components/views/members";

const OWNER = {
  user_id: "u1",
  membership_id: "m1",
  name: "Ada",
  email: "ada@example.test",
  role: "owner",
  status: "active",
  joined_at: new Date().toISOString(),
  is_self: true,
};

const MEMBER = {
  ...OWNER,
  user_id: "u2",
  membership_id: "m2",
  name: "Bo",
  email: "bo@example.test",
  role: "member",
  is_self: false,
};

const INVITE = {
  id: "i1",
  email: "cy@example.test",
  role: "member",
  status: "pending" as const,
  invited_by: "u1",
  invited_by_name: "Ada",
  expires_at: new Date(Date.now() + 86400000).toISOString(),
  created_at: new Date().toISOString(),
};

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(cleanup);

async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
}

describe("MembersPanel", () => {
  it("shows every member with a role control and a way out", () => {
    render(
      createElement(MembersPanel, {
        members: [OWNER, MEMBER],
        onChange: vi.fn(),
        setError: vi.fn(),
      }),
    );
    expect(screen.getByLabelText("Role for Ada")).toBeTruthy();
    expect(screen.getByLabelText("Role for Bo")).toBeTruthy();
    // Your own row says "Leave", not "Remove" — an owner stepping out of a
    // workspace is a different act from removing somebody else.
    expect(screen.getByRole("button", { name: "Remove Ada" }).textContent).toContain(
      "Leave",
    );
    expect(screen.getByRole("button", { name: "Remove Bo" }).textContent).toContain(
      "Remove",
    );
  });

  it("promotes a member and keeps the returned row", async () => {
    const onChange = vi.fn();
    setAdminMemberRole.mockResolvedValue({ ...MEMBER, role: "owner" });
    render(
      createElement(MembersPanel, {
        members: [OWNER, MEMBER],
        onChange,
        setError: vi.fn(),
      }),
    );
    const select = screen.getByLabelText("Role for Bo");
    act(() => {
      fireEvent.change(select, { target: { value: "owner" } });
    });
    await waitFor(() => expect(setAdminMemberRole).toHaveBeenCalledWith("m2", "owner"));
    expect(onChange.mock.calls[0][0]).toEqual([OWNER, { ...MEMBER, role: "owner" }]);
  });

  it("surfaces the last-owner refusal instead of a guess", async () => {
    const setError = vi.fn();
    const detail =
      "This is the workspace's last owner. Promote somebody else first — a " +
      "workspace with no owner cannot be administered by anyone.";
    setAdminMemberRole.mockRejectedValue(new ApiError(detail, 409));
    render(
      createElement(MembersPanel, {
        members: [OWNER],
        onChange: vi.fn(),
        setError,
      }),
    );
    const select = screen.getByLabelText("Role for Ada");
    act(() => {
      fireEvent.change(select, { target: { value: "member" } });
    });
    await waitFor(() => expect(setError).toHaveBeenCalledWith(detail));
  });

  it("asks before removing anybody, and does nothing when refused", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(
      createElement(MembersPanel, {
        members: [OWNER, MEMBER],
        onChange: vi.fn(),
        setError: vi.fn(),
      }),
    );
    act(() => screen.getByRole("button", { name: "Remove Bo" }).click());
    await flush();
    expect(confirm).toHaveBeenCalled();
    expect(removeAdminMember).not.toHaveBeenCalled();
    confirm.mockRestore();
  });
});

describe("InvitesPanel", () => {
  it("lists an invitation without ever showing a token", () => {
    render(
      createElement(InvitesPanel, {
        invites: [INVITE],
        onChange: vi.fn(),
        setError: vi.fn(),
      }),
    );
    expect(screen.getByText("cy@example.test")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Withdraw the invitation/ })).toBeTruthy();
    expect(document.body.textContent).not.toContain("token");
  });

  it("shows the accept link once, after creating an invitation", async () => {
    const onChange = vi.fn();
    createInvite.mockResolvedValue({
      invite: INVITE,
      accept_url: "https://app.test/auth/invite?token=raw-secret",
    });
    render(
      createElement(InvitesPanel, {
        invites: [],
        onChange,
        setError: vi.fn(),
      }),
    );
    act(() => {
      fireEvent.change(screen.getByLabelText("Email"), {
        target: { value: "cy@example.test" },
      });
    });
    act(() => screen.getByRole("button", { name: /Send invitation/ }).click());
    await waitFor(() =>
      expect(createInvite).toHaveBeenCalledWith("cy@example.test", "member"),
    );
    await waitFor(() =>
      expect(
        screen.getByText("https://app.test/auth/invite?token=raw-secret"),
      ).toBeTruthy(),
    );
    expect(onChange).toHaveBeenCalledWith([INVITE]);
  });

  it("reports the already-a-member refusal on the form, not as a toast", async () => {
    const setError = vi.fn();
    createInvite.mockRejectedValue(
      new ApiError("That address is already a member of this workspace", 409),
    );
    render(
      createElement(InvitesPanel, {
        invites: [],
        onChange: vi.fn(),
        setError,
      }),
    );
    act(() => screen.getByRole("button", { name: /Send invitation/ }).click());
    await waitFor(() =>
      expect(
        screen.getByRole("alert").textContent,
      ).toContain("already a member"),
    );
    // Kept beside the field that caused it: a form error routed to the global
    // toast is an error nobody connects to the thing they just typed.
    expect(setError).not.toHaveBeenCalled();
  });

  it("replaces a withdrawn invitation with the row the API returned", async () => {
    const onChange = vi.fn();
    revokeInvite.mockResolvedValue({ ...INVITE, status: "revoked" });
    render(
      createElement(InvitesPanel, {
        invites: [INVITE],
        onChange,
        setError: vi.fn(),
      }),
    );
    act(() =>
      screen.getByRole("button", { name: /Withdraw the invitation/ }).click(),
    );
    await waitFor(() => expect(revokeInvite).toHaveBeenCalledWith("i1"));
    expect(onChange.mock.calls[0][0]).toEqual([{ ...INVITE, status: "revoked" }]);
  });

  it("counts only the pending invitations", () => {
    render(
      createElement(InvitesPanel, {
        invites: [INVITE, { ...INVITE, id: "i2", status: "accepted" as const }],
        onChange: vi.fn(),
        setError: vi.fn(),
      }),
    );
    expect(document.querySelector(".panel-count")?.textContent).toBe("1");
  });
});
