import { describe, expect, it } from "vitest";
import type { Skill, SkillArg } from "@workspace/api-client";
import { argsSatisfied, matchSkills, stripSlashToken } from "../components/views/chat";

/**
 * The slash-picker's pure core, exercised without a DOM. These three functions
 * are the whole of "parse the '/' the user typed, choose the skills it names,
 * and refuse a turn a required arg would only 422." Everything around them in
 * the composer is JSX; only this decides what actually gets attached and sent.
 */

function arg(overrides: Partial<SkillArg> = {}): SkillArg {
  return {
    name: "topic",
    type: "string",
    label: "Topic",
    description: "",
    required: false,
    default: null,
    choices: [],
    ...overrides,
  };
}

function skill(overrides: Partial<Skill> = {}): Skill {
  return {
    id: "skill-1",
    name: "summarize",
    title: "Summarize a thread",
    description: "Condense the current conversation",
    body: "…",
    args: [],
    shared: false,
    version: 1,
    can_share: false,
    can_edit: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("matchSkills", () => {
  const skills = [
    skill({ id: "a", name: "summarize", title: "Summarize a thread", description: "Weekly digest" }),
    skill({ id: "b", name: "translate", title: "Translate", description: "Into French" }),
    skill({ id: "c", name: "outline", title: "Draft an outline", description: "Weekly plan" }),
  ];

  it("returns every skill for an empty or whitespace-only query", () => {
    // Just-typed "/" (query "") must offer the full list, not an empty picker.
    expect(matchSkills(skills, "")).toEqual(skills);
    expect(matchSkills(skills, "   ")).toEqual(skills);
  });

  it("matches the slug, the title, and the description case-insensitively", () => {
    expect(matchSkills(skills, "SUMM").map((s) => s.id)).toEqual(["a"]);
    expect(matchSkills(skills, "outline").map((s) => s.id)).toEqual(["c"]);
    // "french" lives only in the description — the picker still finds it.
    expect(matchSkills(skills, "french").map((s) => s.id)).toEqual(["b"]);
  });

  it("trims the query before matching and keeps order for multiple hits", () => {
    // "weekly" is in the description of "a" and "c" but not the middle "b";
    // trimming the padded query must still match, and the survivors keep their
    // input order rather than collapsing to the two that matched.
    const hits = matchSkills(skills, "  weekly  ");
    expect(hits.map((s) => s.id)).toEqual(["a", "c"]);
  });

  it("returns nothing when the query names no skill", () => {
    expect(matchSkills(skills, "deploy")).toEqual([]);
  });
});

describe("stripSlashToken", () => {
  it("removes the leading '/name ' token and keeps the rest of the draft", () => {
    expect(stripSlashToken("/summarize the last week")).toBe("the last week");
  });

  it("removes a bare '/name' with no trailing text", () => {
    expect(stripSlashToken("/summarize")).toBe("");
  });

  it("removes a lone '/'", () => {
    expect(stripSlashToken("/")).toBe("");
  });

  it("leaves a draft that does not start with a slash untouched", () => {
    // A "/" mid-sentence is ordinary text, not a command.
    expect(stripSlashToken("please read /etc/hosts")).toBe("please read /etc/hosts");
  });

  it("strips only the first token, preserving a later slash", () => {
    expect(stripSlashToken("/translate to /fr please")).toBe("to /fr please");
  });
});

describe("argsSatisfied", () => {
  it("is satisfied when no skill is attached", () => {
    expect(argsSatisfied(null, {})).toBe(true);
  });

  it("is satisfied when the skill declares no required args", () => {
    const s = skill({ args: [arg({ name: "tone", required: false })] });
    expect(argsSatisfied(s, {})).toBe(true);
  });

  it("blocks a blank or whitespace-only required arg", () => {
    const s = skill({ args: [arg({ name: "topic", required: true })] });
    expect(argsSatisfied(s, {})).toBe(false);
    expect(argsSatisfied(s, { topic: "" })).toBe(false);
    expect(argsSatisfied(s, { topic: "   " })).toBe(false);
  });

  it("passes once every required arg carries a value", () => {
    const s = skill({ args: [arg({ name: "topic", required: true })] });
    expect(argsSatisfied(s, { topic: "roadmap" })).toBe(true);
    // A numeric zero is a real value, not an absence.
    const n = skill({ args: [arg({ name: "count", type: "number", required: true })] });
    expect(argsSatisfied(n, { count: 0 })).toBe(true);
  });

  it("treats a required boolean as always satisfied", () => {
    // A checkbox's unchecked state reads as false, so a required boolean can
    // never block the send — the gate exempts it deliberately.
    const s = skill({ args: [arg({ name: "verbose", type: "boolean", required: true })] });
    expect(argsSatisfied(s, {})).toBe(true);
  });

  it("requires every required arg, not just one", () => {
    const s = skill({
      args: [arg({ name: "topic", required: true }), arg({ name: "lang", required: true })],
    });
    expect(argsSatisfied(s, { topic: "roadmap" })).toBe(false);
    expect(argsSatisfied(s, { topic: "roadmap", lang: "fr" })).toBe(true);
  });
});
