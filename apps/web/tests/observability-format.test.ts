import { describe, expect, it } from "vitest";
import {
  OBS_WINDOWS,
  PERCENTILES,
  formatAge,
  formatErrorRate,
  formatMs,
  obsWindowLabel,
  share,
} from "../components/views/observability-format";

/**
 * The panel's honesty lives in these functions, not the JSX: an unmeasured
 * percentile must read as a dash and never as `0ms` (which would look like an
 * instant answer nobody saw), a sub-one-percent error rate must stay visible
 * rather than round to `0%`, and a real-but-tiny bar must not collapse to
 * nothing. Each is a silent failure in a browser — a wrong number looks like a
 * number — so they are asserted here.
 */

describe("formatMs", () => {
  it("prints an em dash for a percentile nobody measured, never 0ms", () => {
    expect(formatMs(null)).toBe("—");
    expect(formatMs(null)).not.toContain("0");
  });

  it("keeps sub-second latency in milliseconds", () => {
    expect(formatMs(0)).toBe("0 ms");
    expect(formatMs(1)).toBe("1 ms");
    expect(formatMs(999)).toBe("999 ms");
  });

  it("switches to seconds, with a decimal only while it still matters", () => {
    expect(formatMs(1000)).toBe("1.0 s");
    expect(formatMs(1500)).toBe("1.5 s");
    expect(formatMs(9999)).toBe("10.0 s");
    expect(formatMs(10_000)).toBe("10 s");
    expect(formatMs(59_999)).toBe("60 s");
  });

  it("switches to minutes once seconds stop reading", () => {
    expect(formatMs(60_000)).toBe("1.0 min");
    expect(formatMs(90_000)).toBe("1.5 min");
  });
});

describe("formatAge", () => {
  it("climbs through the units a live run passes on its way to old", () => {
    expect(formatAge(0)).toBe("0s");
    expect(formatAge(59)).toBe("59s");
    expect(formatAge(60)).toBe("1m");
    expect(formatAge(3599)).toBe("59m");
    expect(formatAge(3600)).toBe("1h");
    expect(formatAge(86_399)).toBe("23h");
    expect(formatAge(86_400)).toBe("1d");
    expect(formatAge(172_800)).toBe("2d");
  });
});

describe("formatErrorRate", () => {
  it("rounds to whole percents in the common range", () => {
    expect(formatErrorRate(0)).toBe("0%");
    expect(formatErrorRate(0.5)).toBe("50%");
    expect(formatErrorRate(1)).toBe("100%");
  });

  it("keeps a real-but-tiny rate visible instead of rounding it to 0%", () => {
    // One failure in a few hundred runs is a real figure; "0%" would hide it.
    expect(formatErrorRate(0.002)).toBe("0.2%");
    expect(formatErrorRate(0.009)).toBe("0.9%");
  });

  it("does not spend a decimal on a rate that is already at least one percent", () => {
    expect(formatErrorRate(0.01)).toBe("1%");
    expect(formatErrorRate(0.126)).toBe("13%");
  });
});

describe("obsWindowLabel", () => {
  it("names every window the panel offers the way a person would", () => {
    expect(obsWindowLabel(1)).toBe("Last hour");
    expect(obsWindowLabel(6)).toBe("Last 6 hours");
    expect(obsWindowLabel(24)).toBe("Last 24 hours");
    expect(obsWindowLabel(72)).toBe("Last 3 days");
    expect(obsWindowLabel(168)).toBe("Last 7 days");
    expect(obsWindowLabel(720)).toBe("Last 30 days");
  });

  it("labels every offered window without falling through to a raw number", () => {
    for (const hours of OBS_WINDOWS) {
      const label = obsWindowLabel(hours);
      expect(label.startsWith("Last ")).toBe(true);
      expect(label).not.toMatch(/\.\d/); // no fractional days leaked in
    }
  });
});

describe("share", () => {
  it("is a percentage of the metric's own worst case", () => {
    expect(share(50, 100)).toBe(50);
    expect(share(100, 100)).toBe(100);
  });

  it("keeps a real-but-tiny value on screen, and draws nothing for a zero", () => {
    expect(share(1, 1000)).toBe(2); // 0.1% would vanish; the floor keeps it
    expect(share(0, 100)).toBe(0);
    expect(share(5, 0)).toBe(0);
    expect(share(-1, 100)).toBe(0);
  });
});

describe("PERCENTILES", () => {
  it("lists the latency rows in worsening order, ending at the max", () => {
    expect(PERCENTILES.map((row) => row.label)).toEqual(["p50", "p90", "p99", "max"]);
    expect(PERCENTILES.map((row) => row.key)).toEqual([
      "p50_ms",
      "p90_ms",
      "p99_ms",
      "max_ms",
    ]);
  });
});
