import { describe, expect, it } from "vitest";
import {
  describeSecretMeta,
  isValidSecretName,
} from "../components/views/sandbox-secrets";

/**
 * The client half of the secrets story. The backend is authoritative on the
 * name rule, but the form validates first so a typo points at the field it came
 * from rather than returning as an opaque 400 — hence `isValidSecretName` must
 * mirror the backend's `NAME_RE` exactly. `describeSecretMeta` is the only thing
 * a card shows beyond the name, and it must never surface the value (which the
 * type does not even carry).
 */
describe("isValidSecretName", () => {
  it("accepts an UPPERCASE env var starting with a letter", () => {
    expect(isValidSecretName("STRIPE_API_KEY")).toBe(true);
    expect(isValidSecretName("A")).toBe(true);
    expect(isValidSecretName("OPENAI_KEY_2")).toBe(true);
  });

  it("trims surrounding whitespace before judging", () => {
    expect(isValidSecretName("  STRIPE_API_KEY  ")).toBe(true);
  });

  it("rejects anything that is not a plain env var name", () => {
    for (const bad of [
      "lowercase",
      "1LEADING_DIGIT",
      "HAS SPACE",
      "HAS-DASH",
      "",
      "HAS.DOT",
    ]) {
      expect(isValidSecretName(bad)).toBe(false);
    }
  });
});

describe("describeSecretMeta", () => {
  const base = {
    name: "STRIPE_API_KEY",
    created_by: "u1",
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
  };

  it("summarises provenance without ever carrying a value", () => {
    const line = describeSecretMeta(base);
    expect(line.startsWith("Added ")).toBe(true);
    expect(line).not.toContain("u1");
  });

  it("falls back to the raw string when the date will not parse", () => {
    expect(describeSecretMeta({ ...base, created_at: "not-a-date" })).toBe(
      "Added not-a-date",
    );
  });
});
