import { describe, expect, it } from "vitest";
import type { ShareLink } from "@workspace/api-client";
import {
  linksFor,
  shareLinkState,
  shareLinkStateLabel,
  shareUrl,
} from "../components/views/share-format";

function link(overrides: Partial<ShareLink> = {}): ShareLink {
  return {
    id: "link-1",
    resource_kind: "dashboard",
    resource_id: "dash-1",
    created_by: "user-1",
    created_at: "2026-08-25T10:00:00",
    expires_at: null,
    revoked_at: null,
    ...overrides,
  };
}

describe("shareLinkState", () => {
  it("calls an untouched link active", () => {
    expect(shareLinkState(link())).toBe("active");
  });

  it("calls a revoked link revoked", () => {
    expect(shareLinkState(link({ revoked_at: "2026-08-25T11:00:00" }))).toBe(
      "revoked",
    );
  });

  it("calls a link past its expiry expired, and one before it active", () => {
    const now = new Date("2026-08-25T12:00:00");
    expect(
      shareLinkState(link({ expires_at: "2026-08-25T11:59:59" }), now),
    ).toBe("expired");
    expect(
      shareLinkState(link({ expires_at: "2026-08-25T12:00:01" }), now),
    ).toBe("active");
  });

  it("lets revoked beat expired: a person stopped this link, and the list should keep saying so", () => {
    const now = new Date("2026-08-25T12:00:00");
    const stopped = link({
      revoked_at: "2026-08-25T10:30:00",
      expires_at: "2026-08-25T11:00:00",
    });
    expect(shareLinkState(stopped, now)).toBe("revoked");
  });
});

describe("shareLinkStateLabel", () => {
  it("says what an active link means rather than just naming the state", () => {
    expect(shareLinkStateLabel("active")).toBe("Anyone with the link can view");
    expect(shareLinkStateLabel("revoked")).toBe("Revoked");
    expect(shareLinkStateLabel("expired")).toBe("Expired");
  });
});

describe("shareUrl", () => {
  it("joins the web origin and the server's path", () => {
    expect(shareUrl("https://grain.example", "/share/tok")).toBe(
      "https://grain.example/share/tok",
    );
  });

  it("does not double a trailing slash on the origin", () => {
    expect(shareUrl("https://grain.example/", "/share/tok")).toBe(
      "https://grain.example/share/tok",
    );
  });
});

describe("linksFor", () => {
  it("keeps only the links about this one resource, newest first", () => {
    const rows = [
      link({ id: "a", created_at: "2026-08-25T09:00:00" }),
      link({ id: "other-kind", resource_kind: "document" }),
      link({ id: "other-id", resource_id: "dash-2" }),
      link({ id: "b", created_at: "2026-08-25T10:00:00" }),
    ];
    expect(linksFor(rows, "dashboard", "dash-1").map((row) => row.id)).toEqual([
      "b",
      "a",
    ]);
  });
});
