import { describe, expect, it } from "vitest";
import type { WorkspaceMember } from "@workspace/api-client";
import {
  completeMention,
  matchMembers,
  mentionQuery,
  parseMentions,
  splitMentions,
} from "../components/views/comment-format";

const members: WorkspaceMember[] = [
  { user_id: "u-ada", name: "Ada Lovelace", role: "owner" },
  { user_id: "u-adam", name: "Adam", role: "member" },
  { user_id: "u-grace", name: "Grace Hopper", role: "member" },
];

describe("mentionQuery", () => {
  it("finds the @-token being typed at the end of the draft", () => {
    expect(mentionQuery("ping @gra")).toBe("gra");
    expect(mentionQuery("@")).toBe("");
    expect(mentionQuery("ping @ada l")).toBe("ada l");
  });

  it("is not fooled by email addresses or finished sentences", () => {
    // "a@b" opens no word, and a token that is not at the caret is not being
    // typed — offering a picker over either would fight the user mid-word.
    expect(mentionQuery("mail ada@example.com")).toBeNull();
    expect(mentionQuery("@ada said this already")).toBeNull();
    expect(mentionQuery("no mention here")).toBeNull();
  });
});

describe("matchMembers", () => {
  it("ranks name-prefix over word-prefix over substring", () => {
    expect(matchMembers(members, "ada").map((m) => m.user_id)).toEqual([
      "u-ada",
      "u-adam",
    ]);
    // "hopper" is a word-prefix hit for Grace, not a name-prefix one.
    expect(matchMembers(members, "hopper").map((m) => m.user_id)).toEqual([
      "u-grace",
    ]);
  });

  it("offers everyone on an empty query", () => {
    expect(matchMembers(members, "").length).toBe(3);
  });
});

describe("completeMention", () => {
  it("replaces the trailing token with the picked name", () => {
    expect(completeMention("ping @gra", "Grace Hopper")).toBe(
      "ping @Grace Hopper ",
    );
  });
});

describe("parseMentions", () => {
  it("derives the ids the request should carry, in order of appearance", () => {
    expect(
      parseMentions("cc @Grace Hopper and @Adam please", members),
    ).toEqual(["u-grace", "u-adam"]);
  });

  it("gives a long name priority over its prefix", () => {
    // Without longest-first matching "@Ada Lovelace" would be claimed by a
    // hypothetical "@Ada" and the rest would read as stray text.
    expect(parseMentions("ask @Ada Lovelace", members)).toEqual(["u-ada"]);
  });

  it("does not count words that merely contain a name", () => {
    expect(parseMentions("madam@Adam.com is an address", members)).toEqual([]);
    expect(parseMentions("@Adamant is not Adam", members)).toEqual([]);
  });

  it("is case-insensitive and de-duplicates", () => {
    expect(parseMentions("@adam then @Adam again", members)).toEqual([
      "u-adam",
    ]);
  });
});

describe("splitMentions", () => {
  it("splits a body into text runs and mention chips", () => {
    expect(
      splitMentions("cc @Adam soon", ["u-adam"], members),
    ).toEqual([
      { kind: "text", text: "cc " },
      { kind: "mention", text: "@Adam", user_id: "u-adam" },
      { kind: "text", text: " soon" },
    ]);
  });

  it("renders a dropped mention as the plain text it really was", () => {
    // The server keeps only real members' ids; a name it dropped must not
    // dress up as a chip and imply somebody was notified.
    expect(splitMentions("cc @Adam soon", [], members)).toEqual([
      { kind: "text", text: "cc @Adam soon" },
    ]);
  });

  it("hands back one text segment when nothing is mentioned", () => {
    expect(splitMentions("plain words", [], members)).toEqual([
      { kind: "text", text: "plain words" },
    ]);
  });
});
