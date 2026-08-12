import { describe, expect, it } from "vitest";
import {
  egressLabel,
  linesToList,
  parseInputSchema,
} from "../components/views/sandbox-tools";

/**
 * These three helpers are the client half of the sandbox-tool safety story, so
 * each pins a property the backend relies on the form to have already enforced:
 *
 *  - `linesToList` is deliberately NOT a shell split. One line is one argv
 *    token, spaces and all, because the backend runs the tokens as argv and
 *    quotes each one — a token that got helpfully split here would become two
 *    arguments and change what command runs.
 *  - `parseInputSchema` rejects anything but a JSON object *before* send, so a
 *    typo points at the textarea rather than returning as an opaque 422; a JSON
 *    array or bare scalar parses fine yet is not a schema the model can use.
 *  - `egressLabel` renders the empty allowlist as "No network" — the strictest
 *    state — so a reviewer reading a card never mistakes "reaches nothing" for
 *    "reaches anything".
 */
describe("linesToList", () => {
  it("keeps one token per line, trimming surrounding whitespace", () => {
    expect(linesToList("curl\n  -sS  \nhttps://example.com")).toEqual([
      "curl",
      "-sS",
      "https://example.com",
    ]);
  });

  it("drops blank and whitespace-only lines", () => {
    expect(linesToList("a\n\n  \nb\n")).toEqual(["a", "b"]);
  });

  it("does not split a token on its internal spaces", () => {
    // The backend quotes each token as one argv argument; splitting here would
    // silently turn one argument into three.
    expect(linesToList("--data one two three")).toEqual(["--data one two three"]);
  });

  it("is empty for an empty or all-blank string", () => {
    expect(linesToList("")).toEqual([]);
    expect(linesToList("\n  \n\t\n")).toEqual([]);
  });
});

describe("parseInputSchema", () => {
  it("returns the parsed object for a JSON object", () => {
    expect(parseInputSchema('{"type":"object","properties":{}}')).toEqual({
      type: "object",
      properties: {},
    });
  });

  it("treats an empty string as the empty schema, never an error", () => {
    // The textarea can be blank; that means "no declared inputs", not invalid.
    expect(parseInputSchema("")).toEqual({});
  });

  it("rejects a JSON array — it parses, but is not a schema object", () => {
    expect(parseInputSchema("[1, 2, 3]")).toBeNull();
  });

  it("rejects a bare scalar", () => {
    expect(parseInputSchema("42")).toBeNull();
    expect(parseInputSchema('"a string"')).toBeNull();
    expect(parseInputSchema("true")).toBeNull();
  });

  it("rejects the JSON literal null", () => {
    // typeof null === "object", so this is the case an object check alone misses.
    expect(parseInputSchema("null")).toBeNull();
  });

  it("rejects malformed JSON without throwing", () => {
    expect(parseInputSchema("{ not json")).toBeNull();
    expect(parseInputSchema('{"a":}')).toBeNull();
  });
});

describe("egressLabel", () => {
  it("calls an empty allowlist No network, not open network", () => {
    expect(egressLabel([])).toBe("No network");
  });

  it("names exactly the hosts the tool may reach", () => {
    expect(egressLabel(["api.example.com"])).toBe("Reaches api.example.com");
    expect(egressLabel(["a.example.com", "b.example.com"])).toBe(
      "Reaches a.example.com, b.example.com",
    );
  });
});
