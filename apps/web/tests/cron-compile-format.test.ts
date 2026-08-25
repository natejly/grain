import { describe, expect, it } from "vitest";
import { formatNextFires } from "../components/views/cron-compile-format";

/**
 * The "Next: …" line under a compiled schedule. The compiler answers in UTC
 * instants; the helper's whole job is to render them as wall times in the
 * schedule's own zone, and to degrade to the viewer's zone — not a crash —
 * when the zone name is one the runtime refuses.
 */

const FIRES = ["2026-08-31T13:00:00Z", "2026-09-07T13:00:00Z", "2026-09-14T13:00:00Z"];

describe("formatNextFires", () => {
  it("renders each instant in the given zone, joined with a middot", () => {
    const line = formatNextFires(FIRES, "UTC");
    const expected = FIRES.map((iso) =>
      new Date(iso).toLocaleString(undefined, { timeZone: "UTC" }),
    ).join(" · ");
    expect(line).toBe(expected);
    expect(line.split(" · ")).toHaveLength(3);
  });

  it("actually applies the zone: the same instant reads differently across zones", () => {
    // 13:00 UTC is 22:00 in Tokyo and 09:00 in New York — whatever the
    // machine's locale, those two wall times can never format alike.
    expect(formatNextFires([FIRES[0]], "Asia/Tokyo")).not.toBe(
      formatNextFires([FIRES[0]], "America/New_York"),
    );
  });

  it("falls back to the plain locale rendering on a zone the runtime rejects", () => {
    const line = formatNextFires([FIRES[0]], "Not/AZone");
    expect(line).toBe(new Date(FIRES[0]).toLocaleString());
  });

  it("renders an empty list as an empty string", () => {
    expect(formatNextFires([], "UTC")).toBe("");
  });
});
