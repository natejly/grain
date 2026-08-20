import { describe, expect, it } from "vitest";
import {
  BUILTIN_COMMANDS,
  commandDescription,
  matchCommands,
  parseAside,
} from "../components/views/commands";

describe("matchCommands", () => {
  it("offers every command on a bare slash", () => {
    expect(matchCommands("")).toEqual(BUILTIN_COMMANDS);
  });

  it("prefix-matches on the name, case-insensitively", () => {
    expect(matchCommands("pl").map((c) => c.name)).toEqual(["plan"]);
    expect(matchCommands("BT").map((c) => c.name)).toEqual(["btw"]);
    expect(matchCommands("lan")).toEqual([]);
  });

  it("closes once the draft has moved past the token", () => {
    // "/btw remember the deadline" is being written, not searched — the picker
    // must not hover over a note mid-type.
    expect(matchCommands("btw remember the deadline")).toEqual([]);
    expect(matchCommands("plan the migration")).toEqual([]);
  });
});

describe("commandDescription", () => {
  const plan = BUILTIN_COMMANDS.find((c) => c.name === "plan")!;
  const btw = BUILTIN_COMMANDS.find((c) => c.name === "btw")!;

  it("says which way the /plan toggle will flip", () => {
    expect(commandDescription(plan, "ask_writes")).toMatch(/Plan first/);
    expect(commandDescription(plan, null)).toMatch(/Plan first/);
    expect(commandDescription(plan, "plan")).toMatch(/off/);
  });

  it("leaves /btw alone", () => {
    expect(commandDescription(btw, "plan")).toBe(btw.description);
  });
});

describe("parseAside", () => {
  it("returns the note of a /btw draft", () => {
    expect(parseAside("/btw the deadline moved")).toBe("the deadline moved");
    expect(parseAside("  /BTW trimmed  ")).toBe("trimmed");
  });

  it("keeps a multi-line note whole", () => {
    expect(parseAside("/btw first line\nsecond line")).toBe(
      "first line\nsecond line",
    );
  });

  it("distinguishes an empty aside from no aside at all", () => {
    // "" means "an aside with nothing in it" — the composer refuses the send.
    expect(parseAside("/btw")).toBe("");
    expect(parseAside("/btw   ")).toBe("");
    // null means "not an aside" — an ordinary prompt goes out as one.
    expect(parseAside("btw no slash")).toBeNull();
    expect(parseAside("/btwsomething")).toBeNull();
    expect(parseAside("tell me /btw mid-sentence")).toBeNull();
  });
});
