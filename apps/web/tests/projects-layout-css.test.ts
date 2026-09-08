import { describe, expect, it } from "vitest";
import { ruleBody } from "./css-rules";

/**
 * The LaTeX preview iframe needs an unbroken flex height chain from
 * `.projects-layout` down to `.project-preview-frame`. Without it the iframe
 * collapses to the browser default (~150px) and the compiled PDF appears as a
 * short white band inside an otherwise empty pane.
 */

describe("projects layout fills the main panel", () => {
  it(".projects-layout participates in flex and has min-height: 0", () => {
    const body = ruleBody(".projects-layout");
    expect(body).toMatch(/flex:\s*1/);
    expect(body).toMatch(/min-height:\s*0/);
  });

  it(".project-preview fills its grid cell", () => {
    const body = ruleBody(".project-preview");
    expect(body).toMatch(/flex:\s*1/);
    expect(body).toMatch(/min-height:\s*0/);
  });

  it(".project-preview-frame stretches inside the preview", () => {
    const body = ruleBody(".project-preview-frame");
    expect(body).toMatch(/flex:\s*1/);
    expect(body).toMatch(/min-height:\s*0/);
  });
});
