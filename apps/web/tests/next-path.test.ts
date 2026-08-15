import { describe, expect, it } from "vitest";
import {
  DEFAULT_NEXT,
  nextPathFrom,
  safeNextPath,
  signInUrlFor,
} from "../components/auth/next-path";

/**
 * The redirect fence on the invitation flow.
 *
 * `?next=` exists so an invitee who has to sign in comes back holding their
 * token instead of having to find the email again. A redirect target read out
 * of a query string is an open redirect unless something refuses the absolute
 * forms, and "starts with a slash" is not enough on its own — `//evil.test` is
 * protocol-relative and browsers follow it off-origin.
 */
describe("safeNextPath", () => {
  it("keeps an ordinary same-origin path", () => {
    expect(safeNextPath("/auth/invite?token=abc")).toBe("/auth/invite?token=abc");
    expect(safeNextPath("/")).toBe("/");
  });

  it("refuses every way of naming another origin", () => {
    for (const hostile of [
      "https://evil.test/steal",
      "http://evil.test",
      "//evil.test/steal",
      "/\\evil.test",
      "javascript:alert(1)",
      "evil.test",
    ]) {
      expect(safeNextPath(hostile)).toBe(DEFAULT_NEXT);
    }
  });

  it("falls back for a missing or empty value", () => {
    expect(safeNextPath(null)).toBe(DEFAULT_NEXT);
    expect(safeNextPath(undefined)).toBe(DEFAULT_NEXT);
    expect(safeNextPath("")).toBe(DEFAULT_NEXT);
  });
});

describe("nextPathFrom", () => {
  it("reads and fences the query parameter", () => {
    expect(nextPathFrom("?next=%2Fauth%2Finvite%3Ftoken%3Dabc")).toBe(
      "/auth/invite?token=abc",
    );
    expect(nextPathFrom("?next=https%3A%2F%2Fevil.test")).toBe(DEFAULT_NEXT);
    expect(nextPathFrom("")).toBe(DEFAULT_NEXT);
  });
});

describe("signInUrlFor", () => {
  it("round-trips a path through the sign-in page", () => {
    const url = signInUrlFor("/auth/invite?token=abc");
    expect(url.startsWith("/auth/login?next=")).toBe(true);
    expect(nextPathFrom(url.slice(url.indexOf("?")))).toBe("/auth/invite?token=abc");
  });

  it("does not add an empty parameter for the default target", () => {
    expect(signInUrlFor("/")).toBe("/auth/login");
    expect(signInUrlFor("https://evil.test")).toBe("/auth/login");
  });
});
