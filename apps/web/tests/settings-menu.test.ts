import { describe, expect, it } from "vitest";
import { digestHourLabel } from "../components/settings-menu";

describe("digestHourLabel", () => {
  it("zero-pads the hour and names the timezone", () => {
    expect(digestHourLabel(0)).toBe("00:00 UTC");
    expect(digestHourLabel(9)).toBe("09:00 UTC");
    expect(digestHourLabel(23)).toBe("23:00 UTC");
  });
});
