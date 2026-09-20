// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * The Share popover's collaboration half. It renders the workspace-shared
 * model rather than changing it: everyone listed can already open the
 * document, the dots say who is around right now, and the invite button is a
 * deep-link to the Admin panel that already owns the flow — owner-only, with
 * an honest sentence for everyone else.
 */

const listShareLinks = vi.fn();
const listMembers = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    listShareLinks: (...a: unknown[]) => listShareLinks(...a),
    listMembers: (...a: unknown[]) => listMembers(...a),
    createShareLink: vi.fn(),
    revokeShareLink: vi.fn(),
  },
}));

import type { CoworkingPresence } from "@workspace/api-client";
import { ShareLinksModal, type SharePeople } from "../components/share-links-modal";
import type { CoworkingState } from "../components/use-coworking";

const MEMBERS = [
  { user_id: "u1", name: "Ada", role: "owner" },
  { user_id: "u2", name: "Bo", role: "member" },
  { user_id: "u3", name: "Cy", role: "member" },
];

function presence(overrides: Partial<CoworkingPresence>): CoworkingPresence {
  return {
    actor_id: "u2",
    actor_kind: "user",
    actor_label: "Bo",
    surface: "document:d1",
    state: {},
    updated_at: "2026-09-20T10:00:00Z",
    ...overrides,
  };
}

function coworking(presences: CoworkingPresence[]): CoworkingState {
  return {
    runs: [],
    presences,
    othersOn: () => [],
    report: () => undefined,
    reportPointer: () => undefined,
    leave: () => undefined,
  };
}

function modal(people?: SharePeople, close = () => undefined) {
  return createElement(ShareLinksModal, {
    kind: "document",
    resourceId: "d1",
    resourceName: "Runbook",
    close,
    people,
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the People with access section", () => {
  it("lists the roster with presence dots, brightest in this document", async () => {
    listShareLinks.mockResolvedValue([]);
    listMembers.mockResolvedValue(MEMBERS);
    const { container } = render(
      modal({
        coworking: coworking([
          presence({}), // Bo, in THIS document
          presence({ actor_id: "u3", actor_label: "Cy", surface: "board:b1" }),
        ]),
        surface: "document:d1",
        selfId: "u1",
        canInvite: true,
        openInvites: () => undefined,
      }),
    );

    expect(await screen.findByText("Ada (you)")).toBeTruthy();
    expect(listMembers).toHaveBeenCalledTimes(1);
    const dots = container.querySelectorAll(".share-presence-dot");
    expect(dots).toHaveLength(3);
    // Bo is on this very surface; Cy is merely online somewhere; Ada (this
    // viewer's own row) has no presence row in the stub and stays unlit.
    expect(container.querySelectorAll(".share-presence-dot.here")).toHaveLength(1);
    expect(screen.getByText("in this document")).toBeTruthy();
  });

  it("deep-links an owner to the invites panel and closes behind them", async () => {
    listShareLinks.mockResolvedValue([]);
    listMembers.mockResolvedValue(MEMBERS);
    const openInvites = vi.fn();
    const close = vi.fn();
    render(
      modal(
        {
          coworking: coworking([]),
          surface: "document:d1",
          selfId: "u1",
          canInvite: true,
          openInvites,
        },
        close,
      ),
    );

    fireEvent.click(await screen.findByRole("button", { name: /Invite teammate/ }));
    expect(openInvites).toHaveBeenCalledTimes(1);
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("offers a member the honest sentence instead of a door they cannot open", async () => {
    listShareLinks.mockResolvedValue([]);
    listMembers.mockResolvedValue(MEMBERS);
    render(
      modal({
        coworking: coworking([]),
        surface: "document:d1",
        selfId: "u2",
        canInvite: false,
        openInvites: () => undefined,
      }),
    );

    expect(
      await screen.findByText("Ask a workspace owner to invite teammates."),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Invite teammate/ })).toBeNull();
  });

  it("stays a plain links modal — no roster fetch — where people is not wired", () => {
    listShareLinks.mockResolvedValue([]);
    render(modal());
    expect(screen.queryByText("People with access")).toBeNull();
    expect(listMembers).not.toHaveBeenCalled();
  });
});
