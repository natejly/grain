import { describe, expect, it } from "vitest";
import {
  nextTemplateName,
  templateBaseName,
} from "../components/views/template-format";

/**
 * "Save as template" is one click, so the *client* must land on a free name —
 * bouncing a 409 off a user who never typed a name is a dead end with no box
 * to fix. These pins hold the naming rules that keep that click conflict-free.
 */

describe("templateBaseName", () => {
  it("names the template after its source", () => {
    expect(templateBaseName("Client onboarding")).toBe(
      "Client onboarding template",
    );
  });

  it("survives a blank source name rather than producing ' template'", () => {
    expect(templateBaseName("   ")).toBe("Template");
  });
});

describe("nextTemplateName", () => {
  it("uses the base name when nothing holds it", () => {
    expect(nextTemplateName("Playbook", [])).toBe("Playbook");
  });

  it("counts upward past every taken name, starting at 2", () => {
    // "Playbook 2" reads as "the second one" — a bare duplicate never appears.
    expect(nextTemplateName("Playbook", ["Playbook"])).toBe("Playbook 2");
    expect(nextTemplateName("Playbook", ["Playbook", "Playbook 2"])).toBe(
      "Playbook 3",
    );
  });

  it("treats case as a collision, because a person would", () => {
    expect(nextTemplateName("Playbook", ["playbook"])).toBe("Playbook 2");
  });

  it("keeps every candidate inside the server's 160-character column", () => {
    const long = "x".repeat(200);
    const first = nextTemplateName(long, []);
    expect(first.length).toBe(160);
    const second = nextTemplateName(long, [first]);
    expect(second.length).toBeLessThanOrEqual(160);
    expect(second.endsWith(" 2")).toBe(true);
  });
});
